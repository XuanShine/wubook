from datetime import date
from pathlib import Path

import pytest

from booking.model import (
    Cents,
    ClosedDeparture,
    ClosedNight,
    Night,
    Offer,
    Party,
    PastArrival,
    Room,
    RoomId,
    RoomStay,
    Stay,
    TooLong,
    quote,
)
from booking.service import Counter, DirectoryLedger, XmlRpcWire

ROOM_ID = 329667
PLAN_EUROS = 100
EMBEDDED_PRICE = 999


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


def room_row() -> dict[str, object]:
    return {
        "id": ROOM_ID,
        "name": "Double",
        "occupancy": 2,
        "subroom": 0,
        "anchorate": 0,
        "price": EMBEDDED_PRICE,
    }


def value_cell(closed_departure: object = "0") -> dict[str, object]:
    return {
        "closed_arrival": 0,
        "booked": 0,
        "max_stay": 0,
        "price": EMBEDDED_PRICE,
        "min_stay": 0,
        "closed_departure": closed_departure,
        "avail": 2,
        "closed": 0,
        "min_stay_arrival": 0,
        "no_ota": 0,
    }


def install(proxy: FakeProxy, nights: int, departure: object = "0", euros: int = PLAN_EUROS) -> None:
    cells = [value_cell() for _ in range(nights)]
    cells.append(value_cell(closed_departure=departure))
    proxy.handlers["fetch_rooms"] = lambda *args: (0, [room_row()])
    proxy.handlers["fetch_rooms_values"] = lambda *args: (0, {str(ROOM_ID): cells})
    proxy.handlers["fetch_plan_prices"] = lambda *args: (0, {str(ROOM_ID): [euros] * nights})


def counter(tmp_path: Path, proxy: FakeProxy) -> Counter:
    return Counter(XmlRpcWire("secret-token", 1, proxy), DirectoryLedger(tmp_path))


def calls(proxy: FakeProxy, method: str) -> list[tuple[object, ...]]:
    return [args for name, args in proxy.calls if name == method]


def test_values_and_prices_use_different_ends(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy, nights=2)
    stay = Stay(date(2026, 12, 14), date(2026, 12, 16), Party(adults=2), 1)
    quoted = counter(tmp_path, proxy).price(stay, RoomId(ROOM_ID), date(2026, 10, 20))

    assert isinstance(quoted, Offer)
    assert calls(proxy, "fetch_rooms_values") == [
        ("secret-token", 1, "14/12/2026", "16/12/2026"),
    ]
    assert calls(proxy, "fetch_plan_prices") == [
        ("secret-token", 1, 0, "14/12/2026", "15/12/2026"),
    ]


def test_plan_cents_ignore_the_values_price(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy, nights=1)
    stay = Stay(date(2026, 12, 14), date(2026, 12, 15), Party(adults=2), 1)
    quoted = counter(tmp_path, proxy).price(stay, RoomId(ROOM_ID), date(2026, 10, 20))

    assert isinstance(quoted, Offer)
    assert quoted.nights == 1
    assert quoted.total_cents == PLAN_EUROS * 100
    assert quoted.total_cents != EMBEDDED_PRICE * 100
    assert calls(proxy, "fetch_rooms_values") != []
    assert calls(proxy, "fetch_plan_prices") != []


def test_closed_departure_string_zero_does_not_refuse(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy, nights=1, departure="0")
    stay = Stay(date(2026, 12, 14), date(2026, 12, 15), Party(adults=2), 1)
    quoted = counter(tmp_path, proxy).price(stay, RoomId(ROOM_ID), date(2026, 10, 20))

    assert isinstance(quoted, Offer)


def test_closed_departure_on_checkout_morning_refuses(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy, nights=1, departure=1)
    checkout = date(2026, 12, 15)
    stay = Stay(date(2026, 12, 14), checkout, Party(adults=2), 1)
    quoted = counter(tmp_path, proxy).price(stay, RoomId(ROOM_ID), date(2026, 10, 20))

    assert isinstance(quoted, ClosedDeparture)
    assert quoted.day == checkout


def test_past_arrival_does_not_call_wired(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy, nights=1)
    stay = Stay(date(2020, 1, 1), date(2020, 1, 3), Party(adults=2), 1)
    quoted = counter(tmp_path, proxy).price(stay, RoomId(ROOM_ID), date(2026, 10, 20))

    assert isinstance(quoted, PastArrival)
    assert proxy.calls == []


def test_too_long_does_not_call_wired(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy, nights=1)
    stay = Stay(date(2026, 12, 14), date(2028, 12, 15), Party(adults=2), 1)
    quoted = counter(tmp_path, proxy).price(stay, RoomId(ROOM_ID), date(2026, 10, 20))

    assert isinstance(quoted, TooLong)
    assert proxy.calls == []


def test_closed_night_wins_before_availability() -> None:
    day = date(2026, 12, 14)
    stay = Stay(day, date(2026, 12, 15), Party(adults=2), 1)
    room = Room(RoomId(ROOM_ID), "Double", 2, RoomId(ROOM_ID), RoomId(ROOM_ID))
    night = Night(day, available=0, closed=True, closed_arrival=False, min_stay=0, max_stay=0, min_stay_arrival=0)
    facts = RoomStay((night,), False, (Cents(PLAN_EUROS * 100),))

    quoted = quote(stay, room, facts)

    assert isinstance(quoted, ClosedNight)
    assert quoted.days == (day,)
