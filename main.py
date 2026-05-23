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
    if "drg" in fn_lower or "drawing" in fn_lower:
        scores["drawing"] = max(scores.get("drawing", 0), 0.8)
    if "spec" in fn_lower:
        scores["specification"] = max(scores.get("specification", 0), 0.8)
    if "sch" in fn_lower or "schedule" in fn_lower:
        scores["schedule"] = max(scores.get("schedule", 0), 0.8)
    if "mech" in fn_lower:
        scores["mechanical"] = max(scores.get("mechanical", 0), 0.7)
    if "elec" in fn_lower:
        scores["electrical"] = max(scores.get("electrical", 0), 0.7)
    
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


# ── Equipment Extraction ─────────────────────────────────────────
EQUIPMENT_PATTERNS = {
    "boiler": r"(?i)(boiler|gas boiler|combi boiler|system boiler|oil boiler)[s]?\s*[-–—]?\s*(.{5,80})",
    "pump": r"(?i)(circulator|circulating pump|heating pump|booster pump|shower pump)[s]?\s*[-–—]?\s*(.{5,80})",
    "valve": r"(?i)(zone valve|motorised valve|thermostatic valve|pressure relief|isolating valve|gate valve|ball valve|butterfly valve)[s]?\s*[-–—]?\s*(.{5,80})",
    "cylinder": r"(?i)(hot water cylinder|unvented cylinder|megaflo|heatrae|indirect cylinder|direct cylinder)[s]?\s*[-–—]?\s*(.{5,80})",
    "radiator": r"(?i)(radiator|panel radiator|column radiator|towel rail|designer radiator|kickspace)[s]?\s*[-–—]?\s*(.{5,80})",
    "fan_coil": r"(?i)(fan coil unit|fcu|fan coil)[s]?\s*[-–—]?\s*(.{5,80})",
    "air_handler": r"(?i)(air handling unit|ahu|air handler)[s]?\s*[-–—]?\s*(.{5,80})",
    "heat_recovery": r"(?i)(heat recovery|mvhr|hrv|heat exchanger)[s]?\s*[-–—]?\s*(.{5,80})",
    "expansion": r"(?i)(expansion vessel|pressure vessel|accumulator)[s]?\s*[-–—]?\s*(.{5,80})",
    "controls": r"(?i)(programmer|thermostat|smart thermostat|zone controller|wiring centre|nest|hive|tado|heatmiser)[s]?\s*[-–—]?\s*(.{5,80})",
}


def extract_equipment(text: str) -> List[Dict]:
    """Extract equipment items from schedule/specification text."""
    found = []
    seen = set()
    
    for category, pattern in EQUIPMENT_PATTERNS.items():
        matches = re.finditer(pattern, text)
        for m in matches:
            item = m.group(0).strip()[:120]
            if item not in seen:
                seen.add(item)
                found.append({
                    "category": category,
                    "item": item,
                    "estimated_price": None,  # Will be populated
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
    """Find best price match for an equipment item."""
    item_lower = item.lower()
    
    # Direct match
    for key, price in PRICE_DB.items():
        if key in item_lower:
            return price
    
    # Partial match — check each word
    words = item_lower.split()
    best_score = 0
    best_price = None
    
    for key, price in PRICE_DB.items():
        key_words = key.split()
        score = sum(1 for kw in key_words if any(kw in w for w in words))
        if score > best_score:
            best_score = score
            best_price = price
    
    if best_score >= 3:
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
    
    # Process each document
    results = {
        "project_id": project_id,
        "project_name": project_name,
        "files_processed": len(saved_files),
        "classified": [],
        "equipment_extracted": [],
        "pricing_errors": 0,
    }
    
    all_text = ""
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
        all_text += "\n" + text
    
    # Extract equipment from all schedules / spec sheets
    equipment = extract_equipment(all_text)
    
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
