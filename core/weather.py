"""Weather from Open-Meteo: free, no key, about 0.2 s, exact numbers.

Open-Meteo blends national weather models (for Canada, Environment Canada's GEM
among them). Places are resolved to coordinates first, so "Whitby" means
Whitby, Ontario (Deval's home) unless another one is asked for.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HOME = {"name": "Whitby", "admin1": "Ontario", "country": "Canada", "country_code": "CA",
        "latitude": 43.88342, "longitude": -78.93287, "timezone": "America/Toronto"}

# WMO weather codes
CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


class WeatherError(RuntimeError):
    pass


def _get(url: str, params: dict, timeout: float = 10) -> dict:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": "jarvis-assistant"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _label(loc: dict) -> str:
    return ", ".join(x for x in (loc.get("name"), loc.get("admin1"), loc.get("country")) if x)


def resolve(place: str | None, region: str | None = None, fetch=_get) -> dict:
    """A place name (optionally with a region or country) -> one location with coordinates.

    No place, or Deval's home town without another region named, means home."""
    if not place or (place.strip().lower() in ("home", "whitby") and not region):
        return dict(HOME)
    name = place.split(",")[0].strip()
    hint = (region or ",".join(place.split(",")[1:])).strip().lower()
    results = fetch(GEOCODE_URL, {"name": name, "count": 10, "language": "en", "format": "json"}).get("results") or []
    if not results:
        raise WeatherError(f"no place called {place!r} found")

    def score(r: dict) -> tuple:
        fields = " ".join(str(r.get(k) or "") for k in ("admin1", "admin2", "country", "country_code")).lower()
        matches_hint = bool(hint) and all(part.strip() in fields for part in hint.split(",") if part.strip())
        in_home_country = r.get("country_code") == HOME["country_code"]
        return (matches_hint, in_home_country, r.get("population") or 0)

    best = max(results, key=score)
    if hint and not score(best)[0]:
        raise WeatherError(f"no {name} in {hint} found; places called {name}: "
                           + "; ".join(_label(r) for r in results[:6]))
    return {k: best.get(k) for k in ("name", "admin1", "country", "country_code", "latitude", "longitude", "timezone")}


def forecast(loc: dict, days: int = 7, hourly_hours: int = 0, fetch=_get) -> dict:
    days = max(1, min(16, int(days)))
    params = {
        "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto", "forecast_days": days,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,"
                   "wind_speed_10m,wind_gusts_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_min,"
                 "precipitation_probability_max,precipitation_sum,snowfall_sum,wind_speed_10m_max,"
                 "wind_gusts_10m_max,sunrise,sunset",
    }
    if hourly_hours:
        params["hourly"] = "temperature_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m"
        params["forecast_hours"] = max(1, min(72, int(hourly_hours)))
    data = fetch(FORECAST_URL, params)
    cur, d = data["current"], data["daily"]
    out = {
        "location": _label(loc), "latitude": loc["latitude"], "longitude": loc["longitude"],
        "timezone": data.get("timezone"),
        "units": {"temperature": "°C", "precipitation": "mm", "snowfall": "cm", "wind": "km/h"},
        "now": {"time": cur["time"], "temperature": cur["temperature_2m"], "feels_like": cur["apparent_temperature"],
                "conditions": CODES.get(cur["weather_code"], f"code {cur['weather_code']}"),
                "humidity_pct": cur["relative_humidity_2m"], "precipitation_mm": cur["precipitation"],
                "wind": cur["wind_speed_10m"], "gusts": cur["wind_gusts_10m"]},
        "days": [
            {"date": d["time"][i], "conditions": CODES.get(d["weather_code"][i], f"code {d['weather_code'][i]}"),
             "high": d["temperature_2m_max"][i], "low": d["temperature_2m_min"][i],
             "feels_like_low": d["apparent_temperature_min"][i],
             "precip_chance_pct": d["precipitation_probability_max"][i], "rain_mm": d["precipitation_sum"][i],
             "snow_cm": d["snowfall_sum"][i], "wind_max": d["wind_speed_10m_max"][i],
             "gusts_max": d["wind_gusts_10m_max"][i], "sunrise": d["sunrise"][i][-5:], "sunset": d["sunset"][i][-5:]}
            for i in range(len(d["time"]))
        ],
        "source": "Open-Meteo (national weather models, incl. Environment Canada's GEM for Canada)",
    }
    if hourly_hours and data.get("hourly"):
        h = data["hourly"]
        out["hours"] = [{"time": h["time"][i], "temperature": h["temperature_2m"][i],
                         "precip_chance_pct": h["precipitation_probability"][i], "precip_mm": h["precipitation"][i],
                         "conditions": CODES.get(h["weather_code"][i], ""), "wind": h["wind_speed_10m"][i]}
                        for i in range(len(h["time"]))]
    return out


async def get_weather(place: str | None = None, region: str | None = None, days: int = 7,
                      hourly_hours: int = 0) -> dict:
    def run():
        loc = resolve(place, region)
        return forecast(loc, days, hourly_hours)

    return await asyncio.to_thread(run)
