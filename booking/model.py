from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import IntEnum
from typing import ClassVar, Mapping


class WireShape(Exception):
    """Code was 0 and the payload was not the shape the parsers require."""


class RoomId(int):
    """Catalog ids are ints. Values keys are decimal strings of that int."""

    def __new__(cls, value: object) -> RoomId:
        if isinstance(value, bool):
            raise ValueError("room id")
        if isinstance(value, int):
            number = value
        elif isinstance(value, str) and value.isdecimal():
            number = int(value)
        else:
            raise ValueError("room id")
        if number <= 0:
            raise ValueError("room id")
        return super().__new__(cls, number)


class Cents(int):
    def __new__(cls, value: object) -> Cents:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("cents")
        return super().__new__(cls, value)


class ReservationCode(str):
    """Wired reservation_code, stored as text. A numeric payload is accepted once."""

    def __new__(cls, value: object) -> ReservationCode:
        if isinstance(value, bool):
            raise ValueError("reservation code")
        if isinstance(value, int):
            text = str(value)
        elif isinstance(value, str) and value != "":
            text = value
        else:
            raise ValueError("reservation code")
        return super().__new__(cls, text)


class IdempotencyKey(str):
    """Non-empty caller key. The same key names one reserve request."""

    def __new__(cls, value: object) -> IdempotencyKey:
        if not isinstance(value, str) or value == "":
            raise ValueError("idempotency key")
        return super().__new__(cls, value)


class ReservationStatus(IntEnum):
    CONFIRMED = 1
    WAITING = 2
    REFUSED = 3
    ACCEPTED = 4
    CANCELLED = 5
    CANCELLED_PENALTY = 6


def _require_date(value: object) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError("date")


@dataclass(frozen=True)
class Party:
    adults: int
    children: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.adults, bool) or isinstance(self.children, bool):
            raise ValueError("party")
        if not isinstance(self.adults, int) or not isinstance(self.children, int):
            raise ValueError("party")
        if self.adults < 0 or self.children < 0 or self.adults + self.children < 1:
            raise ValueError("party")

    def size(self) -> int:
        return self.adults + self.children


@dataclass(frozen=True)
class Stay:
    """check_in is the first slept night. checkout is the morning the guest leaves."""

    check_in: date
    checkout: date
    party: Party
    quantity: int = 1

    def __post_init__(self) -> None:
        _require_date(self.check_in)
        _require_date(self.checkout)
        if not isinstance(self.party, Party):
            raise ValueError("party")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity < 1:
            raise ValueError("quantity")
        if self.checkout <= self.check_in:
            raise ValueError("checkout is after check-in")

    def night_count(self) -> int:
        return (self.checkout - self.check_in).days

    def slept(self) -> tuple[date, ...]:
        return tuple(self.check_in + timedelta(days=offset) for offset in range(self.night_count()))


@dataclass(frozen=True)
class ValuesWindow:
    """fetch_rooms_values only. checkout_morning is not a priced night."""

    check_in: date
    checkout_morning: date

    def __post_init__(self) -> None:
        _require_date(self.check_in)
        _require_date(self.checkout_morning)
        if self.checkout_morning <= self.check_in:
            raise ValueError("values window")


@dataclass(frozen=True)
class PriceWindow:
    """fetch_plan_prices only. last_night is the last slept night."""

    check_in: date
    last_night: date

    def __post_init__(self) -> None:
        _require_date(self.check_in)
        _require_date(self.last_night)
        if self.last_night < self.check_in:
            raise ValueError("price window")


@dataclass(frozen=True)
class BookingWindow:
    """new_reservation only. arrival is dfrom. departure is dto, the checkout morning."""

    arrival: date
    departure: date

    def __post_init__(self) -> None:
        _require_date(self.arrival)
        _require_date(self.departure)
        if self.departure <= self.arrival:
            raise ValueError("booking window")


@dataclass(frozen=True)
class ArrivalDay:
    """fetch_bookings arrival filter only. oncreated is 0, so both wire ends are this day."""

    day: date


@dataclass(frozen=True)
class Guest:
    last_name: str
    first_name: str
    email: str
    city: str = ""
    phone: str = ""
    street: str = ""
    country: str = ""
    arrival_hour: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        for name in (
            "last_name",
            "first_name",
            "email",
            "city",
            "phone",
            "street",
            "country",
            "arrival_hour",
            "notes",
        ):
            if not isinstance(getattr(self, name), str):
                raise ValueError(name)
        if self.last_name == "" or self.first_name == "" or self.email == "":
            raise ValueError("guest")


@dataclass(frozen=True)
class Room:
    """availability_room_id is the stock parent. price_room_id is the price parent.

    Either one may be this room's own id. The two ids may differ.
    """

    id: RoomId
    name: str
    occupancy: int
    availability_room_id: RoomId
    price_room_id: RoomId


@dataclass(frozen=True)
class ListedRoom:
    id: RoomId
    name: str
    occupancy: int
    availability_room_id: RoomId


@dataclass(frozen=True)
class Night:
    day: date
    available: int
    closed: bool
    closed_arrival: bool
    min_stay: int
    max_stay: int
    min_stay_arrival: int


@dataclass(frozen=True)
class RoomStay:
    """cents is None when the series is incomplete.

    departure_closed comes only from the checkout-morning values cell.
    """

    nights: tuple[Night, ...]
    departure_closed: bool
    cents: tuple[Cents, ...] | None


@dataclass(frozen=True)
class NightCell:
    """Includes the extra morning."""

    available: int
    closed: bool
    closed_arrival: bool
    closed_departure: bool
    min_stay: int
    max_stay: int
    min_stay_arrival: int


def closed_flag(value: object) -> bool:
    """'0' and 0 are false. '1' and 1 are true.

    bool("0") is true, so this comparison is the only conversion.
    """

    if isinstance(value, bool):
        raise ValueError("closed flag")
    if value in (0, "0"):
        return False
    if value in (1, "1"):
        return True
    raise ValueError("closed flag")


def parent_id(room_id: RoomId, raw: object) -> RoomId:
    """subroom or anchorate. 0 becomes room_id. Any other int is that parent."""

    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("parent")
    if raw == 0:
        return room_id
    return RoomId(raw)


def _euros_to_cents(amount: object) -> int:
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError("amount")
    euros = Decimal(str(amount))
    return int((euros * 100).to_integral_value(rounding=ROUND_HALF_UP))


def night_cents(amount: object) -> Cents | None:
    if amount is None:
        return None
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError("amount")
    if Decimal(str(amount)) < Decimal("0.01"):
        return None
    cents = _euros_to_cents(amount)
    if cents < 1:
        return None
    return Cents(cents)


def cents_from_amount(amount: object) -> Cents:
    return Cents(_euros_to_cents(amount))


def wire_amount(total: Cents) -> float:
    return float(Decimal(int(total)) / Decimal(100))


def _complete(series: tuple[Cents | None, ...] | None, nights: int) -> tuple[Cents, ...] | None:
    if series is None or len(series) != nights or any(item is None for item in series):
        return None
    return tuple(item for item in series if item is not None)


def series_for(
    room: Room,
    own: tuple[Cents | None, ...] | None,
    anchor: tuple[Cents | None, ...] | None,
    nights: int,
) -> tuple[Cents, ...] | None:
    return _complete(own, nights) or _complete(anchor, nights)


def sum_cents(series: tuple[Cents, ...], quantity: int) -> Cents:
    return Cents(sum(int(item) for item in series) * quantity)


def fingerprint(stay: Stay, room_id: RoomId, guest: Guest) -> str:
    """The cents are not part of it. A price change is the same request."""

    payload = {
        "check_in": stay.check_in.isoformat(),
        "checkout": stay.checkout.isoformat(),
        "adults": stay.party.adults,
        "children": stay.party.children,
        "quantity": stay.quantity,
        "room_id": int(room_id),
        "last_name": guest.last_name,
        "first_name": guest.first_name,
        "email": guest.email,
        "city": guest.city,
        "phone": guest.phone,
        "street": guest.street,
        "country": guest.country,
        "arrival_hour": guest.arrival_hour,
        "notes": guest.notes,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _cells(
    cells_by_room: Mapping[RoomId, tuple[NightCell, ...]],
    room_id: RoomId,
    count: int,
) -> tuple[NightCell, ...]:
    row = cells_by_room.get(room_id)
    if row is None or len(row) != count:
        raise WireShape("values")
    return row


def _cents_row(
    cents_by_room: Mapping[RoomId, tuple[Cents | None, ...]],
    room_id: RoomId,
    count: int,
) -> tuple[Cents | None, ...] | None:
    row = cents_by_room.get(room_id)
    if row is None:
        return None
    if len(row) != count:
        raise WireShape("prices")
    return row


def assemble(
    stay: Stay,
    room: Room,
    cells_by_room: Mapping[RoomId, tuple[NightCell, ...]],
    cents_by_room: Mapping[RoomId, tuple[Cents | None, ...]],
) -> RoomStay:
    count = stay.night_count()
    rules = _cells(cells_by_room, room.id, count + 1)
    stock = _cells(cells_by_room, room.availability_room_id, count + 1)
    nights = tuple(
        Night(
            day=day,
            available=stock[index].available,
            closed=rules[index].closed,
            closed_arrival=rules[index].closed_arrival,
            min_stay=rules[index].min_stay,
            max_stay=rules[index].max_stay,
            min_stay_arrival=rules[index].min_stay_arrival,
        )
        for index, day in enumerate(stay.slept())
    )
    return RoomStay(
        nights=nights,
        departure_closed=rules[-1].closed_departure,
        cents=series_for(
            room,
            _cents_row(cents_by_room, room.id, count),
            _cents_row(cents_by_room, room.price_room_id, count),
            count,
        ),
    )


@dataclass(frozen=True)
class Offer:
    room_id: RoomId
    nights: int
    total_cents: Cents
    quantity: int


@dataclass(frozen=True)
class UnknownRoom:
    reason: ClassVar[str] = "unknown_room"
    room_id: RoomId


@dataclass(frozen=True)
class PastArrival:
    reason: ClassVar[str] = "past_arrival"
    check_in: date


@dataclass(frozen=True)
class TooLong:
    reason: ClassVar[str] = "too_long"
    check_in: date
    checkout: date


@dataclass(frozen=True)
class TooManyPeople:
    reason: ClassVar[str] = "too_many_people"
    occupancy: int
    party: int
    quantity: int


@dataclass(frozen=True)
class ClosedNight:
    reason: ClassVar[str] = "closed_night"
    days: tuple[date, ...]


@dataclass(frozen=True)
class ClosedArrival:
    reason: ClassVar[str] = "closed_arrival"
    day: date


@dataclass(frozen=True)
class ClosedDeparture:
    reason: ClassVar[str] = "closed_departure"
    day: date


@dataclass(frozen=True)
class MinStay:
    reason: ClassVar[str] = "min_stay"
    nights: int
    required: int


@dataclass(frozen=True)
class MinStayArrival:
    reason: ClassVar[str] = "min_stay_arrival"
    nights: int
    required: int


@dataclass(frozen=True)
class MaxStay:
    reason: ClassVar[str] = "max_stay"
    nights: int
    limit: int


@dataclass(frozen=True)
class ShortAvailability:
    reason: ClassVar[str] = "short_availability"
    available: int
    quantity: int


@dataclass(frozen=True)
class MissingPrice:
    reason: ClassVar[str] = "missing_price"
    room_id: RoomId


Refusal = (
    UnknownRoom
    | PastArrival
    | TooLong
    | TooManyPeople
    | ClosedNight
    | ClosedArrival
    | ClosedDeparture
    | MinStay
    | MinStayArrival
    | MaxStay
    | ShortAvailability
    | MissingPrice
)


def past_arrival(stay: Stay, today: date) -> PastArrival | None:
    if stay.check_in < today:
        return PastArrival(stay.check_in)
    return None


def _two_years_after(day: date) -> date:
    try:
        return day.replace(year=day.year + 2)
    except ValueError:
        return day.replace(year=day.year + 2, day=28)


def too_long(stay: Stay) -> TooLong | None:
    """Checkout more than two years after check-in. Wired will not price that range."""

    if stay.checkout > _two_years_after(stay.check_in):
        return TooLong(stay.check_in, stay.checkout)
    return None


def too_many_people(stay: Stay, room: Room) -> TooManyPeople | None:
    party = stay.party.size()
    if party > room.occupancy * stay.quantity:
        return TooManyPeople(room.occupancy, party, stay.quantity)
    return None


def closed_nights(facts: RoomStay) -> ClosedNight | None:
    days = tuple(night.day for night in facts.nights if night.closed)
    if days:
        return ClosedNight(days)
    return None


def closed_arrival(stay: Stay, facts: RoomStay) -> ClosedArrival | None:
    if facts.nights[0].closed_arrival:
        return ClosedArrival(stay.check_in)
    return None


def closed_departure(stay: Stay, facts: RoomStay) -> ClosedDeparture | None:
    if facts.departure_closed:
        return ClosedDeparture(stay.checkout)
    return None


def min_stay(stay: Stay, facts: RoomStay) -> MinStay | None:
    required = max(night.min_stay for night in facts.nights)
    if stay.night_count() < required:
        return MinStay(stay.night_count(), required)
    return None


def min_stay_arrival(stay: Stay, facts: RoomStay) -> MinStayArrival | None:
    required = facts.nights[0].min_stay_arrival
    if stay.night_count() < required:
        return MinStayArrival(stay.night_count(), required)
    return None


def max_stay(stay: Stay, facts: RoomStay) -> MaxStay | None:
    positive = [night.max_stay for night in facts.nights if night.max_stay > 0]
    if not positive:
        return None
    limit = min(positive)
    if stay.night_count() > limit:
        return MaxStay(stay.night_count(), limit)
    return None


def short_availability(stay: Stay, facts: RoomStay) -> ShortAvailability | None:
    available = min(night.available for night in facts.nights)
    if available < stay.quantity:
        return ShortAvailability(available, stay.quantity)
    return None


def missing_price(room: Room, facts: RoomStay) -> MissingPrice | None:
    if facts.cents is None:
        return MissingPrice(room.id)
    return None


def quote(stay: Stay, room: Room, facts: RoomStay) -> Offer | Refusal:
    checks: tuple[Refusal | None, ...] = (
        too_many_people(stay, room),
        closed_nights(facts),
        closed_arrival(stay, facts),
        closed_departure(stay, facts),
        min_stay(stay, facts),
        min_stay_arrival(stay, facts),
        max_stay(stay, facts),
        short_availability(stay, facts),
        missing_price(room, facts),
    )
    for refusal in checks:
        if refusal is not None:
            return refusal
    series = facts.cents
    if series is None:
        return MissingPrice(room.id)
    return Offer(
        room_id=room.id,
        nights=stay.night_count(),
        total_cents=sum_cents(series, stay.quantity),
        quantity=stay.quantity,
    )


@dataclass(frozen=True)
class Reservation:
    """room_ids may contain more than one id on a fetch."""

    code: ReservationCode
    status: ReservationStatus
    check_in: date
    checkout: date
    party: Party | None
    room_ids: tuple[RoomId, ...]
    guest: Guest
    total_cents: Cents
    currency: str
    direct: bool


def guest_to_customer(guest: Guest) -> dict[str, str]:
    return {
        "lname": guest.last_name,
        "fname": guest.first_name,
        "email": guest.email,
        "city": guest.city,
        "phone": guest.phone,
        "street": guest.street,
        "country": guest.country,
        "arrival_hour": guest.arrival_hour,
        "notes": guest.notes,
    }


def guest_from_booking(payload: Mapping[str, object]) -> Guest:
    def text(key: str) -> str:
        value = payload.get(key, "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(key)
        return value

    return Guest(
        last_name=text("customer_surname"),
        first_name=text("customer_name"),
        email=text("customer_mail"),
        city=text("customer_city"),
        phone=text("customer_phone"),
        street=text("customer_address"),
        country=text("customer_country"),
        notes=text("customer_notes"),
    )


def is_direct(channel_reservation_code: object) -> bool:
    """True when the channel code is 0 or '0'. That is the captured meaning of a non-OTA booking."""

    if isinstance(channel_reservation_code, bool):
        return False
    return channel_reservation_code in (0, "0")


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    return value


def dump_refusal(result: Refusal) -> dict[str, object]:
    body: dict[str, object] = {"reason": type(result).reason}
    for field in fields(result):
        body[field.name] = _jsonable(getattr(result, field.name))
    return body
