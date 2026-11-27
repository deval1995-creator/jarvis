"""Nearby places from Google Places API (New): restaurants, shops, services.

About 0.6 s per search, with Google Maps data: rating and review count, price
level, open now and today's hours, distance, address, phone, website and a Maps
link. "Near" defaults to Deval's home (Whitby, Ontario); another town is placed
with the same lookup the weather uses.

Cost note: rating/hours/phone/website are Google's "Enterprise" fields, whose
free monthly allowance is smaller (about 1,000 searches, as of 2025), ample for
personal use. The key lives in the data folder (google_places_api_key).
"""

from __future__ import annotations

import asyncio
import json
import math
import urllib.error
import urllib.request
from datetime import datetime

from core import db
from core import weather as wx

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join("places." + f for f in [
    "displayName", "formattedAddress", "location", "rating", "userRatingCount", "priceLevel",
    "currentOpeningHours.openNow", "currentOpeningHours.weekdayDescriptions", "nationalPhoneNumber",
    "websiteUri", "googleMapsUri", "primaryTypeDisplayName", "businessStatus",
])
PRICE = {"PRICE_LEVEL_FREE": "free", "PRICE_LEVEL_INEXPENSIVE": "$", "PRICE_LEVEL_MODERATE": "$$",
         "PRICE_LEVEL_EXPENSIVE": "$$$", "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$"}


class PlacesError(RuntimeError):
    pass


def api_key() -> str | None:
    try:
        return (db.default_db_path().parent / "google_places_api_key").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _post(body: dict, key: str) -> dict:
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={
        "X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if e.code in (401, 403):
            raise PlacesError("Google rejected the Places API key (or the Places API isn't enabled)") from e
        if e.code == 429:
            raise PlacesError("Google Places quota reached") from e
        raise PlacesError(f"Google Places error {e.code}: {detail[:200]}") from e


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def search(query: str, near: str | None = None, region: str | None = None, open_now: bool = False,
           max_results: int = 6, radius_km: float = 15, post=_post, resolve=wx.resolve) -> dict:
    key = api_key()
    if not key:
        raise PlacesError("Google Places isn't set up (no API key)")
    center = resolve(near, region)  # no place given: home
    here = (center["latitude"], center["longitude"])
    body = {"textQuery": query[:200], "maxResultCount": max(1, min(20, int(max_results))),
            "locationBias": {"circle": {"center": {"latitude": here[0], "longitude": here[1]},
                                        "radius": max(500.0, min(50000.0, float(radius_km) * 1000))}}}
    if open_now:
        body["openNow"] = True
    data = post(body, key)
    today = datetime.now().weekday()  # Google lists hours Monday first
    places = []
    for p in data.get("places", []):
        if p.get("businessStatus") not in (None, "OPERATIONAL"):
            continue
        loc = p.get("location") or {}
        hours = (p.get("currentOpeningHours") or {}).get("weekdayDescriptions") or []
        places.append({
            "name": (p.get("displayName") or {}).get("text"),
            "type": (p.get("primaryTypeDisplayName") or {}).get("text"),
            "rating": p.get("rating"), "reviews": p.get("userRatingCount"),
            "price": PRICE.get(p.get("priceLevel"), None),
            "open_now": (p.get("currentOpeningHours") or {}).get("openNow"),
            "hours_today": hours[today].split(": ", 1)[-1] if len(hours) > today else None,
            "distance_km": round(km(here, (loc["latitude"], loc["longitude"])), 1) if loc else None,
            "address": p.get("formattedAddress"), "phone": p.get("nationalPhoneNumber"),
            "website": p.get("websiteUri"), "maps": p.get("googleMapsUri"),
        })
    return {"near": ", ".join(x for x in (center.get("name"), center.get("admin1")) if x),
            "places": places, "source": "Google Maps (Places API)"}


async def search_async(*a, **k) -> dict:
    return await asyncio.to_thread(search, *a, **k)
