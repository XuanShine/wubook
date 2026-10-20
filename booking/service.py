from __future__ import annotations

import fcntl
import hashlib
import json
import os
import xmlrpc.client
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from booking.model import (
    ArrivalDay,
    BookingWindow,
    Cents,
    Guest,
    IdempotencyKey,
    NightCell,
    ListedRoom,
    Offer,
    Party,
    PriceWindow,
    Refusal,
    Reservation,
    ReservationCode,
    ReservationStatus,
    Room,
    RoomId,
    RoomStay,
    Stay,
    UnknownRoom,
    ValuesWindow,
    WireShape,
    assemble,
    cents_from_amount,
    closed_flag,
    fingerprint,
    guest_from_booking,
    guest_to_customer,
    is_direct,
    night_cents,
    parent_id,
    past_arrival,
    quote,
    too_long,
    wire_amount,
)


class WiredRejected(Exception):
    """Negative Wired code. message has had the token removed."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class WiredDown(Exception):
    """Transport failure, or a return that was not [code, payload]."""


@dataclass(frozen=True)
class KeyUsed:
    key: IdempotencyKey


@dataclass(frozen=True)
class InFlight:
    """A previous attempt may have reached Wired. Retry this key. Do not mint another."""

    key: IdempotencyKey


@dataclass(frozen=True)
class Rejected:
    message: str
    wired_code: int


@dataclass(frozen=True)
class NotDirect:
    code: ReservationCode


@dataclass(frozen=True)
class OpenKey:
    fingerprint: str


@dataclass(frozen=True)
class SentKey:
    fingerprint: str


@dataclass(frozen=True)
class BookedKey:
    fingerprint: str
    code: ReservationCode


@dataclass(frozen=True)
class RejectedKey:
    fingerprint: str
    message: str
    wired_code: int


KeyRecord = OpenKey | SentKey | BookedKey | RejectedKey
ReserveResult = Reservation | Refusal | KeyUsed | InFlight | Rejected

_BOOKING_PAGE = 120
_WIRED_URL = "https://wired.wubook.net/xrws/"
PARITY_PLAN_ID: int = 0


class KeyLock(Protocol):
    def read(self) -> KeyRecord | None:
        ...

    def write(self, record: KeyRecord) -> None:
        ...

    def release(self) -> None:
        ...


class Ledger(Protocol):
    def lock(self, key: IdempotencyKey) -> KeyLock:
        """Exclusive lock for this key's file only. Other keys stay writable."""

        ...


class Wire(Protocol):
    def rooms(self) -> dict[RoomId, Room]:
        ...

    def room_stay(self, stay: Stay, room: Room) -> RoomStay:
        ...

    def book(
        self,
        window: BookingWindow,
        room: Room,
        quantity: int,
        guest: Guest,
        party: Party,
        total: Cents,
        key: IdempotencyKey,
    ) -> ReservationCode:
        """new_reservation. Board nb, status CONFIRMED, ccard 0, both ignore flags 0, origin booking."""

        ...

    def find_by_key(self, arrival: ArrivalDay, key: IdempotencyKey) -> ReservationCode | None:
        """fetch_bookings for this arrival day, oncreated 0, ancillary 1."""

        ...

    def reservation(self, code: ReservationCode) -> Reservation:
        ...

    def cancel(self, code: ReservationCode, reason: str) -> None:
        """cancel_reservation. send_voucher stays 1. reason is the remark."""

        ...


def _call(proxy: object, token: str, lcode: int, method: str, args: tuple[object, ...]) -> object:
    try:
        result = getattr(proxy, method)(token, lcode, *args)
    except Exception:
        raise WiredDown from None
    if isinstance(result, (str, bytes)) or not isinstance(result, (list, tuple)) or len(result) != 2:
        raise WiredDown
    code, payload = result
    if isinstance(code, bool) or not isinstance(code, int):
        raise WiredDown
    if code == 0:
        return payload
    if code < 0:
        message = payload if isinstance(payload, str) else ""
        if token:
            message = message.replace(token, "")
        raise WiredRejected(code, message)
    raise WiredDown


def _wire_date(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def _parse_wire_date(value: object) -> date:
    if not isinstance(value, str):
        raise WireShape("date")
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        raise WireShape("date") from None


def _plain_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer")
    return value


def values_window(stay: Stay) -> ValuesWindow:
    return ValuesWindow(stay.check_in, stay.checkout)


def price_window(stay: Stay) -> PriceWindow:
    return PriceWindow(stay.check_in, stay.checkout - timedelta(days=1))


def booking_window(stay: Stay) -> BookingWindow:
    return BookingWindow(stay.check_in, stay.checkout)


def arrival_day(stay: Stay) -> ArrivalDay:
    """The check-in date alone. Both fetch_bookings ends are this day."""

    return ArrivalDay(stay.check_in)


def _values_args(window: ValuesWindow) -> tuple[str, str]:
    return (_wire_date(window.check_in), _wire_date(window.checkout_morning))


def _price_args(window: PriceWindow) -> tuple[int, str, str]:
    return (PARITY_PLAN_ID, _wire_date(window.check_in), _wire_date(window.last_night))


def _booking_dates(window: BookingWindow) -> tuple[str, str]:
    return (_wire_date(window.arrival), _wire_date(window.departure))


def _arrival_args(day: ArrivalDay) -> tuple[str, str, int, int]:
    text = _wire_date(day.day)
    return (text, text, 0, 1)


def parse_catalog(payload: object) -> dict[RoomId, Room]:
    if not isinstance(payload, list):
        raise WireShape("rooms")
    rooms: dict[RoomId, Room] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise WireShape("room")
        try:
            room_id = RoomId(item["id"])
            name = item["name"]
            occupancy = item["occupancy"]
            if not isinstance(name, str):
                raise ValueError("name")
            if isinstance(occupancy, bool) or not isinstance(occupancy, int):
                raise ValueError("occupancy")
            availability = parent_id(room_id, item["subroom"])
            price_parent = parent_id(room_id, item["anchorate"])
        except (KeyError, ValueError):
            raise WireShape("room") from None
        if room_id in rooms:
            raise WireShape("room")
        rooms[room_id] = Room(room_id, name, occupancy, availability, price_parent)
    return rooms


def _night_cell(cell: object) -> NightCell:
    if not isinstance(cell, dict):
        raise WireShape("values")
    try:
        return NightCell(
            available=_plain_int(cell["avail"]),
            closed=closed_flag(cell["closed"]),
            closed_arrival=closed_flag(cell["closed_arrival"]),
            closed_departure=closed_flag(cell["closed_departure"]),
            min_stay=_plain_int(cell["min_stay"]),
            max_stay=_plain_int(cell["max_stay"]),
            min_stay_arrival=_plain_int(cell["min_stay_arrival"]),
        )
    except (KeyError, ValueError):
        raise WireShape("values") from None


def parse_values(payload: object, stay: Stay) -> dict[RoomId, tuple[NightCell, ...]]:
    """Each room's list includes the checkout-morning cell."""

    if not isinstance(payload, dict):
        raise WireShape("values")
    expected = stay.night_count() + 1
    parsed: dict[RoomId, tuple[NightCell, ...]] = {}
    for key, cells in payload.items():
        try:
            room_id = RoomId(key)
        except ValueError:
            raise WireShape("values") from None
        if not isinstance(cells, list) or len(cells) != expected:
            raise WireShape("values")
        parsed[room_id] = tuple(_night_cell(cell) for cell in cells)
    return parsed


def parse_prices(payload: object, stay: Stay) -> dict[RoomId, tuple[Cents | None, ...]]:
    if not isinstance(payload, dict):
        raise WireShape("prices")
    nights = stay.night_count()
    parsed: dict[RoomId, tuple[Cents | None, ...]] = {}
    for key, amounts in payload.items():
        try:
            room_id = RoomId(key)
        except ValueError:
            raise WireShape("prices") from None
        if not isinstance(amounts, list) or len(amounts) != nights:
            raise WireShape("prices")
        row: list[Cents | None] = []
        for amount in amounts:
            try:
                row.append(night_cents(amount))
            except ValueError:
                raise WireShape("prices") from None
        parsed[room_id] = tuple(row)
    return parsed


def parse_code(payload: object) -> ReservationCode:
    try:
        return ReservationCode(payload)
    except ValueError:
        raise WireShape("code") from None


def _party(payload: dict[str, object]) -> Party | None:
    men = _plain_int(payload["men"])
    if men == -1:
        # Wired uses -1 when the booking has no party.
        return None
    children_raw = payload.get("children", 0)
    children = 0 if children_raw is None else _plain_int(children_raw)
    return Party(adults=men, children=children)


def _room_ids(value: object) -> tuple[RoomId, ...]:
    if not isinstance(value, str):
        raise ValueError("rooms")
    ids: list[RoomId] = []
    for part in value.split(","):
        text = part.strip()
        if text == "":
            continue
        ids.append(RoomId(text))
    return tuple(ids)


def parse_reservation(payload: object) -> Reservation:
    if not isinstance(payload, dict):
        raise WireShape("booking")
    if "reservation_code" in payload:
        raw_code = payload["reservation_code"]
    elif "rcode" in payload:
        raw_code = payload["rcode"]
    else:
        raise WireShape("code")
    try:
        code = ReservationCode(raw_code)
        status = ReservationStatus(_plain_int(payload["status"]))
        check_in = _parse_wire_date(payload["date_arrival"])
        checkout = _parse_wire_date(payload["date_departure"])
        party = _party(payload)
        room_ids = _room_ids(payload["rooms"])
        guest = guest_from_booking(payload)
        total = cents_from_amount(payload["amount"])
    except WireShape:
        raise
    except (KeyError, ValueError):
        raise WireShape("booking") from None
    currency = payload.get("currency")
    if not isinstance(currency, str) or "channel_reservation_code" not in payload:
        raise WireShape("booking")
    return Reservation(
        code=code,
        status=status,
        check_in=check_in,
        checkout=checkout,
        party=party,
        room_ids=room_ids,
        guest=guest,
        total_cents=total,
        currency=currency,
        direct=is_direct(payload["channel_reservation_code"]),
    )


def _row_code(row: dict[str, object]) -> ReservationCode:
    if "reservation_code" in row:
        raw = row["reservation_code"]
    elif "rcode" in row:
        raw = row["rcode"]
    else:
        raise WireShape("code")
    try:
        return ReservationCode(raw)
    except ValueError:
        raise WireShape("code") from None


def _dump_record(record: KeyRecord) -> dict[str, object]:
    if isinstance(record, OpenKey):
        return {"state": "open", "fingerprint": record.fingerprint}
    if isinstance(record, SentKey):
        return {"state": "sent", "fingerprint": record.fingerprint}
    if isinstance(record, BookedKey):
        return {
            "state": "booked",
            "fingerprint": record.fingerprint,
            "code": str(record.code),
        }
    return {
        "state": "rejected",
        "fingerprint": record.fingerprint,
        "message": record.message,
        "wired_code": record.wired_code,
    }


def _load_record(payload: object) -> KeyRecord:
    if not isinstance(payload, dict):
        raise ValueError("ledger")
    state = payload.get("state")
    identity = payload.get("fingerprint")
    if not isinstance(identity, str):
        raise ValueError("ledger")
    if state == "open":
        return OpenKey(identity)
    if state == "sent":
        return SentKey(identity)
    if state == "booked":
        return BookedKey(identity, ReservationCode(payload["code"]))
    if state == "rejected":
        return RejectedKey(identity, str(payload["message"]), _plain_int(payload["wired_code"]))
    raise ValueError("ledger")


class _KeyFile:
    def __init__(self, stem: Path) -> None:
        self._json = stem.with_suffix(".json")
        self._lock_path = stem.with_suffix(".lock")
        self._lock_fd: int | None = None

    def acquire(self) -> None:
        if self._lock_fd is not None:
            return
        self._json.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            os.close(fd)
            raise
        self._lock_fd = fd

    def read(self) -> KeyRecord | None:
        if not self._json.exists():
            return None
        text = self._json.read_text(encoding="utf-8")
        if text.strip() == "":
            return None
        return _load_record(json.loads(text))

    def write(self, record: KeyRecord) -> None:
        temporary = Path(str(self._json) + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(_dump_record(record), separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._json)

    def release(self) -> None:
        fd = self._lock_fd
        if fd is None:
            return
        self._lock_fd = None
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class DirectoryLedger:
    def __init__(self, path: str | Path) -> None:
        self._root = Path(path)

    def lock(self, key: IdempotencyKey) -> _KeyFile:
        digest = hashlib.sha256(str(key).encode()).hexdigest()
        held = _KeyFile(self._root / digest)
        held.acquire()
        return held


class XmlRpcWire:
    def __init__(self, token: str, lcode: int, proxy: object | None = None) -> None:
        self._token = token
        self._lcode = lcode
        self._proxy = proxy if proxy is not None else xmlrpc.client.ServerProxy(_WIRED_URL, allow_none=True)

    def _invoke(self, method: str, args: tuple[object, ...]) -> object:
        return _call(self._proxy, self._token, self._lcode, method, args)

    def rooms(self) -> dict[RoomId, Room]:
        return parse_catalog(self._invoke("fetch_rooms", ()))

    def room_stay(self, stay: Stay, room: Room) -> RoomStay:
        values = self._invoke("fetch_rooms_values", _values_args(values_window(stay)))
        prices = self._invoke("fetch_plan_prices", _price_args(price_window(stay)))
        return assemble(stay, room, parse_values(values, stay), parse_prices(prices, stay))

    def book(
        self,
        window: BookingWindow,
        room: Room,
        quantity: int,
        guest: Guest,
        party: Party,
        total: Cents,
        key: IdempotencyKey,
    ) -> ReservationCode:
        arrival, departure = _booking_dates(window)
        payload = self._invoke(
            "new_reservation",
            (
                arrival,
                departure,
                {str(int(room.id)): [quantity, "nb"]},
                guest_to_customer(guest),
                wire_amount(total),
                "booking",
                0,
                {"idempotency_key": str(key)},
                {"men": party.adults, "children": party.children},
                0,
                0,
                int(ReservationStatus.CONFIRMED),
            ),
        )
        return parse_code(payload)

    def find_by_key(self, arrival: ArrivalDay, key: IdempotencyKey) -> ReservationCode | None:
        payload = self._invoke("fetch_bookings", _arrival_args(arrival))
        if not isinstance(payload, list):
            raise WireShape("bookings")
        matched: ReservationCode | None = None
        for row in payload:
            if not isinstance(row, dict):
                raise WireShape("booking")
            ancillary = row.get("ancillary")
            if not isinstance(ancillary, dict):
                continue
            marker = ancillary.get("idempotency_key")
            if marker is None or str(marker) != str(key):
                continue
            matched = _row_code(row)
            break
        if matched is None and len(payload) >= _BOOKING_PAGE:
            # A full page can hide the key past this cap, so a miss is not absence.
            raise WireShape("bookings")
        return matched

    def reservation(self, code: ReservationCode) -> Reservation:
        return parse_reservation(self._invoke("fetch_booking", (str(code), 0)))

    def cancel(self, code: ReservationCode, reason: str) -> None:
        self._invoke("cancel_reservation", (str(code), reason, 1))


class Counter:
    def __init__(self, wire: Wire, ledger: Ledger) -> None:
        self._wire = wire
        self._ledger = ledger

    @classmethod
    def open(cls) -> Counter:
        """Read WUBOOK_TOKEN and WUBOOK_LCODE. Optional BOOKING_LEDGER_DIR, else .booking-ledger."""

        token = os.environ.get("WUBOOK_TOKEN", "")
        raw_lcode = os.environ.get("WUBOOK_LCODE", "")
        if token == "" or raw_lcode == "":
            raise RuntimeError("WUBOOK_TOKEN and WUBOOK_LCODE are required")
        try:
            lcode = int(raw_lcode)
        except ValueError:
            raise RuntimeError("WUBOOK_LCODE must be an integer") from None
        directory = os.environ.get("BOOKING_LEDGER_DIR") or ".booking-ledger"
        return cls(XmlRpcWire(token, lcode), DirectoryLedger(directory))

    def rooms(self) -> tuple[ListedRoom, ...]:
        return tuple(
            ListedRoom(room.id, room.name, room.occupancy, room.availability_room_id)
            for room in self._wire.rooms().values()
        )

    def price(self, stay: Stay, room_id: RoomId, today: date) -> Offer | Refusal:
        early = past_arrival(stay, today)
        if early is not None:
            return early
        late = too_long(stay)
        if late is not None:
            return late
        room = self._wire.rooms().get(room_id)
        if room is None:
            return UnknownRoom(room_id)
        return quote(stay, room, self._wire.room_stay(stay, room))

    def reserve(
        self,
        stay: Stay,
        room_id: RoomId,
        guest: Guest,
        key: IdempotencyKey,
        today: date,
    ) -> ReserveResult:
        quoted = self.price(stay, room_id, today)
        if not isinstance(quoted, Offer):
            return quoted
        identity = fingerprint(stay, room_id, guest)
        lock = self._ledger.lock(key)
        try:
            record = lock.read()
            if record is not None and record.fingerprint != identity:
                return KeyUsed(key)
            if isinstance(record, BookedKey):
                return self._wire.reservation(record.code)
            if record is None:
                record = OpenKey(identity)
                lock.write(record)
            try:
                found = self._wire.find_by_key(arrival_day(stay), key)
            except WireShape:
                return InFlight(key)
            if found is not None:
                lock.write(BookedKey(identity, found))
                return self._wire.reservation(found)
            if isinstance(record, SentKey):
                return InFlight(key)
            room = self._wire.rooms().get(room_id)
            if room is None:
                return UnknownRoom(room_id)
            lock.write(SentKey(identity))
            try:
                code = self._wire.book(
                    booking_window(stay),
                    room,
                    stay.quantity,
                    guest,
                    stay.party,
                    quoted.total_cents,
                    key,
                )
            except WiredRejected as rejected:
                lock.write(RejectedKey(identity, rejected.message, rejected.code))
                return Rejected(rejected.message, rejected.code)
            lock.write(BookedKey(identity, code))
            return self._wire.reservation(code)
        finally:
            lock.release()

    def reservation(self, code: ReservationCode) -> Reservation:
        return self._wire.reservation(code)

    def cancel(self, code: ReservationCode, reason: str = "") -> Reservation | NotDirect:
        current = self._wire.reservation(code)
        if current.status in (ReservationStatus.CANCELLED, ReservationStatus.CANCELLED_PENALTY):
            return current
        if not current.direct:
            return NotDirect(code)
        self._wire.cancel(code, reason)
        return self._wire.reservation(code)
