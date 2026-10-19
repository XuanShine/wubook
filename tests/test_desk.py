from datetime import date

import pytest

from comptoir.desk import Desk, ReservationOrder
from comptoir.quote import Guest, IdempotencyKey, Origin, RoomId, Stay

IN = date(2026, 10, 21)
OUT = date(2026, 10, 22)


def room_raw(rid, name, occupancy=2, subroom=0, anchorate=0):
    return {
        "id": rid,
        "name": name,
        "occupancy": occupancy,
        "subroom": subroom,
        "anchorate": anchorate,
        "price": 999,
        "min_price": 45,
        "max_price": 500,
        "board": "bb",
        "shortname": "x",
        "men": 2,
        "children": 0,
        "woodoo": 0,
    }


def day(avail=2, **extra):
    cell = {
        "avail": avail,
        "closed": 0,
        "closed_arrival": 0,
        "closed_departure": "0",
        "min_stay": 0,
        "max_stay": 0,
        "min_stay_arrival": 0,
        "price": 999,
    }
    cell.update(extra)
    return cell


class FakeRpc:
    def __init__(self, rooms, values=None, prices=None):
        self.rooms = rooms
        self.values = values or {}
        self.prices = prices or {}
        self.calls = []
        self.fail = None
        self.bookings = []

    def fetch_rooms(self, token, lcode):
        self.calls.append(("fetch_rooms",))
        return [0, self.rooms]

    def fetch_rooms_values(self, token, lcode, dfrom, dto):
        self.calls.append(("fetch_rooms_values", dfrom, dto))
        return [0, self.values]

    def fetch_plan_prices(self, token, lcode, pid, dfrom, dto, rooms):
        self.calls.append(("fetch_plan_prices", pid, dfrom, dto, tuple(rooms)))
        return [0, self.prices]

    def new_reservation(
        self,
        token,
        lcode,
        dfrom,
        dto,
        rooms,
        customer,
        amount,
        origin,
        ccard,
        ancillary,
        guests,
        ignore_restrs,
        ignore_avail,
        status,
    ):
        record = {
            "dfrom": dfrom,
            "dto": dto,
            "rooms": rooms,
            "customer": customer,
            "amount": amount,
            "origin": origin,
            "ccard": ccard,
            "ancillary": ancillary,
            "guests": guests,
            "ignore_restrs": ignore_restrs,
            "ignore_avail": ignore_avail,
            "status": status,
        }
        self.calls.append(("new_reservation", record))
        if self.fail == "raise":
            raise ConnectionError("coupé")
        if self.fail == "wired":
            return [-1, "refusé par WuBook"]
        self.bookings.append({"rcode": "R42", "ancillary": dict(ancillary)})
        return [0, "R42"]

    def fetch_bookings(self, token, lcode, dfrom, dto, oncreated, ancillary):
        self.calls.append(("fetch_bookings", dfrom, dto, oncreated, ancillary))
        if ancillary:
            return [0, list(self.bookings)]
        hidden = [{key: value for key, value in row.items() if key != "ancillary"} for row in self.bookings]
        return [0, hidden]


def open_desk(tmp_path, rpc, sell):
    path = tmp_path / "sell.yml"
    path.write_text(sell, encoding="utf-8")
    return Desk(sell_path=path, receipt_path=tmp_path / "receipts.json", rpc=rpc)


def reserved_calls(rpc):
    return [call for call in rpc.calls if call[0] == "new_reservation"]


def test_one_night_window_ignores_the_values_price(tmp_path):
    rpc = FakeRpc(
        [room_raw(329667, "Double Balcon")],
        {"329667": [day(3), day(1)]},
        {"329667": [90]},
    )
    desk = open_desk(tmp_path, rpc, "329667: yes\n")
    stay = Stay(IN, OUT, adults=2, children=0)
    found = desk.search(stay)
    assert found[0].line() == "Double Balcon, 1 nuit, 90 euros"
    assert "999" not in found[0].line()
    assert ("fetch_rooms_values", "21/10/2026", "22/10/2026") in rpc.calls
    assert ("fetch_plan_prices", 0, "21/10/2026", "21/10/2026", (329667,)) in rpc.calls
    desk.close()


def test_virtual_avail_comes_from_the_parent_and_the_pool_is_spoken(tmp_path):
    rpc = FakeRpc(
        [
            room_raw(1, "Double Balcon"),
            room_raw(2, "Double Balcon Airbnb", subroom=1),
        ],
        {
            "1": [day(2), day(0)],
            "2": [day(0), day(0)],
        },
        {"1": [90], "2": [90]},
    )
    desk = open_desk(tmp_path, rpc, "1: yes\n2: yes\n")
    lines = [item.line() for item in desk.search(Stay(IN, OUT, 2, 0))]
    assert "Double Balcon, 1 nuit, 90 euros, même stock que Double Balcon Airbnb" in lines
    assert "Double Balcon Airbnb, 1 nuit, 90 euros, même stock que Double Balcon" in lines
    assert all("il reste" not in line for line in lines)
    desk.close()


def test_unsold_room_is_absent_and_refused_on_book(tmp_path):
    rpc = FakeRpc(
        [room_raw(329667, "Double Balcon")],
        {"329667": [day(3), day(1)]},
        {"329667": [90]},
    )
    desk = open_desk(tmp_path, rpc, "329667: no\n")
    assert desk.search(Stay(IN, OUT, 2, 0)) == ()
    booked = desk.book(
        Stay(IN, OUT, 2, 0),
        RoomId(329667),
        Guest(lname="Martin"),
        IdempotencyKey("k"),
        Origin.VOIX,
    )
    assert booked.line() == "Double Balcon refusée, pas en vente"
    assert reserved_calls(rpc) == []
    desk.close()


def test_closed_departure_is_read_on_the_checkout_morning(tmp_path):
    rpc = FakeRpc(
        [room_raw(329667, "Double Balcon")],
        {"329667": [day(2, closed_departure="0"), day(0, closed_departure="1")]},
        {"329667": [90]},
    )
    desk = open_desk(tmp_path, rpc, "329667: yes\n")
    line = desk.search(Stay(IN, OUT, 2, 0))[0].line()
    assert line == "Double Balcon refusée, départ fermé le 22 octobre"
    assert ("fetch_rooms_values", "21/10/2026", "22/10/2026") in rpc.calls
    desk.close()


def test_price_below_one_cent_is_missing_and_one_cent_is_kept(tmp_path):
    rpc = FakeRpc(
        [room_raw(329667, "Double Balcon")],
        {"329667": [day(2), day(1)]},
        {"329667": [0.009]},
    )
    desk = open_desk(tmp_path, rpc, "329667: yes\n")
    missing = desk.search(Stay(IN, OUT, 2, 0))[0].line()
    assert "prix manquant" in missing
    assert "0,01" not in missing
    rpc.prices = {"329667": [0.01]}
    kept = desk.search(Stay(date(2026, 10, 23), date(2026, 10, 24), 2, 0))[0].line()
    assert kept == "Double Balcon, 1 nuit, 0,01 euros"
    desk.close()


def test_second_book_replays_the_stored_code(tmp_path):
    rpc = FakeRpc(
        [room_raw(329667, "Double Balcon")],
        {"329667": [day(2), day(1)]},
        {"329667": [90]},
    )
    desk = open_desk(tmp_path, rpc, "329667: yes\n")
    stay = Stay(IN, OUT, 2, 0)
    guest = Guest(lname="Martin", email="m@example.com")
    key = IdempotencyKey("same")
    first = desk.book(stay, RoomId(329667), guest, key, Origin.GUICHET)
    second = desk.book(stay, RoomId(329667), guest, key, Origin.GUICHET)
    assert first.line() == "Réservé, code R42, Double Balcon, 1 nuit, 90 euros"
    assert second.line() == "Déjà réservé, code R42, Double Balcon, 1 nuit, 90 euros"
    assert len(reserved_calls(rpc)) == 1
    placed = reserved_calls(rpc)[0][1]
    assert placed["dfrom"] == "21/10/2026"
    assert placed["dto"] == "22/10/2026"
    assert placed["amount"] == 90
    assert placed["ancillary"] == {"idempotency_key": "same"}
    assert placed["rooms"] == {"329667": [1, "nb"]}
    assert placed["customer"] == {"lname": "Martin", "email": "m@example.com"}
    assert placed["ccard"] == 0
    assert placed["ignore_restrs"] == 0
    assert placed["ignore_avail"] == 0
    assert placed["status"] == 1
    assert placed["origin"] == "guichet"
    desk.close()


def test_sent_receipt_without_ancillary_match_does_not_book_again(tmp_path):
    rpc = FakeRpc(
        [room_raw(329667, "Double Balcon")],
        {"329667": [day(2), day(1)]},
        {"329667": [90]},
    )
    desk = open_desk(tmp_path, rpc, "329667: yes\n")
    stay = Stay(IN, OUT, 2, 0)
    guest = Guest(lname="Martin", email="m@example.com")
    key = IdempotencyKey("same")
    rpc.fail = "raise"
    with pytest.raises(ConnectionError):
        desk.book(stay, RoomId(329667), guest, key, Origin.VOIX)
    rpc.fail = None
    rpc.bookings = [
        {
            "rcode": "OTHER",
            "customer_name": "Martin",
            "ancillary": {"idempotency_key": "nope"},
        }
    ]
    again = desk.book(stay, RoomId(329667), guest, key, Origin.VOIX)
    assert again.line() == "envoi incertain, vérifiez WuBook."
    assert "OTHER" not in again.line()
    assert len(reserved_calls(rpc)) == 1
    lookups = [call for call in rpc.calls if call[0] == "fetch_bookings"]
    assert lookups[-1][3:] == (0, 1)
    desk.close()


def test_wired_error_is_spoken_and_a_later_book_can_retry(tmp_path):
    rpc = FakeRpc(
        [room_raw(329667, "Double Balcon")],
        {"329667": [day(2), day(1)]},
        {"329667": [90]},
    )
    desk = open_desk(tmp_path, rpc, "329667: yes\n")
    stay = Stay(IN, OUT, 2, 0)
    guest = Guest(lname="Martin")
    key = IdempotencyKey("same")
    rpc.fail = "wired"
    refused = desk.book(stay, RoomId(329667), guest, key, Origin.VOIX)
    assert refused.line() == "refusé par WuBook"
    rpc.fail = None
    booked = desk.book(stay, RoomId(329667), guest, key, Origin.VOIX)
    assert booked.line() == "Réservé, code R42, Double Balcon, 1 nuit, 90 euros"
    assert len(reserved_calls(rpc)) == 2
    desk.close()


def test_same_key_rejects_a_different_fingerprint(tmp_path):
    rpc = FakeRpc(
        [room_raw(329667, "Double Balcon")],
        {"329667": [day(2), day(1)]},
        {"329667": [90]},
    )
    desk = open_desk(tmp_path, rpc, "329667: yes\n")
    stay = Stay(IN, OUT, 2, 0)
    key = IdempotencyKey("same")
    desk.book(stay, RoomId(329667), Guest(lname="Martin", email="m@example.com"), key, Origin.VOIX)
    other = desk.book(stay, RoomId(329667), Guest(lname="Durand", email="m@example.com"), key, Origin.VOIX)
    assert other.line() == "clé déjà utilisée pour une autre demande."
    assert len(reserved_calls(rpc)) == 1
    desk.close()


def test_empty_key_and_unknown_room(tmp_path):
    rpc = FakeRpc([room_raw(329667, "Double Balcon")], {"329667": [day(), day()]}, {"329667": [90]})
    desk = open_desk(tmp_path, rpc, "329667: yes\n")
    stay = Stay(IN, OUT, 2, 0)
    assert desk.book(stay, RoomId(329667), Guest(lname="Martin"), IdempotencyKey("  "), Origin.VOIX).line() == (
        "clé manquante"
    )
    assert desk.book(stay, RoomId(404), Guest(lname="Martin"), IdempotencyKey("k"), Origin.VOIX).line() == (
        "chambre inconnue"
    )
    assert reserved_calls(rpc) == []
    desk.close()


def test_sell_file_keeps_other_keys_and_rejects_other_words(tmp_path):
    rpc = FakeRpc([room_raw(1, "Simple")], {"1": [day(), day()]}, {"1": [50]})
    desk = open_desk(tmp_path, rpc, "562928: no\n563031: no\n")
    assert [row.sell for row in desk.rooms()] == [False]
    desk.set_sell(RoomId(1), True)
    text = (tmp_path / "sell.yml").read_text(encoding="utf-8")
    assert text == "562928: no\n563031: no\n1: yes\n"
    assert desk.rooms()[0].sell is True
    desk.close()

    bad = tmp_path / "bad.yml"
    bad.write_text("1: true\n", encoding="utf-8")
    with pytest.raises(ValueError):
        Desk(sell_path=bad, receipt_path=tmp_path / "other.json", rpc=FakeRpc([room_raw(1, "Simple")]))


def test_wrong_span_type_is_a_type_error(tmp_path):
    rpc = FakeRpc([room_raw(1, "Simple")], {"1": [day(), day()]}, {"1": [50]})
    desk = open_desk(tmp_path, rpc, "1: yes\n")
    stay = Stay(IN, OUT, 2, 0)
    with pytest.raises(TypeError):
        desk._wired.fetch_plan_prices(stay.reservation_span(), ())
    with pytest.raises(TypeError):
        desk._wired.fetch_rooms_values(stay.reservation_span())
    order = ReservationOrder(stay, RoomId(1), 1, 5000, Guest(lname="Martin"), Origin.VOIX)
    with pytest.raises(TypeError):
        desk._wired.new_reservation(stay.price_span(), order, IdempotencyKey("k"))
    desk.close()
