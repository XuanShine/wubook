from datetime import date
from pathlib import Path

from booking.model import ClosedNight, Party, PastArrival, RoomId, Stay, TooLong
from booking.service import Board, Counter, DirectoryLedger, XmlRpcWire

TOKEN = "secret-token"
OPEN_ID = 329667
CLOSED_ID = 329668
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
) -> None:
    open_cells = [value_cell(closed=open_closed, avail=open_avail) for _ in range(NIGHTS)]
    open_cells.append(value_cell())
    shut_cells = [value_cell(closed=closed_closed, avail=closed_avail) for _ in range(NIGHTS)]
    shut_cells.append(value_cell())
    proxy.handlers["fetch_rooms"] = lambda *args: (
        0,
        [
            row(OPEN_ID, "Double", open_occupancy, PRICE_PARENT),
            row(CLOSED_ID, "Suite", closed_occupancy),
        ],
    )
    proxy.handlers["fetch_rooms_values"] = lambda *args: (
        0,
        {str(OPEN_ID): open_cells, str(CLOSED_ID): shut_cells},
    )
    proxy.handlers["fetch_plan_prices"] = lambda *args: (
        0,
        {str(PRICE_PARENT): [PLAN_EUROS] * NIGHTS, str(CLOSED_ID): [PLAN_EUROS] * NIGHTS},
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
