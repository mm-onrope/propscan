"""
PropScan — Geocoding module
Free geocoding via Nominatim (OpenStreetMap) — no API key needed.
"""
import time
import logging
import json
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import requests

log = logging.getLogger("geocoder")

NOMINATIM_URL = "https://nominatim.openstreetmap.org"
HEADERS = {"User-Agent": "PoolFinder/1.0 (pool detection SaaS, contact@poolfinder.com.au)"}
CACHE_PATH = Path("data/geocache.json")
REQUEST_DELAY = 1.1  # Nominatim requires 1 req/sec

# Hardcoded fallback for common Australian postcodes (when Nominatim is down)
AU_POSTCODES = {
    "2000":(-33.8688,151.2093,"Sydney","NSW"),"2010":(-33.8775,151.2227,"Darlinghurst","NSW"),
    "2026":(-33.8915,151.2767,"Bondi","NSW"),"2030":(-33.8579,151.2783,"Vaucluse","NSW"),
    "2060":(-33.8378,151.2073,"North Sydney","NSW"),"2065":(-33.8208,151.1955,"Crows Nest","NSW"),
    "2070":(-33.7649,151.1461,"Lindfield","NSW"),"2076":(-33.7178,151.117,"Wahroonga","NSW"),
    "2088":(-33.8292,151.2441,"Mosman","NSW"),"2095":(-33.7969,151.2844,"Manly","NSW"),
    "2099":(-33.7489,151.2878,"Dee Why","NSW"),"2100":(-33.7678,151.2559,"Brookvale","NSW"),
    "2110":(-33.8345,151.1437,"Hunters Hill","NSW"),"2112":(-33.7987,151.0884,"Ryde","NSW"),
    "2120":(-33.7524,151.0779,"Thornleigh","NSW"),"2145":(-33.8054,150.9796,"Westmead","NSW"),
    "2150":(-33.8148,151.0017,"Parramatta","NSW"),"2155":(-33.709,150.956,"Kellyville","NSW"),
    "2154":(-33.731,151.005,"Castle Hill","NSW"),"2170":(-33.9193,150.9232,"Liverpool","NSW"),
    "2195":(-33.8833,151.0833,"Lakemba","NSW"),"2200":(-33.9167,151.0333,"Bankstown","NSW"),
    "2230":(-34.0547,151.1518,"Cronulla","NSW"),"2250":(-33.4268,151.342,"Gosford","NSW"),
    "3000":(-37.8136,144.9631,"Melbourne","VIC"),"3006":(-37.8266,144.9689,"Southbank","VIC"),
    "3121":(-37.8183,144.9928,"Richmond","VIC"),"3142":(-37.8415,145.0087,"Toorak","VIC"),
    "3186":(-37.9067,144.9879,"Brighton","VIC"),"3199":(-38.2173,145.0369,"Frankston","VIC"),
    "4000":(-27.4698,153.0251,"Brisbane","QLD"),"4007":(-27.4326,153.0597,"Ascot","QLD"),
    "4064":(-27.4598,153.0094,"Paddington","QLD"),"4217":(-28.0027,153.4295,"Surfers Paradise","QLD"),
    "4221":(-28.1118,153.4637,"Palm Beach","QLD"),"4567":(-26.3907,153.0909,"Noosa Heads","QLD"),
    "5000":(-34.9285,138.6007,"Adelaide","SA"),"5066":(-34.9399,138.6586,"Burnside","SA"),
    "6000":(-31.9505,115.8605,"Perth","WA"),"6009":(-31.9811,115.8053,"Nedlands","WA"),
    "6011":(-31.9998,115.7652,"Peppermint Grove","WA"),
    "7000":(-42.8821,147.3272,"Hobart","TAS"),"7005":(-42.9032,147.3364,"Sandy Bay","TAS"),
}

# In-memory cache (persisted to disk)
_cache: Dict[str, dict] = {}


def _load_cache():
    global _cache
    if CACHE_PATH.exists():
        try:
            _cache = json.loads(CACHE_PATH.read_text())
        except:
            _cache = {}

def _save_cache():
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(_cache, indent=2))

_load_cache()


def geocode(query: str, country: str = "au") -> Optional[dict]:
    """
    Geocode a search string to lat/lng.
    Accepts: suburb name, postcode, "suburb state postcode", full address.
    Returns: {lat, lng, display_name, suburb, state, postcode, bounds} or None.
    """
    cache_key = f"geo:{query.lower().strip()}"
    if cache_key in _cache:
        return _cache[cache_key]

    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 5,
        "countrycodes": country,
    }

    try:
        time.sleep(REQUEST_DELAY)
        r = requests.get(f"{NOMINATIM_URL}/search", params=params,
                         headers=HEADERS, timeout=20)
        r.raise_for_status()
        results = r.json()
    except Exception as e:
        log.warning(f"Geocode failed for '{query}': {e}")
        # Try hardcoded fallback
        fb = _fallback_geocode(query)
        if fb:
            _cache[cache_key] = fb
            _save_cache()
            return fb
        return None

    if not results:
        # Try hardcoded fallback
        fb = _fallback_geocode(query)
        if fb:
            _cache[cache_key] = fb
            _save_cache()
            return fb
        return None

    # Pick best result — prefer suburb/city level over individual buildings
    best = results[0]
    for res in results:
        rtype = res.get("type", "")
        if rtype in ("suburb", "city", "town", "village", "postcode"):
            best = res
            break

    addr = best.get("address", {})
    result = {
        "lat": float(best["lat"]),
        "lng": float(best["lon"]),
        "display_name": best.get("display_name", ""),
        "suburb": addr.get("suburb") or addr.get("city") or addr.get("town") or addr.get("village") or "",
        "state": _abbrev_state(addr.get("state", "")),
        "postcode": addr.get("postcode", ""),
        "type": best.get("type", ""),
        "bounds": best.get("boundingbox"),  # [south, north, west, east]
    }

    _cache[cache_key] = result
    _save_cache()
    return result


def geocode_postcode(postcode: str) -> Optional[dict]:
    """Geocode an Australian postcode to center + bounds."""
    return geocode(f"{postcode}, Australia")


def reverse_geocode(lat: float, lng: float) -> Optional[dict]:
    """Reverse geocode lat/lng to address."""
    cache_key = f"rev:{lat:.5f},{lng:.5f}"
    if cache_key in _cache:
        return _cache[cache_key]

    params = {
        "lat": lat, "lon": lng,
        "format": "jsonv2", "addressdetails": 1,
    }

    try:
        time.sleep(REQUEST_DELAY)
        r = requests.get(f"{NOMINATIM_URL}/reverse", params=params,
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"Reverse geocode failed for ({lat},{lng}): {e}")
        return None

    addr = data.get("address", {})
    result = {
        "display_name": data.get("display_name", ""),
        "road": addr.get("road", ""),
        "house_number": addr.get("house_number", ""),
        "suburb": addr.get("suburb") or addr.get("city") or addr.get("town") or "",
        "state": _abbrev_state(addr.get("state", "")),
        "postcode": addr.get("postcode", ""),
    }

    _cache[cache_key] = result
    _save_cache()
    return result


def search_suggestions(query: str, limit: int = 5) -> List[dict]:
    """
    Return geocoding suggestions for autocomplete.
    Returns list of {display, lat, lng, suburb, state, postcode, type}.
    """
    if len(query.strip()) < 2:
        return []

    cache_key = f"sug:{query.lower().strip()}"
    if cache_key in _cache:
        return _cache[cache_key]

    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
        "countrycodes": "au",
    }

    try:
        time.sleep(REQUEST_DELAY)
        r = requests.get(f"{NOMINATIM_URL}/search", params=params,
                         headers=HEADERS, timeout=20)
        r.raise_for_status()
        results = r.json()
    except:
        # Fallback to hardcoded matches
        results = []
        q_lower = query.lower().strip()
        for pc, (lat, lng, suburb, state) in AU_POSTCODES.items():
            if q_lower in suburb.lower() or q_lower == pc or q_lower in pc:
                suggestions.append({
                    "display": f"{suburb}, {state} {pc}",
                    "lat": lat, "lng": lng, "suburb": suburb,
                    "state": state, "postcode": pc, "type": "suburb",
                })
        _cache[cache_key] = suggestions
        _save_cache()
        return suggestions[:limit]

    suggestions = []
    seen = set()
    for res in results:
        addr = res.get("address", {})
        suburb = addr.get("suburb") or addr.get("city") or addr.get("town") or addr.get("village") or ""
        state = _abbrev_state(addr.get("state", ""))
        pc = addr.get("postcode", "")
        key = f"{suburb}:{state}:{pc}"
        if key in seen:
            continue
        seen.add(key)

        suggestions.append({
            "display": f"{suburb}, {state} {pc}" if suburb else res.get("display_name", "")[:60],
            "lat": float(res["lat"]),
            "lng": float(res["lon"]),
            "suburb": suburb,
            "state": state,
            "postcode": pc,
            "type": res.get("type", ""),
        })

    _cache[cache_key] = suggestions
    _save_cache()
    return suggestions


def _fallback_geocode(query: str) -> Optional[dict]:
    """Try to match query against hardcoded Australian postcodes/suburbs."""
    q = query.lower().strip().rstrip(",").strip()
    # Remove "australia" suffix
    for suffix in [", australia", " australia", " au"]:
        if q.endswith(suffix):
            q = q[:-len(suffix)].strip().rstrip(",").strip()

    # Try exact postcode match
    for pc, (lat, lng, suburb, state) in AU_POSTCODES.items():
        if q == pc or q == f"{suburb.lower()} {state.lower()} {pc}" or q == f"{suburb.lower()}, {state.lower()} {pc}":
            return {"lat": lat, "lng": lng, "display_name": f"{suburb}, {state} {pc}",
                    "suburb": suburb, "state": state, "postcode": pc, "type": "fallback", "bounds": None}

    # Try suburb name match
    for pc, (lat, lng, suburb, state) in AU_POSTCODES.items():
        if q == suburb.lower() or q == f"{suburb.lower()}, {state.lower()}" or q == f"{suburb.lower()} {state.lower()}":
            return {"lat": lat, "lng": lng, "display_name": f"{suburb}, {state} {pc}",
                    "suburb": suburb, "state": state, "postcode": pc, "type": "fallback", "bounds": None}

    # Partial suburb match
    matches = []
    for pc, (lat, lng, suburb, state) in AU_POSTCODES.items():
        if q in suburb.lower():
            matches.append({"lat": lat, "lng": lng, "display_name": f"{suburb}, {state} {pc}",
                           "suburb": suburb, "state": state, "postcode": pc, "type": "fallback", "bounds": None})
    if matches:
        return matches[0]

    return None


def _abbrev_state(state_name: str) -> str:
    """Convert full state name to abbreviation."""
    mapping = {
        "new south wales": "NSW", "victoria": "VIC", "queensland": "QLD",
        "south australia": "SA", "western australia": "WA",
        "tasmania": "TAS", "northern territory": "NT",
        "australian capital territory": "ACT",
    }
    return mapping.get(state_name.lower().strip(), state_name[:3].upper() if state_name else "")
