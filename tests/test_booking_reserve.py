from datetime import date
from pathlib import Path

import pytest

from booking.model import (
    Guest,
    IdempotencyKey,
    Party,
    Reservation,
    ReservationCode,
    ReservationStatus,
    RoomId,
    Stay,
    fingerprint,
)
from booking.service import (
    Counter,
    DirectoryLedger,
    InFlight,
    NotDirect,
    SentKey,
    XmlRpcWire,
)

ROOM_ID = 329667
PLAN_EUROS = 100
CODE = "1765555311"
KEY = IdempotencyKey("0f3c9a")


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


def guest() -> Guest:
    return Guest(last_name="Ng", first_name="Panorama", email="p@example.com")


def stay() -> Stay:
    return Stay(date(2026, 12, 14), date(2026, 12, 15), Party(adults=2), 1)


def booking_payload(status: int, code: str = CODE, channel: object = 0) -> dict[str, object]:
    return {
        "reservation_code": code,
        "status": status,
        "channel_reservation_code": channel,
        "amount": float(PLAN_EUROS),
        "currency": "EUR",
        "date_arrival": "14/12/2026",
        "date_departure": "15/12/2026",
        "men": 2,
        "children": 0,
        "rooms": str(ROOM_ID),
        "customer_surname": "Ng",
        "customer_name": "Panorama",
        "customer_mail": "p@example.com",
    }


def install(proxy: FakeProxy, euros: dict[str, int] | None = None) -> dict[str, int]:
    state = euros if euros is not None else {"euros": PLAN_EUROS}
    cells = [
        {
            "closed_arrival": 0,
            "booked": 0,
            "max_stay": 0,
            "price": 999,
            "min_stay": 0,
            "closed_departure": "0",
            "avail": 2,
            "closed": 0,
            "min_stay_arrival": 0,
            "no_ota": 0,
        }
        for _ in range(2)
    ]
    proxy.handlers["fetch_rooms"] = lambda *args: (
        0,
        [
            {
                "id": ROOM_ID,
                "name": "Double",
                "occupancy": 2,
                "subroom": 0,
                "anchorate": 0,
            }
        ],
    )
    proxy.handlers["fetch_rooms_values"] = lambda *args: (0, {str(ROOM_ID): cells})
    proxy.handlers["fetch_plan_prices"] = lambda *args: (0, {str(ROOM_ID): [state["euros"]]})
    proxy.handlers["fetch_booking"] = lambda *args: (0, booking_payload(status=1, code=str(args[2])))
    proxy.handlers["new_reservation"] = lambda *args: (0, CODE)
    return state


def methods(proxy: FakeProxy, name: str) -> list[tuple[object, ...]]:
    return [args for method, args in proxy.calls if method == name]


def test_second_reserve_returns_the_same_code(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy)
    booked = Counter(XmlRpcWire("secret-token", 1, proxy), DirectoryLedger(tmp_path))
    first = booked.reserve(stay(), RoomId(ROOM_ID), guest(), KEY, date(2026, 10, 20))
    again = booked.reserve(stay(), RoomId(ROOM_ID), guest(), KEY, date(2026, 10, 20))

    assert isinstance(first, Reservation)
    assert isinstance(again, Reservation)
    assert first.code == CODE
    assert again.code == first.code
    created = methods(proxy, "new_reservation")
    assert len(created) == 1
    assert created[0][2:] == (
        "14/12/2026",
        "15/12/2026",
        {str(ROOM_ID): [1, "nb"]},
        {
            "lname": "Ng",
            "fname": "Panorama",
            "email": "p@example.com",
            "city": "",
            "phone": "",
            "street": "",
            "country": "",
            "arrival_hour": "",
            "notes": "",
        },
        float(PLAN_EUROS),
        "booking",
        0,
        {"idempotency_key": "0f3c9a"},
        {"men": 2, "children": 0},
        0,
        0,
        1,
    )


def test_price_change_keeps_the_same_reservation(tmp_path: Path) -> None:
    proxy = FakeProxy()
    state = install(proxy)
    booked = Counter(XmlRpcWire("secret-token", 1, proxy), DirectoryLedger(tmp_path))
    first = booked.reserve(stay(), RoomId(ROOM_ID), guest(), KEY, date(2026, 10, 20))
    state["euros"] = PLAN_EUROS + 80
    again = booked.reserve(stay(), RoomId(ROOM_ID), guest(), KEY, date(2026, 10, 20))

    assert isinstance(first, Reservation)
    assert isinstance(again, Reservation)
    assert again.code == first.code
    assert len(methods(proxy, "new_reservation")) == 1


def test_sent_key_with_no_match_stays_in_flight(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy)
    proxy.handlers["fetch_bookings"] = lambda *args: (0, [])
    ledger = DirectoryLedger(tmp_path)
    lock = ledger.lock(KEY)
    lock.write(SentKey(fingerprint(stay(), RoomId(ROOM_ID), guest())))
    lock.release()
    result = Counter(XmlRpcWire("secret-token", 1, proxy), ledger).reserve(
        stay(),
        RoomId(ROOM_ID),
        guest(),
        KEY,
        date(2026, 10, 20),
    )

    assert isinstance(result, InFlight)
    assert result.key == KEY
    assert methods(proxy, "new_reservation") == []
    assert methods(proxy, "fetch_bookings") == [
        ("secret-token", 1, "14/12/2026", "14/12/2026", 0, 1),
    ]


def test_full_booking_page_does_not_create(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy)
    proxy.handlers["fetch_bookings"] = lambda *args: (
        0,
        [
            {"reservation_code": str(index), "ancillary": {"idempotency_key": "other"}}
            for index in range(120)
        ],
    )
    result = Counter(XmlRpcWire("secret-token", 1, proxy), DirectoryLedger(tmp_path)).reserve(
        stay(),
        RoomId(ROOM_ID),
        guest(),
        KEY,
        date(2026, 10, 20),
    )

    assert isinstance(result, InFlight)
    assert methods(proxy, "new_reservation") == []


@pytest.mark.parametrize("status", [ReservationStatus.CANCELLED, ReservationStatus.CANCELLED_PENALTY])
def test_cancel_of_cancelled_reservation_does_not_call_wired(tmp_path: Path, status: ReservationStatus) -> None:
    proxy = FakeProxy()
    proxy.handlers["fetch_booking"] = lambda *args: (0, booking_payload(status=int(status)))
    proxy.handlers["cancel_reservation"] = lambda *args: (0, "ok")
    result = Counter(XmlRpcWire("secret-token", 1, proxy), DirectoryLedger(tmp_path)).cancel(
        ReservationCode(CODE)
    )

    assert isinstance(result, Reservation)
    assert result.status == status
    assert methods(proxy, "cancel_reservation") == []


def test_cancel_sends_voucher(tmp_path: Path) -> None:
    proxy = FakeProxy()
    seen = {"reads": 0}

    def fetch_booking(*args: object) -> tuple[int, object]:
        seen["reads"] += 1
        status = 1 if seen["reads"] == 1 else int(ReservationStatus.CANCELLED)
        return (0, booking_payload(status=status, code=str(args[2])))

    proxy.handlers["fetch_booking"] = fetch_booking
    proxy.handlers["cancel_reservation"] = lambda *args: (0, "ok")
    result = Counter(XmlRpcWire("secret-token", 1, proxy), DirectoryLedger(tmp_path)).cancel(
        ReservationCode(CODE),
        "late",
    )

    assert isinstance(result, Reservation)
    assert result.status == ReservationStatus.CANCELLED
    assert methods(proxy, "cancel_reservation") == [("secret-token", 1, CODE, "late", 1)]


def test_cancel_of_channel_booking_is_not_direct(tmp_path: Path) -> None:
    proxy = FakeProxy()
    proxy.handlers["fetch_booking"] = lambda *args: (0, booking_payload(status=1, channel="BDC-9"))
    proxy.handlers["cancel_reservation"] = lambda *args: (0, "ok")
    result = Counter(XmlRpcWire("secret-token", 1, proxy), DirectoryLedger(tmp_path)).cancel(
        ReservationCode(CODE),
        "late",
    )

    assert isinstance(result, NotDirect)
    assert result.code == CODE
    assert methods(proxy, "cancel_reservation") == []
