"""
PropScan — Property Feature Analysis API
Render deployment. uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "propscan.db"
FREE_PREVIEW = 5

STREETS = [
    "Bower","Pacific","Stuart","Quinton","Darley","Wentworth","Ocean",
    "Marine","Victoria","Albert","George","King","Queen","Park","Beach",
    "Bay","Lake","Hill","Valley","Cedar","Pine","Oak","Elm","Palm",
    "Rose","Banksia","Waratah","Grevillea","Acacia","Kurrajong",
    "Jacaranda","Eucalyptus","Hibiscus","Jasmine","Magnolia","Camellia",
]
ST_TYPES = ["St","Rd","Ave","Dr","Pde","Cres","Ct","Pl","Tce","Way","Ln","Cl"]
VARIANTS = ["Rectangular","Kidney","Freeform","Lap","L-Shaped","Oval","Compact","Extended"]
FINISHES = ["Type-A","Type-B","Type-C","Type-D","Type-E"]

REGIONS = {
    "Kellyville NSW 2155": {"lat":-33.709,"lng":150.956,"est":110},
    "Castle Hill NSW 2154": {"lat":-33.731,"lng":151.005,"est":95},
    "Ascot QLD 4007": {"lat":-27.4326,"lng":153.0597,"est":91},
    "Palm Beach QLD 4221": {"lat":-28.1118,"lng":153.4637,"est":88},
    "Mosman NSW 2088": {"lat":-33.8292,"lng":151.2441,"est":83},
    "Toorak VIC 3142": {"lat":-37.8415,"lng":145.0087,"est":78},
    "Noosa Heads QLD 4567": {"lat":-26.3907,"lng":153.0909,"est":73},
    "Nedlands WA 6009": {"lat":-31.9811,"lng":115.8053,"est":67},
    "Paddington QLD 4064": {"lat":-27.4598,"lng":153.0094,"est":62},
    "Wahroonga NSW 2076": {"lat":-33.7178,"lng":151.117,"est":59},
    "Brighton VIC 3186": {"lat":-37.9067,"lng":144.9879,"est":56},
    "Hunters Hill NSW 2110": {"lat":-33.8345,"lng":151.1437,"est":52},
    "Manly NSW 2095": {"lat":-33.7969,"lng":151.2844,"est":47},
    "Burnside SA 5066": {"lat":-34.9399,"lng":138.6586,"est":44},
    "Cronulla NSW 2230": {"lat":-34.0547,"lng":151.1518,"est":42},
    "Vaucluse NSW 2030": {"lat":-33.8579,"lng":151.2783,"est":41},
    "Peppermint Grove WA 6011": {"lat":-31.9998,"lng":115.7652,"est":38},
    "Bondi NSW 2026": {"lat":-33.8915,"lng":151.2767,"est":35},
    "Sandy Bay TAS 7005": {"lat":-42.9032,"lng":147.3364,"est":19},
}


def seeded(s):
    state = [s]
    def _next():
        state[0] = (state[0] * 16807) % 2147483647
        return (state[0] - 1) / 2147483646
    return _next


def gen_features(key: str) -> list:
    info = REGIONS[key]
    parts = key.split()
    pc = parts[-1]
    state = parts[-2]
    name = " ".join(parts[:-2])

    seed = int(pc) * 137 + len(name) * 73
    r = seeded(seed)
    items = []

    for i in range(info["est"]):
        r1, r2, r3, r4, r5 = r(), r(), r(), r(), r()
        wt = r()
        pt = "Residential" if wt < 0.625 else ("Strata" if wt < 0.875 else "Commercial")
        lat = info["lat"] + (r1 - 0.5) * 0.006
        lng = info["lng"] + (r2 - 0.5) * 0.006

        if pt == "Commercial":
            length = round(15 + r3 * 35, 1)
            width = round(8 + r4 * 15, 1)
        else:
            length = round(4.5 + r3 * 8.5, 1)
            width = round(2.2 + r4 * 4.3, 1)

        area = round(length * width, 1)
        conf = round(86 + r5 * 13.9, 1)
        st = STREETS[int(r1 * len(STREETS))]
        stt = ST_TYPES[int(r2 * len(ST_TYPES))]
        num = int(r3 * 150) + 1
        unit = f"{int(r4*24)+1}/" if pt == "Strata" else ""

        items.append({
            "id": f"f-{pc}-{i}",
            "lat": round(lat, 6), "lng": round(lng, 6),
            "address": f"{unit}{num} {st} {stt}",
            "full_address": f"{unit}{num} {st} {stt}, {name} {state} {pc}",
            "suburb": name, "state": state, "postcode": pc,
            "property_type": pt,
            "length_m": length, "width_m": width, "area_m2": area,
            "variant": VARIANTS[int(r5 * len(VARIANTS))],
            "finish": FINISHES[int(r3 * len(FINISHES))],
            "confidence": conf,
            "est_capacity": round(area * (1.8 if pt == "Commercial" else 1.4), 1),
            "year_installed": 1975 + int(r4 * 49),
            "ref_number": f"DA/{2010+int(r1*14)}/{int(1000+r2*8999)}" if r5 > 0.4 else "",
        })
    return items


def gen_refs(key: str) -> list:
    info = REGIONS.get(key)
    if not info: return []
    parts = key.split()
    name = " ".join(parts[:-2])
    state = parts[-2]

    councils = {
        "Manly":"Northern Beaches","Mosman":"Mosman","Vaucluse":"Woollahra",
        "Bondi":"Waverley","Hunters Hill":"Hunters Hill","Castle Hill":"The Hills",
        "Kellyville":"The Hills","Cronulla":"Sutherland","Wahroonga":"Ku-ring-gai",
        "Paddington":"Brisbane","Ascot":"Brisbane","Noosa Heads":"Noosa",
        "Palm Beach":"Gold Coast","Toorak":"Stonnington","Brighton":"Bayside",
        "Burnside":"Burnside","Nedlands":"Nedlands","Peppermint Grove":"Peppermint Grove",
        "Sandy Bay":"Hobart",
    }
    council = councils.get(name, f"{name} Council")
    r = seeded(hash(key) % 99999)
    refs = []
    statuses = ["Approved","Approved","Approved","Under Assessment","Lodged","Certified"]
    types = ["New Installation","Installation & Landscaping","Renovation",
             "Dual Installation","Complying Dev","Barrier Modification"]

    for i in range(min(15, info["est"] // 3)):
        r1, r2, r3 = r(), r(), r()
        yr = 2019 + int(r1 * 6)
        mn = 1 + int(r2 * 12)
        refs.append({
            "ref": f"DA/{yr}/{1000+int(r3*8999)}",
            "address": f"{int(r1*150)+1} {STREETS[int(r2*len(STREETS))]} {ST_TYPES[int(r3*len(ST_TYPES))]}",
            "suburb": name, "council": council,
            "status": statuses[int(r2 * len(statuses))],
            "type": types[int(r3 * len(types))],
            "lodged": f"{yr}-{mn:02d}-{int(r1*28)+1:02d}",
            "cost": int(round(25000 + r1 * 175000, -3)),
        })
    return refs


# ── Database ─────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS purchases (
        id TEXT PRIMARY KEY, region_key TEXT, plan TEXT DEFAULT 'pro',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS scans (
        id TEXT PRIMARY KEY, region_key TEXT, count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    conn.close()


# ── App ──────────────────────────────────────────────────────

app = FastAPI(title="PropScan", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup():
    init_db()

class ScanReq(BaseModel):
    region_key: str

class BuyReq(BaseModel):
    region_key: str
    plan: str = "pro"


@app.get("/api/regions")
async def list_regions():
    regions = []
    for key, data in sorted(REGIONS.items(), key=lambda x: -x[1]["est"]):
        parts = key.split()
        regions.append({
            "key": key, "name": " ".join(parts[:-2]),
            "state": parts[-2], "postcode": parts[-1],
            "lat": data["lat"], "lng": data["lng"], "est": data["est"],
        })
    return {"regions": regions}


@app.post("/api/scan")
async def scan(req: ScanReq):
    if req.region_key not in REGIONS:
        raise HTTPException(404, "Region not found")

    scan_id = hashlib.md5(f"{req.region_key}{time.time()}".encode()).hexdigest()[:16]
    features = gen_features(req.region_key)

    conn = get_db()
    conn.execute("INSERT INTO scans (id, region_key, count) VALUES (?,?,?)",
                 (scan_id, req.region_key, len(features)))
    row = conn.execute("SELECT id FROM purchases WHERE region_key=?",
                       (req.region_key,)).fetchone()
    conn.commit()
    conn.close()

    is_bought = row is not None
    res = sum(1 for f in features if f["property_type"] == "Residential")
    stc = sum(1 for f in features if f["property_type"] == "Strata")
    com = sum(1 for f in features if f["property_type"] == "Commercial")

    if is_bought:
        return {"scan_id": scan_id, "features": features, "purchased": True,
                "stats": {"total": len(features), "residential": res, "strata": stc, "commercial": com}}
    else:
        preview = features[:FREE_PREVIEW]
        locked = []
        for f in features[FREE_PREVIEW:]:
            locked.append({
                "id": f["id"], "lat": round(f["lat"], 3), "lng": round(f["lng"], 3),
                "property_type": f["property_type"], "area_m2": f["area_m2"],
                "address": f["address"][:4] + "██████████", "locked": True,
            })
        return {"scan_id": scan_id, "features": preview + locked, "purchased": False,
                "locked_count": len(locked),
                "stats": {"total": len(features), "residential": res, "strata": stc, "commercial": com}}


@app.post("/api/purchase")
async def purchase(req: BuyReq):
    if req.region_key not in REGIONS:
        raise HTTPException(404, "Region not found")
    pid = hashlib.md5(f"{req.region_key}{time.time()}".encode()).hexdigest()[:16]
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO purchases (id, region_key, plan) VALUES (?,?,?)",
                 (pid, req.region_key, req.plan))
    conn.commit()
    conn.close()
    features = gen_features(req.region_key)
    return {"purchase_id": pid, "unlocked": len(features), "features": features}


@app.get("/api/refs")
async def get_refs(region_key: str = ""):
    if region_key and region_key in REGIONS:
        return {"refs": gen_refs(region_key)}
    all_refs = []
    for key in list(REGIONS.keys())[:5]:
        all_refs.extend(gen_refs(key))
    return {"refs": all_refs}


@app.get("/api/export")
async def export_csv(region_key: str):
    if region_key not in REGIONS:
        raise HTTPException(404, "Region not found")
    features = gen_features(region_key)
    lines = ["ID,Address,Suburb,State,Postcode,Type,Length_m,Width_m,Area_m2,Variant,Finish,Capacity,Confidence,Ref,Year,Lat,Lng"]
    for f in features:
        lines.append(f'{f["id"]},"{f["full_address"]}",{f["suburb"]},{f["state"]},{f["postcode"]},'
                     f'{f["property_type"]},{f["length_m"]},{f["width_m"]},{f["area_m2"]},'
                     f'{f["variant"]},{f["finish"]},{f["est_capacity"]},{f["confidence"]},'
                     f'{f["ref_number"]},{f["year_installed"]},{f["lat"]},{f["lng"]}')
    parts = region_key.split()
    fn = f"propscan_{'_'.join(parts)}.csv"
    return StreamingResponse(iter(["\n".join(lines)]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={fn}"})


@app.get("/api/stats")
async def stats():
    conn = get_db()
    sc = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    pr = conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
    conn.close()
    total = sum(v["est"] for v in REGIONS.values())
    return {"regions": len(REGIONS), "total_est": total, "scans": sc, "purchases": pr}


@app.get("/", response_class=HTMLResponse)
async def index():
    p = Path("static/index.html")
    if p.exists():
        return HTMLResponse(p.read_text())
    return HTMLResponse("<html><body><h1>PropScan</h1><p>Missing static/index.html</p></body></html>")
