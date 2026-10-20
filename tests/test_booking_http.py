from datetime import date

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from booking.http import create_app
from booking.service import Counter, DirectoryLedger, XmlRpcWire

TOKEN = "secret-token"
ROOM_ID = 329667
PRICE_PARENT = 555555
CATALOG_PRICE = 999


class FakeProxy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.handlers: dict[str, object] = {}

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def invoke(*args: object) -> object:
            self.calls.append((name, args))
            handler = self.handlers.get(name)
            if callable(handler):
                return handler(*args)
            return (0, [])

        return invoke


@pytest.fixture(autouse=True)
def pin_today(monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock(date):
        @classmethod
        def today(cls) -> date:
            return date(2026, 10, 20)

    monkeypatch.setattr("booking.service.date", Clock)


def client_for(tmp_path, proxy: FakeProxy) -> TestClient:
    counter = Counter(XmlRpcWire(TOKEN, 1, proxy), DirectoryLedger(tmp_path))
    return TestClient(create_app(counter))


def reservation_body() -> dict[str, object]:
    return {
        "check_in": "2026-12-14",
        "checkout": "2026-12-15",
        "room_id": ROOM_ID,
        "adults": 2,
        "children": 0,
        "quantity": 1,
        "guest": {
            "last_name": "Ng",
            "first_name": "Panorama",
            "email": "p@example.com",
        },
        "idempotency_key": "0f3c9a",
    }


def test_routes_are_the_five_jobs(tmp_path) -> None:
    app = create_app(Counter(XmlRpcWire(TOKEN, 1, FakeProxy()), DirectoryLedger(tmp_path)))
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert paths == {
        "/rooms",
        "/rooms/{room_id}/price",
        "/reservations",
        "/reservations/{code}",
        "/reservations/{code}/cancellation",
    }


def test_rooms_json_has_no_price_parent(tmp_path) -> None:
    proxy = FakeProxy()
    proxy.handlers["fetch_rooms"] = lambda *args: (
        0,
        [
            {
                "id": ROOM_ID,
                "name": "Double",
                "occupancy": 2,
                "subroom": 0,
                "anchorate": PRICE_PARENT,
                "price": CATALOG_PRICE,
                "shortname": "dbl",
            }
        ],
    )
    response = client_for(tmp_path, proxy).get("/rooms")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": ROOM_ID,
            "name": "Double",
            "occupancy": 2,
            "availability_room_id": ROOM_ID,
        }
    ]
    assert "price_room_id" not in response.text
    assert "anchorate" not in response.text
    assert str(PRICE_PARENT) not in response.text
    assert str(CATALOG_PRICE) not in response.text


def test_reservation_rejects_amount(tmp_path) -> None:
    proxy = FakeProxy()
    http = client_for(tmp_path, proxy)
    accepted = http.post("/reservations", json=reservation_body())
    rejected = http.post("/reservations", json={**reservation_body(), "amount": 100})

    assert accepted.status_code == 409
    assert accepted.json()["reason"] == "unknown_room"
    assert rejected.status_code == 422
    assert proxy.calls == [("fetch_rooms", (TOKEN, 1))]


def test_wired_status_and_token(tmp_path) -> None:
    cases = ((-9, 422), (-19, 422), (-18, 429), (-20, 429), (-21, 429), (-10, 502))
    for code, status in cases:
        proxy = FakeProxy()
        proxy.handlers["fetch_rooms"] = lambda *args, wired_code=code: (wired_code, f"fail {TOKEN} now")
        response = client_for(tmp_path, proxy).get("/rooms")
        assert response.status_code == status
        assert TOKEN not in response.text


def test_bad_payload_is_502(tmp_path) -> None:
    proxy = FakeProxy()
    proxy.handlers["fetch_rooms"] = lambda *args: (0, {"id": 1})
    response = client_for(tmp_path, proxy).get("/rooms")
    assert response.status_code == 502


def test_transport_failure_is_503(tmp_path) -> None:
    proxy = FakeProxy()

    def boom(*args: object) -> object:
        raise OSError("down")

    proxy.handlers["fetch_rooms"] = boom
    response = client_for(tmp_path, proxy).get("/rooms")
    assert response.status_code == 503
