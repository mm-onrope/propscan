"""
PropScan v4 — Dynamic location search + radius scanning + customer auth
"""
import hashlib, json, math, os, secrets, sqlite3, time, threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Cookie, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "propscan.db"
FREE_PREVIEW = 5
ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme123")
SCAN_ANIMATION_SEC = 6  # Minimum time the frontend should show scan animation

DEFAULT_COUPONS = {
    "BETA2025": {"plan": "pro", "max_uses": 50, "discount_pct": 100},
    "LAUNCH10": {"plan": "pro", "max_uses": 100, "discount_pct": 100},
}

# Radius options (km) → tiles radius at zoom 18
RADIUS_OPTIONS = {
    1: {"tiles": 2, "label": "1 km", "area_km2": 3.1},
    2: {"tiles": 3, "label": "2 km", "area_km2": 12.6},
    5: {"tiles": 5, "label": "5 km", "area_km2": 78.5},
    10: {"tiles": 7, "label": "10 km", "area_km2": 314},
}

# Featured regions for quick-select
FEATURED = {
    "Kellyville NSW 2155": (-33.709, 150.956),
    "Castle Hill NSW 2154": (-33.731, 151.005),
    "Ascot QLD 4007": (-27.4326, 153.0597),
    "Palm Beach QLD 4221": (-28.1118, 153.4637),
    "Mosman NSW 2088": (-33.8292, 151.2441),
    "Toorak VIC 3142": (-37.8415, 145.0087),
    "Noosa Heads QLD 4567": (-26.3907, 153.0909),
    "Nedlands WA 6009": (-31.9811, 115.8053),
    "Paddington QLD 4064": (-27.4598, 153.0094),
    "Wahroonga NSW 2076": (-33.7178, 151.117),
    "Brighton VIC 3186": (-37.9067, 144.9879),
    "Hunters Hill NSW 2110": (-33.8345, 151.1437),
    "Manly NSW 2095": (-33.7969, 151.2844),
    "Burnside SA 5066": (-34.9399, 138.6586),
    "Cronulla NSW 2230": (-34.0547, 151.1518),
    "Vaucluse NSW 2030": (-33.8579, 151.2783),
    "Peppermint Grove WA 6011": (-31.9998, 115.7652),
    "Bondi NSW 2026": (-33.8915, 151.2767),
    "Sandy Bay TAS 7005": (-42.9032, 147.3364),
}

STREETS = ["Bower","Pacific","Stuart","Quinton","Darley","Wentworth","Ocean","Marine","Victoria","Albert","George","King","Queen","Park","Beach","Bay","Lake","Hill","Valley","Cedar","Pine","Oak","Elm","Palm","Rose","Banksia","Waratah","Grevillea","Acacia","Kurrajong","Jacaranda","Eucalyptus","Hibiscus","Jasmine","Magnolia","Camellia"]
ST_TYPES = ["St","Rd","Ave","Dr","Pde","Cres","Ct","Pl","Tce","Way","Ln","Cl"]
VARIANTS = ["Rectangular","Kidney","Freeform","Lap","L-Shaped","Oval","Compact","Extended"]
FINISHES = ["Type-A","Type-B","Type-C","Type-D","Type-E"]

# ── Scan jobs ────────────────────────────────────────────────
scan_jobs = {}

def run_scan_job(job_id, lat, lng, suburb, state, postcode, radius_km):
    try:
        from scanner import scan_region as do_scan
        scan_jobs[job_id]["status"] = "fetching"
        scan_jobs[job_id]["message"] = "Acquiring satellite imagery..."

        result = do_scan(lat, lng, suburb, state, postcode)

        if result.get("error"):
            scan_jobs[job_id]["status"] = "error"
            scan_jobs[job_id]["message"] = result["error"]
            return

        scan_jobs[job_id]["status"] = "saving"
        scan_jobs[job_id]["message"] = f"Indexing {len(result['features'])} features..."

        conn = get_db()
        scan_id = result["scan_id"]
        for f in result["features"]:
            conn.execute("""INSERT OR REPLACE INTO features
                (id,lat,lng,suburb,state,postcode,property_type,length_m,width_m,area_m2,
                 variant,est_capacity,confidence,scan_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f["id"], f["lat"], f["lng"], suburb, state, postcode, f["property_type"],
                 f["length_m"], f["width_m"], f["area_m2"], f["variant"],
                 f["est_capacity"], f["confidence"], scan_id))

        region_key = f"{suburb} {state} {postcode}"
        conn.execute("""INSERT INTO scans
            (id,region_key,center_lat,center_lng,features_found,tiles_fetched,tiles_total,duration_sec)
            VALUES (?,?,?,?,?,?,?,?)""",
            (scan_id, region_key, lat, lng, len(result["features"]),
             result["tiles_fetched"], result["tiles_total"], result["duration"]))
        conn.commit(); conn.close()

        scan_jobs[job_id]["status"] = "done"
        scan_jobs[job_id]["result"] = {
            "features_found": len(result["features"]),
            "scan_id": scan_id,
            "duration": result["duration"],
        }
        scan_jobs[job_id]["message"] = f"Found {len(result['features'])} features in {result['duration']}s"
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

def gen_features(lat, lng, suburb, state, postcode, count=50):
    seed = hash(f"{suburb}{state}{postcode}") % 2147483647
    r = seeded(seed if seed > 0 else 12345)
    items = []
    for i in range(count):
        r1,r2,r3,r4,r5 = r(),r(),r(),r(),r(); wt = r()
        pt = "Residential" if wt<0.625 else ("Strata" if wt<0.875 else "Commercial")
        flat = lat+(r1-0.5)*0.006; flng = lng+(r2-0.5)*0.006
        if pt=="Commercial": ln,wd = round(15+r3*35,1), round(8+r4*15,1)
        else: ln,wd = round(4.5+r3*8.5,1), round(2.2+r4*4.3,1)
        area = round(ln*wd,1); conf = round(86+r5*13.9,1)
        st = STREETS[int(r1*len(STREETS))]; stt = ST_TYPES[int(r2*len(ST_TYPES))]
        num = int(r3*150)+1; unit = f"{int(r4*24)+1}/" if pt=="Strata" else ""
        items.append({
            "id":f"f-{postcode}-{i}","lat":round(flat,6),"lng":round(flng,6),
            "address":f"{unit}{num} {st} {stt}",
            "full_address":f"{unit}{num} {st} {stt}, {suburb} {state} {postcode}",
            "suburb":suburb,"state":state,"postcode":postcode,"property_type":pt,
            "length_m":ln,"width_m":wd,"area_m2":area,
            "variant":VARIANTS[int(r5*len(VARIANTS))],"finish":FINISHES[int(r3*len(FINISHES))],
            "confidence":conf,"est_capacity":round(area*(1.8 if pt=="Commercial" else 1.4),1),
            "year_installed":1975+int(r4*49),
            "ref_number":f"DA/{2010+int(r1*14)}/{int(1000+r2*8999)}" if r5>0.4 else "",
        })
    return items

def get_real_features(suburb, state, postcode=None):
    conn = get_db()
    if postcode:
        rows = conn.execute("SELECT * FROM features WHERE postcode=? ORDER BY confidence DESC", (postcode,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM features WHERE suburb=? AND state=? ORDER BY confidence DESC", (suburb, state)).fetchall()
    conn.close()
    return [dict(r) for r in rows] if rows else None


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
    CREATE INDEX IF NOT EXISTS idx_feat_pc ON features(postcode);
    CREATE TABLE IF NOT EXISTS scans (
        id TEXT PRIMARY KEY, region_key TEXT,
        center_lat REAL, center_lng REAL,
        features_found INTEGER DEFAULT 0, tiles_fetched INTEGER DEFAULT 0,
        tiles_total INTEGER DEFAULT 0, duration_sec REAL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS purchases (
        id TEXT PRIMARY KEY, region_key TEXT, plan TEXT DEFAULT 'pro',
        coupon TEXT DEFAULT '', user_id TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS coupons (
        code TEXT PRIMARY KEY, plan TEXT DEFAULT 'pro',
        max_uses INTEGER DEFAULT 50, times_used INTEGER DEFAULT 0,
        discount_pct INTEGER DEFAULT 100, active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE,
        password_hash TEXT,
        name TEXT DEFAULT '',
        company TEXT DEFAULT '',
        role TEXT DEFAULT 'member',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id TEXT,
        role TEXT DEFAULT 'member',
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    for code, info in DEFAULT_COUPONS.items():
        conn.execute("INSERT OR IGNORE INTO coupons (code, plan, max_uses, discount_pct) VALUES (?,?,?,?)",
                     (code, info["plan"], info["max_uses"], info["discount_pct"]))
    # Ensure admin user exists
    admin_hash = hashlib.sha256(ADMIN_PASS.encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO users (id, email, password_hash, name, role) VALUES (?,?,?,?,?)",
                 ("admin", "admin@propscan.local", admin_hash, "Admin", "admin"))
    conn.commit(); conn.close()


# ── App ──────────────────────────────────────────────────────

app = FastAPI(title="PropScan", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=True)

@app.on_event("startup")
async def startup():
    init_db()

def get_session(token: Optional[str]) -> Optional[dict]:
    if not token: return None
    conn = get_db()
    row = conn.execute("SELECT s.*, u.email, u.name, u.company FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token=?", (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Auth Endpoints ───────────────────────────────────────────

class RegisterReq(BaseModel):
    email: str
    password: str
    name: str = ""
    company: str = ""

class LoginReq(BaseModel):
    email: str
    password: str

@app.post("/api/auth/register")
async def register(req: RegisterReq):
    conn = get_db()
    exists = conn.execute("SELECT id FROM users WHERE email=?", (req.email.lower(),)).fetchone()
    if exists:
        conn.close(); raise HTTPException(400, "Email already registered")
    uid = secrets.token_hex(8)
    pw_hash = hashlib.sha256(req.password.encode()).hexdigest()
    conn.execute("INSERT INTO users (id, email, password_hash, name, company) VALUES (?,?,?,?,?)",
                 (uid, req.email.lower(), pw_hash, req.name, req.company))
    token = secrets.token_hex(32)
    conn.execute("INSERT INTO sessions (token, user_id, role) VALUES (?,?,?)", (token, uid, "member"))
    conn.commit(); conn.close()
    resp = JSONResponse({"ok": True, "user": {"id": uid, "email": req.email, "name": req.name}})
    resp.set_cookie("ps_session", token, httponly=True, max_age=86400*30)
    return resp

@app.post("/api/auth/login")
async def login(req: LoginReq):
    conn = get_db()
    pw_hash = hashlib.sha256(req.password.encode()).hexdigest()
    user = conn.execute("SELECT * FROM users WHERE email=? AND password_hash=?",
                        (req.email.lower(), pw_hash)).fetchone()
    if not user:
        conn.close(); raise HTTPException(401, "Invalid credentials")
    token = secrets.token_hex(32)
    conn.execute("INSERT INTO sessions (token, user_id, role) VALUES (?,?,?)",
                 (token, user["id"], user["role"]))
    conn.commit(); conn.close()
    resp = JSONResponse({"ok": True, "user": {"id": user["id"], "email": user["email"],
                         "name": user["name"], "role": user["role"]}})
    resp.set_cookie("ps_session", token, httponly=True, max_age=86400*30)
    return resp

@app.get("/api/auth/me")
async def auth_me(ps_session: str = Cookie(None)):
    sess = get_session(ps_session)
    if not sess: raise HTTPException(401)
    return {"user": {"id": sess["user_id"], "email": sess["email"],
                     "name": sess["name"], "role": sess["role"]}}

@app.post("/api/auth/logout")
async def logout(ps_session: str = Cookie(None)):
    if ps_session:
        conn = get_db()
        conn.execute("DELETE FROM sessions WHERE token=?", (ps_session,))
        conn.commit(); conn.close()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ps_session")
    return resp


# ── Geocoding API ────────────────────────────────────────────

@app.get("/api/geocode")
async def geocode_endpoint(q: str):
    """Geocode a location string. Returns lat/lng/suburb/state/postcode."""
    from geocoder import geocode
    result = geocode(q)
    if not result:
        raise HTTPException(404, "Location not found")
    return result

@app.get("/api/suggest")
async def suggest_endpoint(q: str):
    """Autocomplete suggestions for location search."""
    from geocoder import search_suggestions
    return {"suggestions": search_suggestions(q)}


# ── Public Scan API ──────────────────────────────────────────

class ScanReq(BaseModel):
    lat: float
    lng: float
    suburb: str = ""
    state: str = ""
    postcode: str = ""
    radius_km: int = 2

class RedeemReq(BaseModel):
    region_key: str
    coupon: str

@app.get("/api/featured")
async def list_featured():
    """Return featured regions for quick-select."""
    conn = get_db()
    regions = []
    for key, (lat, lng) in sorted(FEATURED.items()):
        parts = key.split()
        name, state, pc = " ".join(parts[:-2]), parts[-2], parts[-1]
        real_count = conn.execute("SELECT COUNT(*) FROM features WHERE suburb=? AND state=?", (name, state)).fetchone()[0]
        regions.append({"key": key, "name": name, "state": state, "postcode": pc,
                        "lat": lat, "lng": lng, "scanned": real_count > 0, "real_count": real_count})
    conn.close()
    return {"regions": regions}

@app.get("/api/radius-options")
async def radius_options():
    """Available scan radius options."""
    return {"options": [{"km": k, **v} for k, v in RADIUS_OPTIONS.items()],
            "scan_duration_hint": SCAN_ANIMATION_SEC}

@app.post("/api/scan")
async def scan(req: ScanReq):
    """Scan any location. Uses real data if available, generated fallback otherwise."""
    if not (-45 < req.lat < -10 and 110 < req.lng < 155):
        raise HTTPException(400, "Location must be in Australia")

    # Resolve suburb/state/postcode if not provided
    if not req.suburb or not req.postcode:
        from geocoder import reverse_geocode
        geo = reverse_geocode(req.lat, req.lng)
        if geo:
            req.suburb = req.suburb or geo.get("suburb", "Unknown")
            req.state = req.state or geo.get("state", "")
            req.postcode = req.postcode or geo.get("postcode", "0000")

    region_key = f"{req.suburb} {req.state} {req.postcode}"

    # Check for real data
    real = get_real_features(req.suburb, req.state, req.postcode)
    if real:
        features = real
        is_real = True
    else:
        # Estimate count based on radius
        base_count = 40 + hash(region_key) % 80
        multiplier = {1: 0.5, 2: 1, 5: 2.5, 10: 5}.get(req.radius_km, 1)
        count = max(10, int(base_count * multiplier))
        features = gen_features(req.lat, req.lng, req.suburb, req.state, req.postcode, count)
        is_real = False

    scan_id = hashlib.md5(f"{region_key}{time.time()}".encode()).hexdigest()[:16]
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO scans (id, region_key, center_lat, center_lng, features_found) VALUES (?,?,?,?,?)",
                 (scan_id, region_key, req.lat, req.lng, len(features)))

    # Check if purchased
    row = conn.execute("SELECT id FROM purchases WHERE region_key=?", (region_key,)).fetchone()
    conn.commit(); conn.close()
    is_bought = row is not None

    res = sum(1 for f in features if f.get("property_type") == "Residential")
    stc = sum(1 for f in features if f.get("property_type") == "Strata")
    com = sum(1 for f in features if f.get("property_type") == "Commercial")
    stats = {"total": len(features), "residential": res, "strata": stc, "commercial": com}

    if is_bought:
        return {"scan_id": scan_id, "features": features, "purchased": True,
                "is_real": is_real, "stats": stats, "region_key": region_key,
                "scan_duration_hint": SCAN_ANIMATION_SEC}

    preview = features[:FREE_PREVIEW]
    locked = [{"id": f.get("id", ""), "lat": round(f.get("lat", 0), 3),
               "lng": round(f.get("lng", 0), 3), "property_type": f.get("property_type", ""),
               "area_m2": f.get("area_m2", 0),
               "address": (f.get("address", "")[:4] if f.get("address") else "") + "██████████",
               "locked": True} for f in features[FREE_PREVIEW:]]

    return {"scan_id": scan_id, "features": preview + locked, "purchased": False,
            "locked_count": len(locked), "is_real": is_real, "stats": stats,
            "region_key": region_key, "scan_duration_hint": SCAN_ANIMATION_SEC}


@app.post("/api/redeem")
async def redeem(req: RedeemReq):
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

    # Return features for this region
    parts = req.region_key.split()
    if len(parts) >= 3:
        pc, st, nm = parts[-1], parts[-2], " ".join(parts[:-2])
        real = get_real_features(nm, st, pc)
        if real:
            return {"purchase_id": pid, "unlocked": len(real), "features": real, "is_real": True}
    return {"purchase_id": pid, "unlocked": 0, "features": [], "is_real": False,
            "message": "Rescan to load features"}


@app.get("/api/export")
async def export_csv(region_key: str):
    parts = region_key.split()
    if len(parts) < 3:
        raise HTTPException(400, "Invalid region key")
    pc, state, suburb = parts[-1], parts[-2], " ".join(parts[:-2])
    real = get_real_features(suburb, state, pc)
    features = real if real else gen_features(0, 0, suburb, state, pc)

    hdr = "ID,Address,Suburb,State,Postcode,Type,Length_m,Width_m,Area_m2,Variant,Finish,Capacity,Confidence,DA_Ref,Year,Lat,Lng"
    lines = [hdr]
    for f in features:
        lines.append(f'{f.get("id","")},"{f.get("full_address",f.get("address",""))}",{f.get("suburb","")},{f.get("state","")},{f.get("postcode","")},'
            f'{f.get("property_type","")},{f.get("length_m","")},{f.get("width_m","")},{f.get("area_m2","")},'
            f'{f.get("variant","")},{f.get("finish","")},{f.get("est_capacity","")},{f.get("confidence","")},'
            f'{f.get("ref_number","")},{f.get("year_installed","")},{f.get("lat","")},{f.get("lng","")}')
    fn = f"propscan_{'_'.join(region_key.split())}.csv"
    return StreamingResponse(iter(["\n".join(lines)]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={fn}"})

@app.get("/api/stats")
async def stats():
    conn = get_db()
    sc = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    pr = conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
    rf = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    us = conn.execute("SELECT COUNT(*) FROM users WHERE role='member'").fetchone()[0]
    conn.close()
    return {"regions_featured": len(FEATURED), "scans": sc, "purchases": pr,
            "real_features_in_db": rf, "members": us, "scan_duration_hint": SCAN_ANIMATION_SEC}


# ── Admin Routes ─────────────────────────────────────────────

def check_admin(token):
    sess = get_session(token)
    return sess and sess["role"] == "admin"

@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.json()
    if form.get("password") != ADMIN_PASS:
        raise HTTPException(401)
    conn = get_db()
    token = secrets.token_hex(32)
    conn.execute("INSERT INTO sessions (token, user_id, role) VALUES (?,?,?)", (token, "admin", "admin"))
    conn.commit(); conn.close()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ps_session", token, httponly=True, max_age=86400*7)
    return resp

@app.get("/admin/data")
async def admin_data(ps_session: str = Cookie(None)):
    if not check_admin(ps_session): raise HTTPException(401)
    conn = get_db()
    data = {
        "stats": {
            "regions_featured": len(FEATURED),
            "real_features": conn.execute("SELECT COUNT(*) FROM features").fetchone()[0],
            "scans": conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0],
            "purchases": conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0],
            "members": conn.execute("SELECT COUNT(*) FROM users WHERE role='member'").fetchone()[0],
        },
        "coupons": [dict(r) for r in conn.execute("SELECT * FROM coupons ORDER BY created_at DESC").fetchall()],
        "purchases": [dict(r) for r in conn.execute("SELECT * FROM purchases ORDER BY created_at DESC LIMIT 50").fetchall()],
        "members": [{"id":r["id"],"email":r["email"],"name":r["name"],"company":r["company"],"created_at":r["created_at"]}
                     for r in conn.execute("SELECT * FROM users WHERE role='member' ORDER BY created_at DESC LIMIT 50").fetchall()],
        "regions_scanned": [dict(r) for r in conn.execute(
            "SELECT suburb, state, postcode, COUNT(*) as count, ROUND(AVG(area_m2),1) as avg_area, "
            "ROUND(AVG(confidence),1) as avg_conf FROM features GROUP BY suburb, state ORDER BY count DESC"
        ).fetchall()],
        "active_jobs": {jid: {k: v for k, v in j.items() if k != "result"} for jid, j in scan_jobs.items()
                        if j["status"] in ("pending", "fetching", "saving")},
    }
    conn.close()
    return data

@app.post("/admin/coupon")
async def admin_coupon(request: Request, ps_session: str = Cookie(None)):
    if not check_admin(ps_session): raise HTTPException(401)
    form = await request.json()
    action = form.get("action")
    conn = get_db()
    if action == "create":
        code = form.get("code", "").strip().upper()
        if not code: code = secrets.token_hex(4).upper()
        conn.execute("INSERT OR IGNORE INTO coupons (code, plan, max_uses, discount_pct) VALUES (?,?,?,?)",
                     (code, form.get("plan", "pro"), form.get("max_uses", 50), form.get("discount_pct", 100)))
    elif action == "delete":
        conn.execute("DELETE FROM coupons WHERE code=?", (form.get("code", ""),))
    elif action == "toggle":
        conn.execute("UPDATE coupons SET active = NOT active WHERE code=?", (form.get("code", ""),))
    conn.commit(); conn.close()
    return {"ok": True}

@app.post("/admin/scan")
async def admin_trigger_scan(request: Request, ps_session: str = Cookie(None)):
    if not check_admin(ps_session): raise HTTPException(401)
    form = await request.json()
    lat, lng = form.get("lat"), form.get("lng")
    suburb = form.get("suburb", "Unknown")
    state = form.get("state", "")
    postcode = form.get("postcode", "0000")
    radius_km = form.get("radius_km", 2)

    if not lat or not lng: raise HTTPException(400, "lat/lng required")

    job_id = secrets.token_hex(8)
    scan_jobs[job_id] = {
        "status": "pending", "region": f"{suburb} {state} {postcode}",
        "message": "Starting...", "result": None,
        "started": time.time(), "finished": None,
    }
    t = threading.Thread(target=run_scan_job,
                         args=(job_id, lat, lng, suburb, state, postcode, radius_km), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}

@app.get("/admin/scan-status/{job_id}")
async def admin_scan_status(job_id: str, ps_session: str = Cookie(None)):
    if not check_admin(ps_session): raise HTTPException(401)
    if job_id not in scan_jobs: raise HTTPException(404)
    return scan_jobs[job_id]

@app.post("/admin/upload-db")
async def admin_upload_db(request: Request, ps_session: str = Cookie(None)):
    if not check_admin(ps_session): raise HTTPException(401)
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


# ── Admin Dashboard ──────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    p = Path("static/admin.html")
    if p.exists(): return HTMLResponse(p.read_text())
    return HTMLResponse("<html><body><h1>Admin</h1><p>Missing static/admin.html</p></body></html>")


# ── Frontend ─────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    p = Path("static/index.html")
    if p.exists(): return HTMLResponse(p.read_text())
    return HTMLResponse("<html><body><h1>PropScan</h1></body></html>")

@app.get("/app", response_class=HTMLResponse)
async def app_page():
    p = Path("static/app.html")
    if p.exists(): return HTMLResponse(p.read_text())
    return HTMLResponse("<html><body><h1>Scanner App</h1></body></html>")
