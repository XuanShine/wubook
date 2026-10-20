from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from booking.guest import (
    AskingContact,
    AskingStay,
    BlockKind,
    Blocked,
    ChoosingExtras,
    ChoosingRoom,
    Done,
    load_extras,
    step,
)
from booking.http import create_app
from booking.model import ClosedNight, Party, PastArrival, RoomId, Stay, TooLong
from booking.service import Board, Counter, DirectoryLedger, XmlRpcWire

TOKEN = "secret-token"
OPEN_ID = 329667
CLOSED_ID = 329668
HIDDEN_ID = 562928
CODE = "1765555311"
PRICE_PARENT = 555555
PLAN_EUROS = 100
NIGHTS = 1
QUANTITY = 1
TODAY = date(2026, 10, 20)
CHECK_IN = date(2026, 12, 14)
CHECKOUT = date(2026, 12, 15)


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


def value_cell(closed: object = 0, avail: int = 2) -> dict[str, object]:
    return {
        "closed_arrival": 0,
        "booked": 0,
        "max_stay": 0,
        "price": 999,
        "min_stay": 0,
        "closed_departure": "0",
        "avail": avail,
        "closed": closed,
        "min_stay_arrival": 0,
        "no_ota": 0,
    }


def row(room_id: int, name: str, occupancy: int, anchor: int = 0) -> dict[str, object]:
    return {
        "id": room_id,
        "name": name,
        "occupancy": occupancy,
        "subroom": 0,
        "anchorate": anchor,
        "price": 999,
    }


def install(
    proxy: FakeProxy,
    *,
    open_closed: object = 0,
    open_avail: int = 2,
    closed_closed: object = 1,
    closed_avail: int = 2,
    open_occupancy: int = 2,
    closed_occupancy: int = 2,
    include_hidden: bool = False,
) -> None:
    open_cells = [value_cell(closed=open_closed, avail=open_avail) for _ in range(NIGHTS)]
    open_cells.append(value_cell())
    shut_cells = [value_cell(closed=closed_closed, avail=closed_avail) for _ in range(NIGHTS)]
    shut_cells.append(value_cell())
    catalog = [
        row(OPEN_ID, "Double", open_occupancy, PRICE_PARENT),
        row(CLOSED_ID, "Suite", closed_occupancy),
    ]
    values = {str(OPEN_ID): open_cells, str(CLOSED_ID): shut_cells}
    prices: dict[str, list[int]] = {
        str(PRICE_PARENT): [PLAN_EUROS] * NIGHTS,
        str(CLOSED_ID): [PLAN_EUROS] * NIGHTS,
    }
    if include_hidden:
        catalog.append(row(HIDDEN_ID, "Airbnb", 2))
        values[str(HIDDEN_ID)] = list(open_cells)
        prices[str(HIDDEN_ID)] = [PLAN_EUROS] * NIGHTS
    proxy.handlers["fetch_rooms"] = lambda *args: (0, catalog)
    proxy.handlers["fetch_rooms_values"] = lambda *args: (0, values)
    proxy.handlers["fetch_plan_prices"] = lambda *args: (0, prices)


def install_booking(proxy: FakeProxy, *, include_hidden: bool = False) -> None:
    install(proxy, include_hidden=include_hidden)
    proxy.handlers["new_reservation"] = lambda *args: (0, CODE)
    proxy.handlers["fetch_booking"] = lambda *args: (
        0,
        {
            "reservation_code": CODE,
            "status": 1,
            "channel_reservation_code": 0,
            "amount": float(PLAN_EUROS),
            "currency": "EUR",
            "date_arrival": "14/12/2026",
            "date_departure": "15/12/2026",
            "men": 1,
            "children": 0,
            "rooms": str(OPEN_ID),
            "customer_surname": "Ng",
            "customer_name": "Panorama",
            "customer_mail": "p@example.com",
        },
    )


def counter(tmp_path: Path, proxy: FakeProxy) -> Counter:
    return Counter(XmlRpcWire(TOKEN, 1, proxy), DirectoryLedger(tmp_path))


def stay(quantity: int = QUANTITY, adults: int = 2) -> Stay:
    return Stay(CHECK_IN, CHECKOUT, Party(adults=adults), quantity)


def methods(proxy: FakeProxy, name: str) -> list[tuple[object, ...]]:
    return [args for method, args in proxy.calls if method == name]


def test_bookable_rooms_quotes_every_room_from_one_pull(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy)
    board = counter(tmp_path, proxy).bookable_rooms(stay(), TODAY)

    assert isinstance(board, Board)
    assert [room.name for room in board.rooms] == ["Double"]
    assert board.rooms[0].id == RoomId(OPEN_ID)
    assert board.rooms[0].total_cents == PLAN_EUROS * 100 * NIGHTS * QUANTITY
    assert board.rooms[0].nights == NIGHTS
    assert board.rooms[0].quantity == QUANTITY
    assert board.block is None
    assert methods(proxy, "fetch_rooms") == [(TOKEN, 1)]
    assert methods(proxy, "fetch_rooms_values") == [(TOKEN, 1, "14/12/2026", "15/12/2026")]
    assert methods(proxy, "fetch_plan_prices") == [(TOKEN, 1, 0, "14/12/2026", "14/12/2026")]
    assert methods(proxy, "new_reservation") == []


def test_past_arrival_and_too_long_skip_wired(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy)
    booked = counter(tmp_path, proxy)
    past = booked.bookable_rooms(Stay(date(2020, 1, 1), date(2020, 1, 3), Party(adults=2), 1), TODAY)
    late = booked.bookable_rooms(Stay(date(2026, 12, 14), date(2028, 12, 15), Party(adults=2), 1), TODAY)

    assert isinstance(past, PastArrival)
    assert isinstance(late, TooLong)
    assert proxy.calls == []


def test_equal_refusals_become_the_block(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy, open_closed=1, closed_closed=1)
    board = counter(tmp_path, proxy).bookable_rooms(stay(), TODAY)

    assert isinstance(board, Board)
    assert board.rooms == ()
    assert board.block == ClosedNight((CHECK_IN,))
    assert len(methods(proxy, "fetch_rooms_values")) == 1
    assert len(methods(proxy, "fetch_plan_prices")) == 1


def test_mixed_refusals_leave_the_block_empty(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy, open_closed=1, closed_closed=0, closed_avail=0)
    board = counter(tmp_path, proxy).bookable_rooms(stay(), TODAY)

    assert isinstance(board, Board)
    assert board.rooms == ()
    assert board.block is None
    assert len(methods(proxy, "fetch_rooms_values")) == 1
    assert methods(proxy, "new_reservation") == []


def stay_form(**extra: object) -> dict[str, object]:
    form: dict[str, object] = {
        "check_in": "2026-12-14",
        "checkout": "2026-12-15",
        "adults": "2",
        "children": "0",
    }
    form.update(extra)
    return form


def contact_fields(**extra: object) -> dict[str, object]:
    form = stay_form(
        action="contact",
        room_id=str(OPEN_ID),
        room_name="Double",
        last_name="Ng",
        first_name="Panorama",
        email="p@example.com",
        phone="+33420000000",
        arrival_hour="18:00",
        remarks="",
        key="abc123",
    )
    form.update(extra)
    return form


def test_step_lists_the_open_room_and_the_shared_refusal(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy)
    shown = step(counter(tmp_path, proxy), stay_form(action="stay"), TODAY)

    assert isinstance(shown, ChoosingRoom)
    assert [room.name for room in shown.rooms] == ["Double"]
    assert shown.rooms[0].total_cents == PLAN_EUROS * 100 * NIGHTS * QUANTITY
    assert shown.sentence == ""
    assert len(methods(proxy, "fetch_rooms_values")) == 1
    assert len(methods(proxy, "fetch_plan_prices")) == 1
    assert methods(proxy, "new_reservation") == []

    proxy = FakeProxy()
    install(proxy, open_closed=1, closed_closed=1)
    blocked = step(counter(tmp_path, proxy), stay_form(action="stay"), TODAY)
    assert isinstance(blocked, ChoosingRoom)
    assert blocked.rooms == ()
    assert blocked.sentence == "La chambre est fermée sur ce séjour."

    proxy = FakeProxy()
    install(proxy, open_closed=1, closed_closed=0, closed_avail=0)
    mixed = step(counter(tmp_path, proxy), stay_form(action="stay"), TODAY)
    assert isinstance(mixed, ChoosingRoom)
    assert mixed.rooms == ()
    assert mixed.sentence == "Aucune chambre n'est réservable pour ces dates."


def test_step_hides_configured_rooms_and_does_not_reserve_them(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install_booking(proxy, include_hidden=True)
    booked = counter(tmp_path, proxy)
    board = booked.bookable_rooms(stay(), TODAY)
    assert isinstance(board, Board)
    assert HIDDEN_ID in [int(room.id) for room in board.rooms]

    shown = step(booked, stay_form(action="stay"), TODAY)
    assert isinstance(shown, ChoosingRoom)
    assert HIDDEN_ID not in [int(room.id) for room in shown.rooms]
    assert [room.name for room in shown.rooms] == ["Double"]

    posted = step(booked, stay_form(action="room", room_id=str(HIDDEN_ID)), TODAY)
    assert isinstance(posted, ChoosingRoom)
    assert posted.sentence == "Cette chambre n'est pas proposée."
    assert methods(proxy, "new_reservation") == []


def test_missing_extras_file_still_shows_the_step(tmp_path: Path) -> None:
    assert load_extras(tmp_path / "missing.yml") == ()
    proxy = FakeProxy()
    shown = step(
        counter(tmp_path, proxy),
        stay_form(action="room", room_id=str(OPEN_ID), room_name="Double"),
        TODAY,
        extras_path=tmp_path / "missing.yml",
    )

    assert isinstance(shown, ChoosingExtras)
    assert shown.catalog == ()
    assert shown.notice == "Aucun extra n'est proposé pour le moment."
    assert proxy.calls == []


def test_broken_extras_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "extras.yml"
    path.write_text(":\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_extras(path)


def test_shipped_extras_are_the_hotel_rows() -> None:
    assert [(extra.id, extra.label, int(extra.cents), extra.per) for extra in load_extras()] == [
        ("petit_dejeuner", "Petit-déjeuner", 1400, "person_night"),
        ("parking", "Parking", 1500, "stay"),
        ("lit_bebe", "Lit bébé", 1000, "stay"),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phone", ""),
        ("phone", "12345"),
        ("arrival_hour", ""),
        ("arrival_hour", "25:00"),
        ("email", "panorama.example"),
        ("email", "a@b"),
        ("last_name", ""),
        ("remarks", "x" * 501),
    ],
)
def test_contact_rules_stop_before_reserve(tmp_path: Path, field: str, value: str) -> None:
    proxy = FakeProxy()
    shown = step(counter(tmp_path, proxy), contact_fields(**{field: value}), TODAY)

    assert isinstance(shown, AskingContact)
    assert shown.key == "abc123"
    assert field in [name for name, _message in shown.errors]
    assert proxy.calls == []


def test_empty_key_does_not_reserve(tmp_path: Path) -> None:
    proxy = FakeProxy()
    shown = step(counter(tmp_path, proxy), contact_fields(key=""), TODAY)

    assert isinstance(shown, AskingContact)
    assert shown.key != ""
    assert "key" in [name for name, _message in shown.errors]
    assert proxy.calls == []


def test_extras_and_contact_do_not_pull_the_grid(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy)
    booked = counter(tmp_path, proxy)
    found = step(booked, stay_form(action="stay", adults="1"), TODAY)
    assert isinstance(found, ChoosingRoom)
    pulls = len(methods(proxy, "fetch_rooms_values"))

    picked = step(booked, stay_form(action="room", adults="1", room_id=str(OPEN_ID), room_name="Double"), TODAY)
    assert isinstance(picked, ChoosingExtras)
    asked = step(
        booked,
        stay_form(
            action="extras",
            adults="1",
            room_id=str(OPEN_ID),
            extra=["petit_dejeuner", "parking"],
        ),
        TODAY,
    )
    assert isinstance(asked, AskingContact)
    assert len(asked.key) == 32
    held = step(
        booked,
        contact_fields(adults="1", key=str(asked.key), phone=""),
        TODAY,
    )
    assert isinstance(held, AskingContact)
    assert held.key == asked.key
    assert len(methods(proxy, "fetch_rooms_values")) == pulls
    assert methods(proxy, "new_reservation") == []


def test_back_from_contact_drops_the_key_without_another_pull(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install(proxy)
    booked = counter(tmp_path, proxy)
    step(booked, stay_form(action="stay"), TODAY)
    pulls = len(methods(proxy, "fetch_rooms_values"))
    asked = step(booked, stay_form(action="extras", room_id=str(OPEN_ID), extra="parking"), TODAY)
    assert isinstance(asked, AskingContact)
    backed = step(
        booked,
        stay_form(action="back", room_id=str(OPEN_ID), extra="parking", key=str(asked.key)),
        TODAY,
    )

    assert isinstance(backed, ChoosingExtras)
    assert len(methods(proxy, "fetch_rooms_values")) == pulls


def test_place_charges_the_room_and_keeps_one_reservation(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install_booking(proxy)
    booked = counter(tmp_path, proxy)
    found = step(booked, stay_form(action="stay", adults="1"), TODAY)
    assert isinstance(found, ChoosingRoom)
    assert [room.name for room in found.rooms] == ["Double"]
    asked = step(
        booked,
        stay_form(action="extras", adults="1", room_id=str(OPEN_ID), extra=["petit_dejeuner", "parking"]),
        TODAY,
    )
    assert isinstance(asked, AskingContact)
    form = contact_fields(
        adults="1",
        key=str(asked.key),
        extra=["petit_dejeuner", "parking"],
        amount="1",
        total_cents="1",
        currency="USD",
    )
    done = step(booked, form, TODAY)
    again = step(booked, form, TODAY)

    assert isinstance(done, Done)
    assert isinstance(again, Done)
    assert str(done.reservation.code) == CODE
    assert again.reservation.code == done.reservation.code
    assert done.reservation.total_cents == PLAN_EUROS * 100
    created = methods(proxy, "new_reservation")
    assert len(created) == 1
    assert created[0][6] == float(PLAN_EUROS)
    assert created[0][5]["notes"] == (
        "Extras à régler à l'hôtel : Petit-déjeuner (14,00 €), Parking (15,00 €)"
    )
    assert created[0][5]["phone"] == "+33420000000"
    assert created[0][9] == {"idempotency_key": str(asked.key)}


def test_inflight_back_stays_blocked(tmp_path: Path) -> None:
    proxy = FakeProxy()
    install_booking(proxy)
    proxy.handlers["fetch_bookings"] = lambda *args: (
        0,
        [{"reservation_code": str(index), "ancillary": {"idempotency_key": "other"}} for index in range(120)],
    )
    booked = counter(tmp_path, proxy)
    form = contact_fields(extra="parking")
    blocked = step(booked, form, TODAY)
    calls = len(proxy.calls)
    backed = step(booked, {**form, "action": "back", "outcome": "inflight"}, TODAY)

    assert isinstance(blocked, Blocked)
    assert blocked.kind is BlockKind.IN_FLIGHT
    assert blocked.allow_back is False
    assert blocked.french == "Votre demande est déjà en cours. Aucune seconde réservation ne sera créée."
    assert isinstance(backed, Blocked)
    assert backed.kind is BlockKind.IN_FLIGHT
    assert backed.key == form["key"]
    assert methods(proxy, "new_reservation") == []
    assert len(proxy.calls) == calls


def test_json_reservation_without_a_phone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock(date):
        @classmethod
        def today(cls) -> date:
            return TODAY

    proxy = FakeProxy()
    install_booking(proxy)
    http = TestClient(create_app(counter(tmp_path, proxy)))
    monkeypatch.setattr("booking.http.date", Clock)
    response = http.post(
        "/reservations",
        json={
            "check_in": "2026-12-14",
            "checkout": "2026-12-15",
            "room_id": OPEN_ID,
            "adults": 2,
            "children": 0,
            "quantity": 1,
            "guest": {"last_name": "Ng", "first_name": "Panorama", "email": "p@example.com"},
            "idempotency_key": "json-no-phone",
        },
    )

    assert response.status_code == 200
    assert response.json()["code"] == CODE
    assert response.json()["guest"]["phone"] == ""
    created = methods(proxy, "new_reservation")
    assert len(created) == 1
    assert created[0][5]["phone"] == ""
    assert created[0][6] == float(PLAN_EUROS)
