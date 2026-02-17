"""
PropScan v3 — with integrated live scanning from admin dashboard
"""
import hashlib, json, math, os, secrets, sqlite3, time, threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Cookie
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "propscan.db"
FREE_PREVIEW = 5
ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme123")

DEFAULT_COUPONS = {
    "BETA2025": {"plan": "pro", "max_uses": 50, "discount_pct": 100},
    "LAUNCH10": {"plan": "pro", "max_uses": 100, "discount_pct": 100},
}

STREETS = ["Bower","Pacific","Stuart","Quinton","Darley","Wentworth","Ocean","Marine","Victoria","Albert","George","King","Queen","Park","Beach","Bay","Lake","Hill","Valley","Cedar","Pine","Oak","Elm","Palm","Rose","Banksia","Waratah","Grevillea","Acacia","Kurrajong","Jacaranda","Eucalyptus","Hibiscus","Jasmine","Magnolia","Camellia"]
ST_TYPES = ["St","Rd","Ave","Dr","Pde","Cres","Ct","Pl","Tce","Way","Ln","Cl"]
VARIANTS = ["Rectangular","Kidney","Freeform","Lap","L-Shaped","Oval","Compact","Extended"]
FINISHES = ["Type-A","Type-B","Type-C","Type-D","Type-E"]

REGIONS = {
    "Kellyville NSW 2155":{"lat":-33.709,"lng":150.956,"est":110},
    "Castle Hill NSW 2154":{"lat":-33.731,"lng":151.005,"est":95},
    "Ascot QLD 4007":{"lat":-27.4326,"lng":153.0597,"est":91},
    "Palm Beach QLD 4221":{"lat":-28.1118,"lng":153.4637,"est":88},
    "Mosman NSW 2088":{"lat":-33.8292,"lng":151.2441,"est":83},
    "Toorak VIC 3142":{"lat":-37.8415,"lng":145.0087,"est":78},
    "Noosa Heads QLD 4567":{"lat":-26.3907,"lng":153.0909,"est":73},
    "Nedlands WA 6009":{"lat":-31.9811,"lng":115.8053,"est":67},
    "Paddington QLD 4064":{"lat":-27.4598,"lng":153.0094,"est":62},
    "Wahroonga NSW 2076":{"lat":-33.7178,"lng":151.117,"est":59},
    "Brighton VIC 3186":{"lat":-37.9067,"lng":144.9879,"est":56},
    "Hunters Hill NSW 2110":{"lat":-33.8345,"lng":151.1437,"est":52},
    "Manly NSW 2095":{"lat":-33.7969,"lng":151.2844,"est":47},
    "Burnside SA 5066":{"lat":-34.9399,"lng":138.6586,"est":44},
    "Cronulla NSW 2230":{"lat":-34.0547,"lng":151.1518,"est":42},
    "Vaucluse NSW 2030":{"lat":-33.8579,"lng":151.2783,"est":41},
    "Peppermint Grove WA 6011":{"lat":-31.9998,"lng":115.7652,"est":38},
    "Bondi NSW 2026":{"lat":-33.8915,"lng":151.2767,"est":35},
    "Sandy Bay TAS 7005":{"lat":-42.9032,"lng":147.3364,"est":19},
}

# ── Scan job tracking ────────────────────────────────────────
# In-memory job queue (simple for single-worker Render free tier)
scan_jobs = {}  # job_id → {status, region_key, progress, result, started, finished}

def run_scan_job(job_id, region_key):
    """Background scan — runs in a thread."""
    try:
        from scanner import scan_region
        info = REGIONS[region_key]
        parts = region_key.split()
        pc, state, name = parts[-1], parts[-2], " ".join(parts[:-2])

        scan_jobs[job_id]["status"] = "fetching"
        scan_jobs[job_id]["message"] = "Fetching satellite tiles..."

        result = scan_region(info["lat"], info["lng"], name, state, pc)

        if result.get("error"):
            scan_jobs[job_id]["status"] = "error"
            scan_jobs[job_id]["message"] = result["error"]
            return

        scan_jobs[job_id]["status"] = "saving"
        scan_jobs[job_id]["message"] = f"Saving {len(result['features'])} features..."

        # Save to DB
        conn = get_db()
        scan_id = result["scan_id"]
        for f in result["features"]:
            conn.execute("""INSERT OR REPLACE INTO features
                (id,lat,lng,suburb,state,postcode,property_type,length_m,width_m,area_m2,
                 variant,est_capacity,confidence,scan_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f["id"], f["lat"], f["lng"], name, state, pc, f["property_type"],
                 f["length_m"], f["width_m"], f["area_m2"], f["variant"],
                 f["est_capacity"], f["confidence"], scan_id))

        conn.execute("""INSERT INTO scans
            (id,region_key,center_lat,center_lng,features_found,tiles_fetched,tiles_total,duration_sec)
            VALUES (?,?,?,?,?,?,?,?)""",
            (scan_id, region_key, info["lat"], info["lng"], len(result["features"]),
             result["tiles_fetched"], result["tiles_total"], result["duration"]))
        conn.commit()
        conn.close()

        scan_jobs[job_id]["status"] = "done"
        scan_jobs[job_id]["message"] = f"Found {len(result['features'])} features in {result['duration']}s"
        scan_jobs[job_id]["result"] = {
            "features_found": len(result["features"]),
            "tiles": f"{result['tiles_fetched']}/{result['tiles_total']}",
            "duration": result["duration"],
        }
        scan_jobs[job_id]["finished"] = time.time()

    except Exception as e:
        scan_jobs[job_id]["status"] = "error"
        scan_jobs[job_id]["message"] = str(e)


# ── Helpers ──────────────────────────────────────────────────

def seeded(s):
    state = [s]
    def _next():
        state[0] = (state[0] * 16807) % 2147483647
        return (state[0] - 1) / 2147483646
    return _next

def gen_features(key):
    info = REGIONS[key]; parts = key.split()
    pc, state, name = parts[-1], parts[-2], " ".join(parts[:-2])
    seed = int(pc)*137 + len(name)*73; r = seeded(seed); items = []
    for i in range(info["est"]):
        r1,r2,r3,r4,r5 = r(),r(),r(),r(),r(); wt = r()
        pt = "Residential" if wt<0.625 else ("Strata" if wt<0.875 else "Commercial")
        lat = info["lat"]+(r1-0.5)*0.006; lng = info["lng"]+(r2-0.5)*0.006
        if pt=="Commercial": ln,wd = round(15+r3*35,1), round(8+r4*15,1)
        else: ln,wd = round(4.5+r3*8.5,1), round(2.2+r4*4.3,1)
        area = round(ln*wd,1); conf = round(86+r5*13.9,1)
        st = STREETS[int(r1*len(STREETS))]; stt = ST_TYPES[int(r2*len(ST_TYPES))]
        num = int(r3*150)+1; unit = f"{int(r4*24)+1}/" if pt=="Strata" else ""
        items.append({
            "id":f"f-{pc}-{i}","lat":round(lat,6),"lng":round(lng,6),
            "address":f"{unit}{num} {st} {stt}",
            "full_address":f"{unit}{num} {st} {stt}, {name} {state} {pc}",
            "suburb":name,"state":state,"postcode":pc,"property_type":pt,
            "length_m":ln,"width_m":wd,"area_m2":area,
            "variant":VARIANTS[int(r5*len(VARIANTS))],"finish":FINISHES[int(r3*len(FINISHES))],
            "confidence":conf,"est_capacity":round(area*(1.8 if pt=="Commercial" else 1.4),1),
            "year_installed":1975+int(r4*49),
            "ref_number":f"DA/{2010+int(r1*14)}/{int(1000+r2*8999)}" if r5>0.4 else "",
        })
    return items

def get_real_features(suburb, state):
    conn = get_db()
    rows = conn.execute("SELECT * FROM features WHERE suburb=? AND state=? ORDER BY confidence DESC", (suburb, state)).fetchall()
    conn.close()
    return [dict(r) for r in rows] if rows else None

def get_features(key):
    parts = key.split()
    name, state = " ".join(parts[:-2]), parts[-2]
    real = get_real_features(name, state)
    if real: return real, True
    return gen_features(key), False


# ── Database ─────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS features (
        id TEXT PRIMARY KEY, lat REAL, lng REAL,
        address TEXT DEFAULT '', suburb TEXT, state TEXT, postcode TEXT,
        property_type TEXT, length_m REAL, width_m REAL, area_m2 REAL,
        variant TEXT, finish TEXT DEFAULT 'Unknown',
        est_capacity REAL, confidence REAL,
        ref_number TEXT DEFAULT '', year_installed INTEGER DEFAULT 0,
        scan_id TEXT, created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_feat_sub ON features(suburb, state);
    CREATE TABLE IF NOT EXISTS scans (
        id TEXT PRIMARY KEY, region_key TEXT,
        center_lat REAL, center_lng REAL,
        features_found INTEGER DEFAULT 0, tiles_fetched INTEGER DEFAULT 0,
        tiles_total INTEGER DEFAULT 0, duration_sec REAL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS purchases (
        id TEXT PRIMARY KEY, region_key TEXT, plan TEXT DEFAULT 'pro',
        coupon TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS coupons (
        code TEXT PRIMARY KEY, plan TEXT DEFAULT 'pro',
        max_uses INTEGER DEFAULT 50, times_used INTEGER DEFAULT 0,
        discount_pct INTEGER DEFAULT 100, active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS admin_sessions (
        token TEXT PRIMARY KEY, created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    for code, info in DEFAULT_COUPONS.items():
        conn.execute("INSERT OR IGNORE INTO coupons (code, plan, max_uses, discount_pct) VALUES (?,?,?,?)",
                     (code, info["plan"], info["max_uses"], info["discount_pct"]))
    conn.commit(); conn.close()


# ── App ──────────────────────────────────────────────────────

app = FastAPI(title="PropScan", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup():
    init_db()

def check_admin(token):
    if not token: return False
    conn = get_db()
    row = conn.execute("SELECT token FROM admin_sessions WHERE token=?", (token,)).fetchone()
    conn.close()
    return row is not None

class ScanReq(BaseModel):
    region_key: str
class RedeemReq(BaseModel):
    region_key: str
    coupon: str


# ── Public API ───────────────────────────────────────────────

@app.get("/api/regions")
async def list_regions():
    conn = get_db()
    regions = []
    for key, data in sorted(REGIONS.items(), key=lambda x: -x[1]["est"]):
        parts = key.split()
        name, state, pc = " ".join(parts[:-2]), parts[-2], parts[-1]
        real_count = conn.execute("SELECT COUNT(*) FROM features WHERE suburb=? AND state=?", (name, state)).fetchone()[0]
        regions.append({"key":key,"name":name,"state":state,"postcode":pc,
                        "lat":data["lat"],"lng":data["lng"],"est":data["est"],
                        "scanned":real_count>0,"real_count":real_count})
    conn.close()
    return {"regions": regions}

@app.post("/api/scan")
async def scan(req: ScanReq):
    if req.region_key not in REGIONS: raise HTTPException(404)
    features, is_real = get_features(req.region_key)
    scan_id = hashlib.md5(f"{req.region_key}{time.time()}".encode()).hexdigest()[:16]
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO scans (id, region_key, features_found) VALUES (?,?,?)",
                 (scan_id, req.region_key, len(features)))
    row = conn.execute("SELECT id FROM purchases WHERE region_key=?", (req.region_key,)).fetchone()
    conn.commit(); conn.close()
    is_bought = row is not None
    res = sum(1 for f in features if f.get("property_type")=="Residential")
    stc = sum(1 for f in features if f.get("property_type")=="Strata")
    com = sum(1 for f in features if f.get("property_type")=="Commercial")
    stats = {"total":len(features),"residential":res,"strata":stc,"commercial":com}
    if is_bought:
        return {"scan_id":scan_id,"features":features,"purchased":True,"is_real":is_real,"stats":stats}
    preview = features[:FREE_PREVIEW]
    locked = [{"id":f.get("id",""),"lat":round(f.get("lat",0),3),"lng":round(f.get("lng",0),3),
                "property_type":f.get("property_type",""),"area_m2":f.get("area_m2",0),
                "address":(f.get("address","")[:4] if f.get("address") else "")+"██████████","locked":True}
               for f in features[FREE_PREVIEW:]]
    return {"scan_id":scan_id,"features":preview+locked,"purchased":False,
            "locked_count":len(locked),"is_real":is_real,"stats":stats}

@app.post("/api/redeem")
async def redeem(req: RedeemReq):
    if req.region_key not in REGIONS: raise HTTPException(404)
    code = req.coupon.strip().upper()
    conn = get_db()
    row = conn.execute("SELECT * FROM coupons WHERE code=? AND active=1", (code,)).fetchone()
    if not row: conn.close(); raise HTTPException(400, "Invalid or expired code")
    if row["times_used"] >= row["max_uses"]: conn.close(); raise HTTPException(400, "Code fully redeemed")
    pid = hashlib.md5(f"{req.region_key}{time.time()}".encode()).hexdigest()[:16]
    conn.execute("INSERT OR IGNORE INTO purchases (id, region_key, plan, coupon) VALUES (?,?,?,?)",
                 (pid, req.region_key, row["plan"], code))
    conn.execute("UPDATE coupons SET times_used=times_used+1 WHERE code=?", (code,))
    conn.commit(); conn.close()
    features, is_real = get_features(req.region_key)
    return {"purchase_id":pid,"unlocked":len(features),"features":features,"is_real":is_real}

@app.get("/api/export")
async def export_csv(region_key: str):
    if region_key not in REGIONS: raise HTTPException(404)
    features, _ = get_features(region_key)
    hdr = "ID,Address,Suburb,State,Postcode,Type,Length_m,Width_m,Area_m2,Variant,Finish,Capacity,Confidence,Ref,Year,Lat,Lng"
    lines = [hdr] + [f'{f.get("id","")},"{f.get("full_address",f.get("address",""))}",{f.get("suburb","")},{f.get("state","")},{f.get("postcode","")},'
        f'{f.get("property_type","")},{f.get("length_m","")},{f.get("width_m","")},{f.get("area_m2","")},'
        f'{f.get("variant","")},{f.get("finish","")},{f.get("est_capacity","")},{f.get("confidence","")},'
        f'{f.get("ref_number","")},{f.get("year_installed","")},{f.get("lat","")},{f.get("lng","")}' for f in features]
    fn = f"propscan_{'_'.join(region_key.split())}.csv"
    return StreamingResponse(iter(["\n".join(lines)]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={fn}"})

@app.get("/api/stats")
async def stats():
    conn = get_db()
    sc = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    pr = conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
    rf = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    conn.close()
    return {"regions":len(REGIONS),"total_est":sum(v["est"] for v in REGIONS.values()),"scans":sc,"purchases":pr,"real_features_in_db":rf}


# ── Admin Routes ─────────────────────────────────────────────

@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.json()
    if form.get("password") != ADMIN_PASS: raise HTTPException(401)
    token = secrets.token_hex(32)
    conn = get_db()
    conn.execute("INSERT INTO admin_sessions (token) VALUES (?)", (token,))
    conn.commit(); conn.close()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ps_admin", token, httponly=True, max_age=86400*7)
    return resp

@app.get("/admin/data")
async def admin_data(ps_admin: str = Cookie(None)):
    if not check_admin(ps_admin): raise HTTPException(401)
    conn = get_db()
    data = {
        "stats": {
            "regions": len(REGIONS),
            "real_features": conn.execute("SELECT COUNT(*) FROM features").fetchone()[0],
            "scans": conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0],
            "purchases": conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0],
        },
        "coupons": [dict(r) for r in conn.execute("SELECT * FROM coupons ORDER BY created_at DESC").fetchall()],
        "purchases": [dict(r) for r in conn.execute("SELECT * FROM purchases ORDER BY created_at DESC LIMIT 50").fetchall()],
        "regions_scanned": [dict(r) for r in conn.execute(
            "SELECT suburb, state, COUNT(*) as count, ROUND(AVG(area_m2),1) as avg_area, "
            "ROUND(AVG(confidence),1) as avg_conf FROM features GROUP BY suburb, state ORDER BY count DESC"
        ).fetchall()],
        "active_jobs": {jid: {k:v for k,v in j.items() if k!="result"} for jid, j in scan_jobs.items() if j["status"] in ("pending","fetching","saving")},
    }
    conn.close()
    return data

@app.post("/admin/coupon")
async def admin_coupon(request: Request, ps_admin: str = Cookie(None)):
    if not check_admin(ps_admin): raise HTTPException(401)
    form = await request.json()
    action = form.get("action")
    conn = get_db()
    if action == "create":
        code = form.get("code","").strip().upper()
        if not code: code = secrets.token_hex(4).upper()
        conn.execute("INSERT OR IGNORE INTO coupons (code, plan, max_uses, discount_pct) VALUES (?,?,?,?)",
                     (code, form.get("plan","pro"), form.get("max_uses",50), form.get("discount_pct",100)))
    elif action == "delete":
        conn.execute("DELETE FROM coupons WHERE code=?", (form.get("code",""),))
    elif action == "toggle":
        conn.execute("UPDATE coupons SET active = NOT active WHERE code=?", (form.get("code",""),))
    conn.commit(); conn.close()
    return {"ok": True}

@app.post("/admin/upload-db")
async def admin_upload_db(request: Request, ps_admin: str = Cookie(None)):
    if not check_admin(ps_admin): raise HTTPException(401)
    body = await request.body()
    tmp = DATA_DIR / "upload_tmp.db"
    tmp.write_bytes(body)
    try:
        src = sqlite3.connect(str(tmp)); src.row_factory = sqlite3.Row
        rows = src.execute("SELECT * FROM features").fetchall(); src.close()
        conn = get_db(); imported = 0
        for r in rows:
            conn.execute("""INSERT OR REPLACE INTO features
                (id,lat,lng,address,suburb,state,postcode,property_type,
                 length_m,width_m,area_m2,variant,finish,est_capacity,
                 confidence,ref_number,year_installed,scan_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["id"],r["lat"],r["lng"],r.get("address",""),r["suburb"],r["state"],
                 r.get("postcode",""),r["property_type"],r["length_m"],r["width_m"],
                 r["area_m2"],r.get("variant",""),r.get("finish","Unknown"),
                 r.get("est_capacity",0),r["confidence"],r.get("ref_number",""),
                 r.get("year_installed",0),r.get("scan_id","")))
            imported += 1
        conn.commit(); conn.close(); tmp.unlink(missing_ok=True)
        return {"ok": True, "imported": imported}
    except Exception as e:
        tmp.unlink(missing_ok=True); raise HTTPException(400, f"Invalid database: {e}")


# ── Admin Live Scan ──────────────────────────────────────────

@app.post("/admin/scan")
async def admin_trigger_scan(request: Request, ps_admin: str = Cookie(None)):
    """Trigger a real satellite scan for a region — runs in background."""
    if not check_admin(ps_admin): raise HTTPException(401)
    form = await request.json()
    region_key = form.get("region_key", "")
    if region_key not in REGIONS: raise HTTPException(404, "Region not found")

    # Check if already scanning
    for jid, j in scan_jobs.items():
        if j["region_key"] == region_key and j["status"] in ("pending", "fetching", "saving"):
            return {"job_id": jid, "status": "already_running"}

    job_id = secrets.token_hex(8)
    scan_jobs[job_id] = {
        "status": "pending", "region_key": region_key,
        "message": "Starting...", "result": None,
        "started": time.time(), "finished": None,
    }

    t = threading.Thread(target=run_scan_job, args=(job_id, region_key), daemon=True)
    t.start()

    return {"job_id": job_id, "status": "started"}

@app.get("/admin/scan-status/{job_id}")
async def admin_scan_status(job_id: str, ps_admin: str = Cookie(None)):
    if not check_admin(ps_admin): raise HTTPException(401)
    if job_id not in scan_jobs: raise HTTPException(404)
    return scan_jobs[job_id]

@app.post("/admin/scan-all")
async def admin_scan_all(request: Request, ps_admin: str = Cookie(None)):
    """Queue scans for all unscanned regions."""
    if not check_admin(ps_admin): raise HTTPException(401)
    conn = get_db()
    queued = []
    for key in REGIONS:
        parts = key.split()
        name, state = " ".join(parts[:-2]), parts[-2]
        count = conn.execute("SELECT COUNT(*) FROM features WHERE suburb=? AND state=?", (name, state)).fetchone()[0]
        if count == 0:  # Not yet scanned
            already = any(j["region_key"] == key and j["status"] in ("pending","fetching","saving") for j in scan_jobs.values())
            if not already:
                job_id = secrets.token_hex(8)
                scan_jobs[job_id] = {
                    "status": "pending", "region_key": key,
                    "message": "Queued...", "result": None,
                    "started": time.time(), "finished": None,
                }
                # Stagger starts to avoid overwhelming ESRI
                delay = len(queued) * 5
                t = threading.Timer(delay, run_scan_job, args=(job_id, key))
                t.daemon = True
                t.start()
                queued.append(key)
    conn.close()
    return {"queued": len(queued), "regions": queued}


# ── Admin Dashboard HTML ─────────────────────────────────────

ADMIN_HTML_PATH = Path("static/admin.html")

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    if ADMIN_HTML_PATH.exists():
        return HTMLResponse(ADMIN_HTML_PATH.read_text())
    return HTMLResponse(ADMIN_HTML)


ADMIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PropScan Admin</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui;background:#0a0e1a;color:#e8ecf4;padding:16px;max-width:960px;margin:0 auto}
h1{font-size:1.3rem;margin-bottom:16px;color:#06d6a0}h2{font-size:1rem;margin:20px 0 8px;color:#4fc3f7}
.card{background:#111827;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:16px;margin-bottom:12px}
input,button,select{font-family:inherit;font-size:.85rem}
input{padding:8px 12px;background:#1a2035;border:1px solid rgba(255,255,255,.1);border-radius:6px;color:#e8ecf4;width:100%;margin-bottom:8px}
select{padding:8px 12px;background:#1a2035;border:1px solid rgba(255,255,255,.1);border-radius:6px;color:#e8ecf4}
button{padding:8px 16px;border-radius:6px;border:none;cursor:pointer;font-weight:600}
.bp{background:#06d6a0;color:#070b14}.bd{background:#ef4444;color:#fff;font-size:.75rem;padding:4px 10px}
.bs{background:transparent;border:1px solid rgba(255,255,255,.1);color:#e8ecf4}
.bw{background:#f59e0b;color:#070b14}
table{width:100%;border-collapse:collapse;font-size:.8rem;margin-top:8px}
th{text-align:left;color:#5a6478;padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.06)}
td{padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.03)}
.stat{display:inline-block;text-align:center;padding:10px 16px;margin:4px}
.stat .n{font-size:1.5rem;font-weight:700;font-family:monospace}.stat .l{font-size:.65rem;color:#5a6478}
.tag{font-size:.65rem;padding:2px 6px;border-radius:3px;font-weight:500}
.tag-ok{background:rgba(6,214,160,.15);color:#06d6a0}.tag-off{background:rgba(239,68,68,.15);color:#ef4444}
.tag-real{background:rgba(79,195,247,.15);color:#4fc3f7}.tag-gen{background:rgba(255,209,102,.15);color:#ffd166}
.tag-run{background:rgba(245,158,11,.15);color:#f59e0b}
.hidden{display:none}#login{max-width:320px;margin:100px auto}
.prog{height:4px;background:#1a2035;border-radius:2px;margin-top:4px;overflow:hidden}
.prog-bar{height:100%;background:#06d6a0;transition:width .3s}
</style></head><body>
<div id="login"><div class="card">
<h1>PropScan Admin</h1>
<input id="pw" type="password" placeholder="Admin password">
<button class="bp" onclick="login()" style="width:100%">Login</button>
</div></div>

<div id="dash" class="hidden">
<h1>PropScan Admin</h1>
<div id="stats" class="card"></div>

<h2>Satellite Scanner</h2>
<div class="card">
<p style="font-size:.8rem;color:#8a94a8;margin-bottom:10px">Trigger real satellite scans directly from here. Fetches ESRI imagery and runs feature detection.</p>
<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap">
<select id="scan-region" style="flex:1;min-width:200px"></select>
<button class="bp" onclick="triggerScan()">Scan Region</button>
<button class="bw" onclick="scanAll()">Scan All Unscanned</button>
</div>
<div id="scan-status"></div>
</div>

<h2>Coupon Codes</h2>
<div class="card">
<div style="display:flex;gap:8px;margin-bottom:8px">
<input id="nc" placeholder="Code (blank=auto)" style="flex:1;margin:0">
<input id="nm" placeholder="Max uses" value="50" style="width:80px;margin:0">
<button class="bp" onclick="addCoupon()">Add</button>
</div>
<table><thead><tr><th>Code</th><th>Uses</th><th>Status</th><th></th></tr></thead><tbody id="ctbl"></tbody></table>
</div>

<h2>Scanned Regions (Real Data)</h2>
<div class="card"><table><thead><tr><th>Region</th><th>Features</th><th>Avg Area</th><th>Avg Conf</th></tr></thead><tbody id="rtbl"></tbody></table></div>

<h2>Upload Scanner Database</h2>
<div class="card">
<p style="font-size:.8rem;color:#8a94a8;margin-bottom:8px">Optional: upload a .db from Google Colab to import features.</p>
<input type="file" id="dbfile" accept=".db,.sqlite,.sqlite3" style="background:none;border:none;padding:0">
<button class="bp" onclick="uploadDB()" style="margin-top:8px">Upload & Import</button>
<div id="upload-status" style="font-size:.8rem;margin-top:6px"></div>
</div>

<h2>Recent Purchases</h2>
<div class="card"><table><thead><tr><th>Region</th><th>Plan</th><th>Coupon</th><th>Date</th></tr></thead><tbody id="ptbl"></tbody></table></div>
</div>

<script>
const API=window.location.origin; let pollTimer=null;

async function login(){
  const r=await fetch(API+'/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});
  if(r.ok){document.getElementById('login').classList.add('hidden');document.getElementById('dash').classList.remove('hidden');loadData()}
  else alert('Wrong password')}

async function loadData(){
  const r=await fetch(API+'/admin/data',{credentials:'include'});
  if(!r.ok){alert('Session expired');location.reload();return}
  const d=await r.json();const s=d.stats;

  document.getElementById('stats').innerHTML=
    [{n:s.regions,l:'Regions',c:'#06d6a0'},{n:s.real_features,l:'Real Features',c:'#4fc3f7'},
     {n:s.scans,l:'Scans',c:'#ffd166'},{n:s.purchases,l:'Purchases',c:'#ef8354'}]
    .map(x=>`<div class="stat"><div class="n" style="color:${x.c}">${x.n}</div><div class="l">${x.l}</div></div>`).join('');

  // Populate scanner dropdown
  const sel=document.getElementById('scan-region');
  const rr=await fetch(API+'/api/regions');const rd=await rr.json();
  sel.innerHTML=rd.regions.map(r2=>`<option value="${r2.key}">${r2.name}, ${r2.state} ${r2.postcode} ${r2.scanned?'✓':''} (~${r2.est})</option>`).join('');

  // Active jobs
  const jobs=d.active_jobs||{};
  if(Object.keys(jobs).length>0){
    let jh='';
    for(const[jid,j] of Object.entries(jobs)){
      jh+=`<div style="margin:4px 0;font-size:.82rem"><span class="tag tag-run">${j.status}</span> ${j.region_key} — ${j.message}</div>`;
    }
    document.getElementById('scan-status').innerHTML=jh;
    if(!pollTimer) pollTimer=setInterval(loadData,3000);
  } else {
    if(pollTimer){clearInterval(pollTimer);pollTimer=null}
  }

  document.getElementById('ctbl').innerHTML=d.coupons.map(c=>`<tr><td><code>${c.code}</code></td><td>${c.times_used}/${c.max_uses}</td><td><span class="tag ${c.active?'tag-ok':'tag-off'}">${c.active?'Active':'Off'}</span></td><td><button class="bs" onclick="toggleCoupon('${c.code}')" style="font-size:.7rem;padding:2px 8px">Toggle</button> <button class="bd" onclick="delCoupon('${c.code}')">x</button></td></tr>`).join('')||'<tr><td colspan=4>No coupons</td></tr>';

  document.getElementById('rtbl').innerHTML=d.regions_scanned.map(r2=>`<tr><td>${r2.suburb}, ${r2.state} <span class="tag tag-real">Real</span></td><td>${r2.count}</td><td>${r2.avg_area}m2</td><td>${r2.avg_conf}%</td></tr>`).join('')||'<tr><td colspan=4>No real data yet — use the scanner above</td></tr>';

  document.getElementById('ptbl').innerHTML=d.purchases.map(p=>`<tr><td>${p.region_key}</td><td>${p.plan}</td><td>${p.coupon||'—'}</td><td>${p.created_at}</td></tr>`).join('')||'<tr><td colspan=4>None</td></tr>';
}

async function triggerScan(){
  const key=document.getElementById('scan-region').value;
  if(!key)return;
  document.getElementById('scan-status').innerHTML='<div style="font-size:.82rem"><span class="tag tag-run">Starting</span> '+key+'...</div>';
  const r=await fetch(API+'/admin/scan',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({region_key:key})});
  const d=await r.json();
  if(d.status==='already_running'){document.getElementById('scan-status').innerHTML='<div style="font-size:.82rem"><span class="tag tag-run">Already running</span></div>'}
  else{document.getElementById('scan-status').innerHTML='<div style="font-size:.82rem"><span class="tag tag-run">Started</span> '+key+' — polling...</div>';
    if(!pollTimer) pollTimer=setInterval(loadData,3000);}
}

async function scanAll(){
  if(!confirm('Scan all unscanned regions? This may take 10-20 minutes.'))return;
  const r=await fetch(API+'/admin/scan-all',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  const d=await r.json();
  document.getElementById('scan-status').innerHTML=`<div style="font-size:.82rem"><span class="tag tag-run">Queued</span> ${d.queued} regions</div>`;
  if(!pollTimer) pollTimer=setInterval(loadData,3000);
}

async function addCoupon(){
  await fetch(API+'/admin/coupon',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'create',code:document.getElementById('nc').value,max_uses:parseInt(document.getElementById('nm').value)||50})});
  document.getElementById('nc').value='';loadData()}
async function delCoupon(code){if(!confirm('Delete '+code+'?'))return;await fetch(API+'/admin/coupon',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'delete',code})});loadData()}
async function toggleCoupon(code){await fetch(API+'/admin/coupon',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'toggle',code})});loadData()}

async function uploadDB(){
  const f=document.getElementById('dbfile').files[0];if(!f){alert('Select a .db file');return}
  document.getElementById('upload-status').textContent='Uploading...';
  const buf=await f.arrayBuffer();
  const r=await fetch(API+'/admin/upload-db',{method:'POST',credentials:'include',headers:{'Content-Type':'application/octet-stream'},body:buf});
  const d=await r.json();
  document.getElementById('upload-status').textContent=r.ok?'Imported '+d.imported+' features':'Error: '+(d.detail||'');
  if(r.ok)loadData()}

document.getElementById('pw').addEventListener('keyup',e=>{if(e.key==='Enter')login()});
</script></body></html>"""


# ── Frontend ─────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    p = Path("static/index.html")
    if p.exists(): return HTMLResponse(p.read_text())
    return HTMLResponse("<html><body><h1>PropScan</h1><p>Missing static/index.html</p></body></html>")
