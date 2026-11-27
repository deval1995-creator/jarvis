import pytest

from core import places, weather


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(places, "api_key", lambda: "AIza-test")


def fake_post(body, key):
    return {"places": [
        {"displayName": {"text": "Balti"}, "location": {"latitude": 43.89, "longitude": -78.94}, "rating": 4.6,
         "userRatingCount": 3410, "priceLevel": "PRICE_LEVEL_MODERATE", "businessStatus": "OPERATIONAL",
         "currentOpeningHours": {"openNow": True, "weekdayDescriptions": [f"Day{i}: 11:00 AM - 10:00 PM" for i in range(7)]}},
        {"displayName": {"text": "Gone"}, "location": {"latitude": 43.9, "longitude": -78.9}, "businessStatus": "CLOSED_PERMANENTLY"},
    ]}


def test_home_search_shape(key):
    seen = {}

    def post(body, k):
        seen.update(body)
        return fake_post(body, k)

    out = places.search("Indian restaurant", open_now=True, post=post)
    assert out["near"] == "Whitby, Ontario"
    c = seen["locationBias"]["circle"]["center"]
    assert (c["latitude"], c["longitude"]) == (weather.HOME["latitude"], weather.HOME["longitude"]) and seen["openNow"]
    [p] = out["places"]  # permanently closed ones are dropped
    assert p["name"] == "Balti" and p["price"] == "$$" and p["open_now"] and p["hours_today"] == "11:00 AM - 10:00 PM"
    assert 0 < p["distance_km"] < 2


def test_no_key(monkeypatch):
    monkeypatch.setattr(places, "api_key", lambda: None)
    with pytest.raises(places.PlacesError, match="isn't set up"):
        places.search("x")
