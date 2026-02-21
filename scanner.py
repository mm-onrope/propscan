"""
PropScan — Satellite Feature Scanner
Fetches ESRI tiles and runs colour-based detection.
"""
import math
import time
import hashlib
import logging
from pathlib import Path
from io import BytesIO

import requests
import numpy as np
from PIL import Image

log = logging.getLogger("scanner")

TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TILE_CACHE = Path("data/tiles")
TILE_CACHE.mkdir(parents=True, exist_ok=True)

ZOOM = 18
RADIUS = 3
TS = 256
DELAY = 0.08

HSV_RANGES = [
    {"h": (85, 130), "s": (35, 255), "v": (100, 255)},
    {"h": (75, 100), "s": (40, 255), "v": (90, 255)},
    {"h": (100, 145), "s": (50, 255), "v": (70, 240)},
    {"h": (80, 105), "s": (60, 255), "v": (120, 255)},
]
MIN_A = 30
MAX_A = 15000
MIN_FILL = 0.30
MIN_CONF = 50.0


def ll2t(lat, lng, z):
    n = 2 ** z
    x = int((lng + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y

def t2ll(x, y, z):
    n = 2 ** z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))), x / n * 360 - 180

def mpp(lat, z):
    return 156543.03 * math.cos(math.radians(lat)) / (2 ** z)


def fetch_tile(x, y, z):
    c = TILE_CACHE / f"{z}_{x}_{y}.png"
    if c.exists():
        try:
            return Image.open(c).convert("RGB")
        except:
            c.unlink(missing_ok=True)
    try:
        r = requests.get(TILE_URL.format(z=z, x=x, y=y), timeout=20,
                         headers={"User-Agent": "PropScan/1.0"})
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")
        img.save(c)
        return img
    except Exception as e:
        log.warning(f"Tile fail {z}/{x}/{y}: {e}")
        return None


def fetch_area(lat, lng, zoom=ZOOM, radius=RADIUS, progress_cb=None):
    cx, cy = ll2t(lat, lng, zoom)
    gs = 2 * radius + 1
    comp = Image.new("RGB", (gs * TS, gs * TS))
    fetched = 0
    total = gs * gs

    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            tile = fetch_tile(cx + dx, cy + dy, zoom)
            if tile:
                comp.paste(tile, ((dx + radius) * TS, (dy + radius) * TS))
                fetched += 1
            if progress_cb:
                progress_cb(fetched, total)
            time.sleep(DELAY)

    nw = t2ll(cx - radius, cy - radius, zoom)
    se = t2ll(cx + radius + 1, cy + radius + 1, zoom)
    return comp, {
        "bounds": {"north": nw[0], "south": se[0], "west": nw[1], "east": se[1]},
        "mpp": mpp(lat, zoom), "fetched": fetched, "total": total,
    }


def rgb2hsv(a):
    f = a.astype(np.float32) / 255
    r, g, b = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    h = np.zeros_like(mx)
    m = d > 0
    mr, mg, mb = m & (mx == r), m & (mx == g), m & (mx == b)
    h[mr] = (60 * ((g[mr] - b[mr]) / d[mr]) + 360) % 360
    h[mg] = (60 * ((b[mg] - r[mg]) / d[mg]) + 120) % 360
    h[mb] = (60 * ((r[mb] - g[mb]) / d[mb]) + 240) % 360
    s = np.zeros_like(mx)
    s[mx > 0] = d[mx > 0] / mx[mx > 0] * 255
    return (h / 2).astype(np.uint8), s.astype(np.uint8), (mx * 255).astype(np.uint8)


def detect(image, meta):
    arr = np.array(image)
    ih, iw = arr.shape[:2]
    b = meta["bounds"]
    m = meta["mpp"]

    h, s, v = rgb2hsv(arr)

    mask = np.zeros((ih, iw), dtype=np.uint8)
    for rng in HSV_RANGES:
        hr, sr, vr = rng["h"], rng["s"], rng["v"]
        mask[((h >= hr[0]) & (h <= hr[1]) & (s >= sr[0]) & (s <= sr[1]) &
              (v >= vr[0]) & (v <= vr[1]))] = 255

    # Morphological cleanup
    k = 3
    p = k // 2
    pd = np.pad(mask, p, mode="constant", constant_values=0)
    dl = np.zeros_like(mask)
    for dy in range(k):
        for dx in range(k):
            dl = np.maximum(dl, pd[dy:dy + ih, dx:dx + iw])
    pd = np.pad(dl, p, mode="constant", constant_values=255)
    er = np.full_like(mask, 255)
    for dy in range(k):
        for dx in range(k):
            er = np.minimum(er, pd[dy:dy + ih, dx:dx + iw])
    mask = er

    # Connected components via BFS
    vis = np.zeros((ih, iw), dtype=bool)
    feats = []
    for y in range(ih):
        for x in range(iw):
            if mask[y, x] > 0 and not vis[y, x]:
                comp = []
                q = [(y, x)]
                vis[y, x] = True
                while q:
                    cy2, cx2 = q.pop(0)
                    comp.append((cy2, cx2))
                    if len(comp) > MAX_A:
                        break
                    for d2, d1 in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                        ny, nx = cy2 + d2, cx2 + d1
                        if 0 <= ny < ih and 0 <= nx < iw and not vis[ny, nx] and mask[ny, nx] > 0:
                            vis[ny, nx] = True
                            q.append((ny, nx))

                if MIN_A <= len(comp) <= MAX_A:
                    ys = [c2[0] for c2 in comp]
                    xs = [c2[1] for c2 in comp]
                    my, My, mx2, Mx = min(ys), max(ys), min(xs), max(xs)
                    bw, bh = Mx - mx2 + 1, My - my + 1
                    if bw < 3 or bh < 3:
                        continue
                    fill = len(comp) / (bw * bh)
                    if fill < MIN_FILL:
                        continue

                    cpx = (mx2 + Mx) / 2
                    cpy = (my + My) / 2
                    lat2 = b["north"] - (cpy / ih) * (b["north"] - b["south"])
                    lng2 = b["west"] + (cpx / iw) * (b["east"] - b["west"])

                    ln = round(max(bw, bh) * m, 1)
                    wd = round(min(bw, bh) * m, 1)
                    ar = round(len(comp) * m * m, 1)

                    sr2 = s[my:My + 1, mx2:Mx + 1]
                    bp = mask[my:My + 1, mx2:Mx + 1] > 0
                    avs = float(np.mean(sr2[bp])) if np.any(bp) else 0

                    cf = min(99.5, fill * 25 + min(1, avs / 140) * 30 +
                             (25 if 8 < ar < 300 else 10) +
                             (min(bw, bh) / max(bw, bh)) * 20)
                    if cf < MIN_CONF:
                        continue

                    asp = min(bw, bh) / max(bw, bh) if max(bw, bh) > 0 else 1
                    if asp < 0.25:
                        shape = "Lap"
                    elif fill > 0.85 and asp > 0.6:
                        shape = "Rectangular"
                    elif fill > 0.7 and asp > 0.4:
                        shape = "Oval"
                    elif fill < 0.55:
                        shape = "L-Shaped"
                    elif fill > 0.55 and asp > 0.35:
                        shape = "Kidney"
                    else:
                        shape = "Freeform"

                    pt = "Commercial" if ar > 200 else ("Strata" if ar > 80 else "Residential")

                    feats.append({
                        "id": hashlib.md5(f"{lat2:.6f}{lng2:.6f}".encode()).hexdigest()[:12],
                        "lat": round(lat2, 6), "lng": round(lng2, 6),
                        "px": mx2, "py": my, "pw": bw, "ph": bh,
                        "length_m": ln, "width_m": wd, "area_m2": ar,
                        "variant": shape, "property_type": pt,
                        "confidence": round(cf, 1),
                        "est_capacity": round(ar * 1.4, 1),
                    })

    # NMS
    feats.sort(key=lambda f2: f2["confidence"], reverse=True)
    keep = []
    for ft in feats:
        ov = False
        for k2 in keep:
            x1 = max(ft["px"], k2["px"])
            y1 = max(ft["py"], k2["py"])
            x2 = min(ft["px"] + ft["pw"], k2["px"] + k2["pw"])
            y2 = min(ft["py"] + ft["ph"], k2["py"] + k2["ph"])
            if x1 < x2 and y1 < y2:
                inter = (x2 - x1) * (y2 - y1)
                a1 = ft["pw"] * ft["ph"]
                a2 = k2["pw"] * k2["ph"]
                if inter / (a1 + a2 - inter) > 0.3:
                    ov = True
                    break
        if not ov:
            keep.append(ft)

    return keep


def scan_region(lat, lng, suburb, state, postcode):
    """Full scan pipeline: fetch tiles → detect → return features."""
    t0 = time.time()
    image, meta = fetch_area(lat, lng)

    if meta["fetched"] == 0:
        return {"error": "No tiles fetched", "features": [], "duration": 0}

    features = detect(image, meta)
    duration = round(time.time() - t0, 1)

    scan_id = hashlib.md5(f"{suburb}{state}{time.time()}".encode()).hexdigest()[:16]

    return {
        "scan_id": scan_id,
        "features": features,
        "tiles_fetched": meta["fetched"],
        "tiles_total": meta["total"],
        "mpp": meta["mpp"],
        "duration": duration,
    }
