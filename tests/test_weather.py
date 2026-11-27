import pytest

from core import weather as wx

PLACES = {"results": [
    {"name": "Whitby", "admin1": "Ontario", "country": "Canada", "country_code": "CA", "latitude": 43.88, "longitude": -78.93, "population": 138501, "timezone": "America/Toronto"},
    {"name": "Whitby", "admin1": "England", "country": "United Kingdom", "country_code": "GB", "latitude": 54.49, "longitude": -0.61, "population": 13213, "timezone": "Europe/London"},
    {"name": "London", "admin1": "England", "country": "United Kingdom", "country_code": "GB", "latitude": 51.5, "longitude": -0.12, "population": 8900000},
    {"name": "London", "admin1": "Ontario", "country": "Canada", "country_code": "CA", "latitude": 42.98, "longitude": -81.25, "population": 400000},
]}


def fake(url, params):
    name = params.get("name")
    return {"results": [r for r in PLACES["results"] if r["name"] == name]}


def test_home_needs_no_lookup():
    def boom(*a):
        raise AssertionError("no network for home")
    assert wx.resolve(None, fetch=boom)["admin1"] == "Ontario"
    assert wx.resolve("Whitby", fetch=boom)["latitude"] == wx.HOME["latitude"]


def test_region_picks_the_right_place():
    assert wx.resolve("Whitby", "England", fetch=fake)["country_code"] == "GB"
    assert wx.resolve("London, England", fetch=fake)["country_code"] == "GB"
    assert wx.resolve("London", "Ontario", fetch=fake)["admin1"] == "Ontario"


def test_without_region_prefers_canada():
    assert wx.resolve("London", fetch=fake)["admin1"] == "Ontario"


def test_unknown_region_lists_the_options():
    with pytest.raises(wx.WeatherError, match="places called Whitby"):
        wx.resolve("Whitby", "Texas", fetch=fake)


def test_forecast_shape():
    def fc(url, params):
        return {"timezone": "America/Toronto",
                "current": {"time": "t", "temperature_2m": 2.7, "apparent_temperature": -2.8, "relative_humidity_2m": 90,
                            "precipitation": 0.1, "weather_code": 51, "wind_speed_10m": 18.7, "wind_gusts_10m": 40},
                "daily": {"time": ["2026-11-27"], "weather_code": [63], "temperature_2m_max": [6.2], "temperature_2m_min": [1.7],
                          "apparent_temperature_min": [-2], "precipitation_probability_max": [100], "precipitation_sum": [24.2],
                          "snowfall_sum": [0], "wind_speed_10m_max": [30], "wind_gusts_10m_max": [60],
                          "sunrise": ["2026-11-27T07:14"], "sunset": ["2026-11-27T16:42"]}}
    out = wx.forecast(wx.HOME, 1, fetch=fc)
    assert out["location"] == "Whitby, Ontario, Canada"
    assert out["now"]["conditions"] == "light drizzle" and out["days"][0]["conditions"] == "rain"
    assert out["days"][0]["sunset"] == "16:42"
