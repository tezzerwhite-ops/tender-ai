"""
TenderAI — Automated Document Intake + Equipment Extraction + Live Pricing
Phase 1: Sort, extract, price. Phase 2 (future): auto-takeoff from drawings.
"""
import os
import re
import json
import zipfile
import shutil
import asyncio
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx

app = FastAPI(title="TenderAI", version="0.1.0")

BASE_DIR = Path(__file__).parent
UPLOADS = BASE_DIR / "uploads"
OUTPUT = BASE_DIR / "output"
PRICING = BASE_DIR / "pricing"

for d in [UPLOADS, OUTPUT, PRICING]:
    d.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ── Document Classification ─────────────────────────────────────
TRADE_KEYWORDS = {
    "drawing": ["scale", "dimension", "elevation", "section", "plan view",
                 "detail a", "revision", "drawn by", "checked by", "cad", "sheet",
                 "floor plan", "roof plan", "site plan", "section a-a"],
    "specification": ["specification", "shall", "comply with", "standard",
                       "manufacturer", "warranty", "performance criteria",
                       "scope of work", "preliminaries", "conditions of contract"],
    "schedule": ["schedule", "qty", "quantity", "unit", "rate", "total",
                  "model", "make", "supplier", "item", "description",
                  "equipment schedule", "door schedule", "finish schedule"],
    "mechanical": ["mechanical", "hvac", "heating", "ventilation", "air conditioning",
                    "boiler", "radiator", "pipework", "ductwork", "extract", "supply",
                    "lthw", "chw", "flow rate", "pressure drop", "ahu", "fan coil"],
    "electrical": ["electrical", "lighting", "power", "distribution", "consumer unit",
                    "circuit", "cable", "wiring", "socket", "switch", "luminaire",
                    "mcb", "rcbo", "rcd", "db", "earth", "bonding"],
    "correspondence": ["dear", "regards", "meeting", "minutes", "agenda",
                        "email", "letter", "attached", "please find"],
}


def classify_document(text: str, filename: str) -> Dict[str, float]:
    """Score document against trade categories. Returns top match."""
    text_lower = text.lower()
    scores = {}
    for category, keywords in TRADE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        scores[category] = score / max(1, len(keywords))  # normalize
    
    # Boost filename hints
    fn_lower = filename.lower()
    if "drg" in fn_lower or "drawing" in fn_lower or "drw" in fn_lower:
        scores["drawing"] = max(scores.get("drawing", 0), 0.8)
    # "drawing pack" or "cat a drawing pack" is a drawing, not a schedule
    if "drawing pack" in fn_lower or "drg pack" in fn_lower:
        scores["drawing"] = 0.9
        scores["schedule"] = scores.get("schedule", 0) * 0.3  # suppress schedule
    if "spec" in fn_lower:
        scores["specification"] = max(scores.get("specification", 0), 0.8)
    if "sch" in fn_lower or "schedule" in fn_lower:
        scores["schedule"] = max(scores.get("schedule", 0), 0.8)
    if "mech" in fn_lower:
        scores["mechanical"] = max(scores.get("mechanical", 0), 0.7)
    if "elec" in fn_lower:
        scores["electrical"] = max(scores.get("electrical", 0), 0.7)

    # Auto-detect schedule from table patterns
    lines = text_lower.split('\n')
    tabular_lines = [l for l in lines if '\t' in l or '|' in l or l.count(',') >= 2]
    has_equipment_headers = any(h in text_lower for h in ['qty', 'quantity', 'unit', 'rate', 'total', 'description', 'model', 'make', 'supplier'])
    amount_lines = len([l for l in lines if re.search(r'\d+\.\d{2}', l)])
    
    if len(tabular_lines) >= 3 and has_equipment_headers:
        scores["schedule"] = max(scores.get("schedule", 0), 0.85)
    elif len(tabular_lines) >= 10 and amount_lines >= 5:
        scores["schedule"] = max(scores.get("schedule", 0), 0.75)
    
    # Boost specification if it contains equipment mentions
    equip_count = 0
    for pattern in EQUIPMENT_PATTERNS.values():
        equip_count += len(re.findall(pattern, text_lower))
    if equip_count >= 3 and scores.get("specification", 0) > 0:
        scores["specification"] = min(scores["specification"] + 0.2, 1.0)
    
    # Best match (or "drawing" for PDFs with no clear text — likely drawings)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    if not ranked or ranked[0][1] == 0:
        return {"category": "general", "confidence": 0.0}
    
    return {"category": ranked[0][0], "confidence": round(ranked[0][1], 2)}


def extract_text_from_pdf(filepath: str) -> str:
    """Extract text from a PDF file using pymupdf."""
    try:
        import fitz  # pymupdf
        doc = fitz.open(filepath)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text[:50000]  # Cap at 50K chars
    except ImportError:
        return ""
    except Exception:
        return ""


def extract_tables_from_pdf(filepath: str) -> List[List[str]]:
    """Extract structured tables from a PDF using pymupdf table detection.
    Returns list of table rows (each row is a list of cell strings)."""
    try:
        import fitz
        doc = fitz.open(filepath)
        all_rows = []
        for page in doc:
            tables = page.find_tables()
            for table in tables:
                rows = table.extract()
                if rows:
                    all_rows.extend(rows)
        doc.close()
        return all_rows
    except ImportError:
        return []
    except Exception:
        return []


def parse_schedule_rows(rows: List[List[str]]) -> List[Dict]:
    """Parse table rows looking for equipment schedule data.
    Detects header row (with Qty/Unit/Rate/Description columns) and extracts data rows."""
    if not rows:
        return []
    
    equipment = []
    # Find header row index and column positions
    header_idx = -1
    col_map = {}
    header_keywords = {
        'qty': ['qty', 'quantity', 'no.', 'count', 'qnty', 'nr'],
        'description': ['description', 'item', 'equipment', 'details', 'name', 'model'],
        'unit': ['unit', 'uom'],
        'rate': ['rate', 'price', 'cost', 'each', 'total'],
        'supplier': ['supplier', 'make', 'manufacturer'],
    }
    
    for i, row in enumerate(rows):
        row_lower = [str(c).lower().strip() for c in row]
        row_text = ' '.join(row_lower)
        matches = 0
        for header_set in header_keywords.values():
            if any(h in row_text for h in header_set):
                matches += 1
        if matches >= 3:
            header_idx = i
            # Map columns
            for j, cell in enumerate(row_lower):
                for col_name, keywords in header_keywords.items():
                    if any(kw == cell or kw in cell for kw in keywords):
                        col_map[col_name] = j
            break
    
    if header_idx < 0 or 'description' not in col_map:
        return []
    
    # Extract data rows
    for row in rows[header_idx + 1:]:
        cells = [str(c).strip() for c in row]
        if len(cells) <= col_map.get('description', 0):
            continue
        
        description = cells[col_map['description']] if 'description' in col_map else ''
        if not description or len(description) < 3:
            continue
        
        qty = cells[col_map['qty']] if 'qty' in col_map and col_map['qty'] < len(cells) else '1'
        rate = cells[col_map['rate']] if 'rate' in col_map and col_map['rate'] < len(cells) else ''
        
        # Clean up qty — extract first number
        qty_num = 1
        qty_match = re.search(r'(\d+)', str(qty))
        if qty_match:
            qty_num = int(qty_match.group(1))
        
        # Clean up rate — extract price
        rate_val = None
        rate_match = re.search(r'[\d,]+\.?\d*', str(rate).replace(',', ''))
        if rate_match:
            try:
                rate_val = float(rate_match.group())
            except ValueError:
                pass
        
        item_text = f"{description} (Qty: {qty_num})"
        if rate_val:
            item_text += f" [Schedule rate: £{rate_val:.2f}]"
        
        # Categorize
        cat = "general"
        desc_lower = description.lower()
        for category, patterns in [
            ("boiler", ["boiler", "combi", "system boiler"]),
            ("pump", ["pump", "circulator", "accelerator"]),
            ("valve", ["valve", "motorised", "actuator"]),
            ("cylinder", ["cylinder", "megaflo", "buffer", "vessel"]),
            ("radiator", ["radiator", "panel", "towel rail", "column"]),
            ("fan_coil", ["fan coil", "fcu"]),
            ("air_handler", ["ahu", "air handling"]),
            ("heat_recovery", ["mvhr", "heat recovery", "hrv"]),
            ("consumer_unit", ["consumer unit", "distribution board", "mcb"]),
            ("luminaire", ["luminaire", "led panel", "downlight", "light"]),
            ("cable", ["cable", "swa", "fp200", "cat6"]),
            ("pipe", ["pipe", "tube", "copper", "speedfit"]),
        ]:
            if any(p in desc_lower for p in patterns):
                cat = category
                break
        
        equipment.append({
            "category": cat,
            "item": item_text[:150],
            "estimated_price": rate_val if rate_val else None,
            "source": "schedule_table",
            "quantity": qty_num,
        })
    
    return equipment


# ── Equipment Extraction ─────────────────────────────────────────
EQUIPMENT_PATTERNS = {
    "boiler": r"(?i)(boiler|gas boiler|combi boiler|system boiler|oil boiler|condensing boiler)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "pump": r"(?i)(pump|circulator|circulating pump|heating pump|booster pump|shower pump|accelerator)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "valve": r"(?i)(zone valve|motorised valve|thermostatic valve|pressure relief|isolating valve|gate valve|ball valve|butterfly valve|control valve|mixing valve)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "cylinder": r"(?i)(hot water cylinder|unvented cylinder|megaflo|heatrae|indirect cylinder|direct cylinder|buffer vessel|thermal store|calorifier)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "radiator": r"(?i)(radiator|panel radiator|column radiator|towel rail|designer radiator|kickspace|lst radiator|convector)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "fan_coil": r"(?i)(fan coil unit|fcu|fan coil|fan convector)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "air_handler": r"(?i)(air handling unit|ahu|air handler|rooftop unit|rtu)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "heat_recovery": r"(?i)\b(heat recovery|mvhr|hrv|heat exchanger|plate heat exchanger|phe)\b[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "expansion": r"(?i)(expansion vessel|pressure vessel|accumulator|expansion tank)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "controls": r"(?i)(programmer|thermostat|smart thermostat|zone controller|wiring centre|nest|hive|tado|heatmiser|time clock)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "consumer_unit": r"(?i)(consumer unit|distribution board|fuse board|switchboard|panel board|mcb board)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "luminaire": r"(?i)(luminaire|light fitting|led panel|troffer|downlight|floodlight|bulkhead|batten)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "cable": r"(?i)(swa cable|armoured cable|fp200|fire cable|data cable|cat6|cat6a|tray cable)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
    "pipe": r"(?i)(copper tube|copper pipe|steel tube|stainless pipe|plastic pipe|speedfit|hep2o|mapress|geberit)[s]?\s*[-–—:]?\s*([^.\n]{3,40})",
}

# Spec-prose keywords — if the extracted item contains these, it's not equipment
PROSE_FILTER = [
    "shall", "fitted on", "discharge from", "connect into", "allow for",
    "each service", "range of", "accordance", "specified", "approved",
    "installed", "supplied", "contractor", "subcontractor", "testing",
    "commissioning", "maintenance", "access", "provision", "complete with",
    "as per", "to bs", "to en", "standard", "schedule of", "drawing no",
    "refer to", "note:", "typical", "indicative",  "allow", "ensure",
]


def extract_equipment(text: str) -> List[Dict]:
    """Extract equipment items from schedule/specification text.
    Filters out spec prose (items containing 'shall', 'fitted', etc.)"""
    found = []
    seen = set()
    
    for category, pattern in EQUIPMENT_PATTERNS.items():
        matches = re.finditer(pattern, text)
        for m in matches:
            item = m.group(0).strip()
            
            # Reject if too long (spec prose, not equipment)
            if len(item) > 80:
                continue
            
            # Reject if it contains spec-prose language
            item_lower = item.lower()
            if any(kw in item_lower for kw in PROSE_FILTER):
                continue
            
            # Reject single-word nonsense (like "Phenolic")
            if len(item.split()) <= 1:
                continue
            
            if item not in seen:
                seen.add(item)
                found.append({
                    "category": category,
                    "item": item[:120],
                    "estimated_price": None,
                    "source": "extracted",
                })
    
    return found


# ── Live Pricing ─────────────────────────────────────────────────
# UK trade supplier pricing — representative 2026 data
PRICE_DB = {
    # Boilers
    "worcester bosch greenstar 4000 30kw combi": 1450,
    "worcester bosch greenstar 8000 35kw combi": 1850,
    "vaillant ecotec plus 832 combi": 1350,
    "vaillant ecotec plus 838 combi": 1550,
    "ideal logic max combi 30kw": 950,
    "ideal logic max combi 35kw": 1100,
    "baxi 830 combi": 980,
    "baxi 836 combi": 1150,
    "viessmann vitodens 100-w 30kw": 1200,
    "viessmann vitodens 200-w 35kw": 1600,
    "alpha e-tec 33": 820,
    
    # Cylinders
    "megaflo 210l unvented": 950,
    "megaflo 250l unvented": 1100,
    "heatrae sadia megaflo 170l": 850,
    "joule cyclone 200l": 720,
    "telford tempest 210l": 680,
    "gledhill 200l": 600,
    
    # Pumps
    "grundfos ups3 15-50/65": 110,
    "grundfos alpha 2 15-60": 140,
    "grundfos magna3 25-80": 450,
    "wilo yonos pico 25/1-6": 95,
    "dab evosta 3 15-60/130": 85,
    "salamander ct50+ twin": 250,
    "stuart turner monsoon 3.0 bar": 380,
    
    # Radiators
    "stelrad compact 600x1000 k1": 120,
    "stelrad compact 600x1200 k2": 180,
    "stellrad softline 600x1000 k1": 110,
    "stellrad softline 600x1200 k2": 165,
    "myson premier he 600x1000": 95,
    "quinn round top 600x1000": 85,
    "acova column 600x1000 4-col": 250,
    "reina neval 1800x500 vertical": 320,
    "milano aruba 1800x500 vertical": 280,
    
    # Controls
    "hive active heating": 130,
    "nest learning thermostat 3rd gen": 200,
    "tado smart thermostat v3+": 140,
    "heatmiser neostat v2": 90,
    "honeywell dt4r wireless": 120,
    "danfoss tp5000si": 55,
    
    # Pipe & Fittings (per metre / each)
    "22mm copper tube": 6.50,
    "15mm copper tube": 4.20,
    "28mm copper tube": 9.80,
    "35mm copper tube": 14.50,
    "22mm speedfit pipe": 4.80,
    "15mm speedfit pipe": 3.20,
    "22mm press fitting elbow": 4.50,
    "15mm press fitting elbow": 3.20,
    "22mm press fitting tee": 5.80,
    "15mm press fitting tee": 4.20,
    
    # Valves
    "honeywell v4073a 3-port valve": 85,
    "honeywell v4043h 2-port valve 22mm": 65,
    "danfoss hpa2 2-port valve": 55,
    "danfoss hsa3 3-port valve": 75,
    "drayton za5 2-port": 50,
    "pegler terrier trv": 12,
    "danfoss ras-c2 trv": 15,
    
    # Expansion
    "18l expansion vessel": 35,
    "24l expansion vessel": 45,
    "35l expansion vessel": 65,
    "50l expansion vessel": 80,
    
    # Labour rates
    "heating engineer labour day": 350,
    "electrician labour day": 320,
    "labourer day": 200,
    "apprentice day": 120,
}


def price_equipment(item: str) -> Optional[float]:
    """Find best price match for an equipment item using fuzzy matching."""
    item_lower = item.lower()
    
    # Direct match: the entire key appears as a substring
    for key, price in PRICE_DB.items():
        if key in item_lower or item_lower in key:
            return price
    
    # Token overlap: count how many key words appear anywhere in the item
    item_words = set(item_lower.split())
    best_score = 0
    best_price = None
    
    for key, price in PRICE_DB.items():
        key_words = key.split()
        # Count words from key that appear in item (substring match, not just whole-word)
        score = sum(1 for kw in key_words if kw in item_lower)
        if score > best_score:
            best_score = score
            best_price = price
    
    if best_score >= 2:
        return best_price
    
    return None


# ── API Endpoints ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})


@app.post("/api/upload")
async def api_upload(files: List[UploadFile] = File(...), project_name: str = Form("Untitled Project")):
    """Upload a pack of documents — drawings, specs, schedules."""
    
    # Create project folder
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    project_slug = re.sub(r'[^a-zA-Z0-9]', '_', project_name)[:40]
    project_id = f"{project_slug}_{timestamp}"
    project_dir = OUTPUT / project_id
    project_dir.mkdir(parents=True)
    
    # Save uploaded files
    saved_files = []
    for file in files:
        if not file.filename:
            continue
        safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', file.filename)
        filepath = project_dir / safe_name
        content = await file.read()
        filepath.write_bytes(content)
        saved_files.append({"filename": file.filename, "path": str(filepath), "size": len(content)})
    
    # Process each document — classify first, then extract
    results = {
        "project_id": project_id,
        "project_name": project_name,
        "files_processed": len(saved_files),
        "classified": [],
        "equipment_extracted": [],
        "schedules_found": 0,
        "pricing_errors": 0,
    }
    
    all_equipment = []
    for f in saved_files:
        filepath = f["path"]
        if filepath.lower().endswith('.pdf'):
            text = extract_text_from_pdf(filepath)
        else:
            text = ""
        
        classification = classify_document(text, f["filename"])
        results["classified"].append({
            "filename": f["filename"],
            "category": classification["category"],
            "confidence": classification["confidence"],
        })
        
        # SMART ROUTING: Different strategy per document type
        cat = classification["category"]
        conf = classification["confidence"]
        
        if cat == "schedule":
            # Try table extraction first for schedule-classified files
            tables = extract_tables_from_pdf(filepath)
            schedule_equipment = parse_schedule_rows(tables)
            if schedule_equipment:
                results["schedules_found"] += 1
                all_equipment.extend(schedule_equipment)
            # Always also try regex on schedule text as fallback
            all_equipment.extend(extract_equipment(text))
        
        elif cat in ("specification", "mechanical", "electrical"):
            # Specs and trade documents — regex extraction + table backup
            all_equipment.extend(extract_equipment(text))
            tables = extract_tables_from_pdf(filepath)
            extra = parse_schedule_rows(tables)
            if extra:
                results["schedules_found"] += 1
                all_equipment.extend(extra)
        
        elif cat == "general" and len(text) > 200:
            # Unknown but has text — try basic extraction
            all_equipment.extend(extract_equipment(text))
        
        # Drawings (text < 200 chars): skip — just title blocks
    
    # Deduplicate equipment by item text
    seen = set()
    equipment = []
    for e in all_equipment:
        key = e["item"].lower()[:80]
        if key not in seen:
            seen.add(key)
            equipment.append(e)
    
    # Price the equipment
    for item in equipment:
        price = price_equipment(item["item"])
        if price:
            item["estimated_price"] = price
        else:
            results["pricing_errors"] += 1
    
    results["equipment_extracted"] = equipment
    results["total_estimated_materials"] = round(sum(
        e["estimated_price"] or 0 for e in equipment
    ), 2)
    
    # Generate output files
    # 1. Equipment schedule CSV
    csv_path = project_dir / "equipment_schedule.csv"
    with open(csv_path, "w") as f:
        f.write("Category,Item,Estimated Price (£)\n")
        for e in equipment:
            price = f"£{e['estimated_price']:.2f}" if e['estimated_price'] else "N/A"
            f.write(f"{e['category']},{e['item']},{price}\n")
    
    # 2. Classification report
    report_path = project_dir / "document_classification.json"
    report_path.write_text(json.dumps(results["classified"], indent=2))
    
    # 3. Summary markdown
    summary_path = project_dir / "SUMMARY.md"
    summaries = []
    cats = {}
    for c in results["classified"]:
        cats.setdefault(c["category"], []).append(c["filename"])
    
    for cat, files in sorted(cats.items()):
        summaries.append(f"### {cat.title()} ({len(files)} files)")
        for fn in files:
            summaries.append(f"- {fn}")
    
    summary_md = f"""# TenderAI — {project_name}
**Processed:** {datetime.now().strftime('%d %B %Y at %H:%M')}

## Document Breakdown

{chr(10).join(summaries)}

## Equipment Extract

| Category | Item | Estimated Price |
|----------|------|----------------|
"""
    for e in equipment:
        price = f"£{e['estimated_price']:.2f}" if e['estimated_price'] else "TBC"
        summary_md += f"| {e['category']} | {e['item'][:60]} | {price} |\n"
    
    summary_md += f"\n**Total Estimated Materials:** £{results['total_estimated_materials']:.2f}"
    summary_md += f"\n\n*{results['pricing_errors']} items could not be auto-priced — check manually.*"
    
    summary_path.write_text(summary_md)
    
    return JSONResponse(results)


@app.get("/api/projects")
async def api_list_projects():
    """List processed projects."""
    projects = []
    for d in sorted(OUTPUT.iterdir(), reverse=True):
        if d.is_dir():
            summary = d / "SUMMARY.md"
            csv_file = d / "equipment_schedule.csv"
            projects.append({
                "id": d.name,
                "has_summary": summary.exists(),
                "has_csv": csv_file.exists(),
                "created": datetime.fromtimestamp(d.stat().st_mtime).isoformat(),
            })
    return projects[:20]


@app.get("/api/projects/{project_id}/download")
async def api_download_project(project_id: str):
    """Download processed project as ZIP."""
    project_dir = OUTPUT / project_id
    if not project_dir.exists():
        return JSONResponse({"error": "Project not found"}, status_code=404)
    
    zip_path = OUTPUT / f"{project_id}.zip"
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file in project_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(project_dir))
    
    return FileResponse(zip_path, filename=f"{project_id}.zip",
                        media_type="application/zip")


# ── Run ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
