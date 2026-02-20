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
HEADERS = {"User-Agent": "PropScan/1.0 (contact@example.com)"}
CACHE_PATH = Path("data/geocache.json")
REQUEST_DELAY = 1.1  # Nominatim requires 1 req/sec

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
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        results = r.json()
    except Exception as e:
        log.warning(f"Geocode failed for '{query}': {e}")
        return None

    if not results:
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
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        results = r.json()
    except:
        return []

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


def _abbrev_state(state_name: str) -> str:
    """Convert full state name to abbreviation."""
    mapping = {
        "new south wales": "NSW", "victoria": "VIC", "queensland": "QLD",
        "south australia": "SA", "western australia": "WA",
        "tasmania": "TAS", "northern territory": "NT",
        "australian capital territory": "ACT",
    }
    return mapping.get(state_name.lower().strip(), state_name[:3].upper() if state_name else "")
