from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import NewType


RoomId = NewType("RoomId", int)
IdempotencyKey = NewType("IdempotencyKey", str)
Euros = NewType("Euros", Decimal)


class Origin(Enum):
    VOIX = "voix"
    GUICHET = "guichet"


_MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def _when(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]}"


def speak_euros(amount: Decimal) -> str:
    cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    whole, rem = divmod(cents, 100)
    if rem == 0:
        return str(whole)
    return f"{whole},{rem:02d}"


def _night_word(nights: int) -> str:
    return "nuit" if nights == 1 else "nuits"


def _with_pool(text: str, names: tuple[str, ...]) -> str:
    if not names:
        return text
    return text + ", même stock que " + ", ".join(names)


@dataclass(frozen=True)
class Blocked:
    reason: str

    def line(self) -> str:
        return self.reason


def _add_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


@dataclass(frozen=True)
class Stay:
    check_in: date
    check_out: date
    adults: int
    children: int
    quantity: int = 1

    @classmethod
    def from_iso(
        cls,
        arrivee: str,
        depart: str,
        adults: int,
        children: int,
        quantity: int = 1,
    ) -> Stay | Blocked:
        try:
            check_in = date.fromisoformat(arrivee)
            check_out = date.fromisoformat(depart)
        except (TypeError, ValueError):
            return Blocked("date invalide")
        if check_in < date.today():
            return Blocked("arrivée déjà passée")
        if check_out <= check_in:
            return Blocked("départ avant l'arrivée")
        if check_out > _add_years(check_in, 2):
            return Blocked("séjour trop long")
        if (
            isinstance(adults, bool)
            or isinstance(children, bool)
            or not isinstance(adults, int)
            or not isinstance(children, int)
            or adults < 0
            or children < 0
            or adults + children == 0
        ):
            return Blocked("indiquez les personnes")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            return Blocked("quantité invalide")
        return cls(check_in, check_out, adults, children, quantity)

    def party(self) -> int:
        return self.adults + self.children

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    def slept(self) -> tuple[date, ...]:
        return tuple(self.check_in + timedelta(days=i) for i in range(self.nights))

    def price_span(self) -> InclusiveNights:
        return InclusiveNights(self.check_in, self.check_out - timedelta(days=1))

    def values_span(self) -> InclusiveNights:
        return InclusiveNights(self.check_in, self.check_out)

    def reservation_span(self) -> ReservationSpan:
        return ReservationSpan(self.check_in, self.check_out)


@dataclass(frozen=True)
class InclusiveNights:
    first: date
    last: date

    def __post_init__(self) -> None:
        if self.last < self.first:
            raise ValueError("InclusiveNights se termine avant le début")


@dataclass(frozen=True)
class ReservationSpan:
    """dto is the departure morning. new_reservation takes this, not InclusiveNights."""

    dfrom: date
    dto: date

    def __post_init__(self) -> None:
        if self.dto <= self.dfrom:
            raise ValueError("ReservationSpan.dto n'est pas après dfrom")


@dataclass(frozen=True)
class Guest:
    lname: str
    fname: str = ""
    email: str = ""
    city: str = ""
    phone: str = ""
    street: str = ""
    country: str = ""
    arrival_hour: str = ""
    notes: str = ""


@dataclass(frozen=True)
class Room:
    id: RoomId
    name: str
    occupancy: int
    parent: RoomId | None
    price_anchor: RoomId | None


@dataclass(frozen=True)
class Catalog:
    rooms: Mapping[RoomId, Room]

    def pool(self, room: Room) -> RoomId:
        if room.parent is not None:
            return room.parent
        return room.id


@dataclass(frozen=True)
class SellBook:
    choice: Mapping[RoomId, bool]

    def sells(self, room_id: RoomId) -> bool:
        return self.choice.get(room_id, False) is True


@dataclass(frozen=True)
class SleptNight:
    date: date
    avail: int
    closed: bool
    closed_arrival: bool
    min_stay: int
    max_stay: int
    min_stay_arrival: int = 0


@dataclass(frozen=True)
class StayFacts:
    nights: Mapping[RoomId, tuple[SleptNight, ...]]
    cents: Mapping[RoomId, tuple[int | None, ...]]
    departure_closed: Mapping[RoomId, bool]


@dataclass(frozen=True)
class Offer:
    room_id: RoomId
    name: str
    nights: int
    euros: Euros
    quantity: int
    same_pool: tuple[str, ...]

    def line(self) -> str:
        text = f"{self.name}, {self.nights} {_night_word(self.nights)}, {speak_euros(self.euros)} euros"
        if self.quantity > 1:
            text += f", {self.quantity} chambres"
        return _with_pool(text, self.same_pool)


@dataclass(frozen=True)
class Refusal:
    room_id: RoomId
    name: str
    reason: str
    same_pool: tuple[str, ...]

    def line(self) -> str:
        return _with_pool(f"{self.name} refusée, {self.reason}", self.same_pool)


@dataclass(frozen=True)
class Booking:
    code: str
    room_id: RoomId
    name: str
    nights: int
    euros: Euros
    replayed: bool

    def line(self) -> str:
        head = "Déjà réservé" if self.replayed else "Réservé"
        money = speak_euros(self.euros)
        return f"{head}, code {self.code}, {self.name}, {self.nights} {_night_word(self.nights)}, {money} euros"


@dataclass(frozen=True)
class ShelfRow:
    room_id: RoomId
    name: str
    parent: RoomId | None
    parent_name: str
    sell: bool


def price_ids(catalog: Catalog, selling: tuple[RoomId, ...]) -> tuple[RoomId, ...]:
    chosen: list[RoomId] = []
    seen: set[RoomId] = set()
    for room_id in selling:
        room = catalog.rooms.get(room_id)
        anchor = None if room is None else room.price_anchor
        for candidate in (room_id, anchor):
            if candidate is None or candidate in seen:
                continue
            seen.add(candidate)
            chosen.append(candidate)
    return tuple(chosen)


def _usable(series: tuple[int | None, ...] | None, nights: int) -> tuple[int, ...] | None:
    if nights <= 0 or series is None or len(series) != nights:
        return None
    if any(not isinstance(cent, int) or isinstance(cent, bool) for cent in series):
        return None
    return tuple(cent for cent in series if isinstance(cent, int))


def series_for(room: Room, facts: StayFacts) -> tuple[int, ...] | None:
    expected = len(facts.nights.get(room.id, ()))
    own = _usable(facts.cents.get(room.id), expected)
    if own is not None:
        return own
    if room.price_anchor is None:
        return None
    return _usable(facts.cents.get(room.price_anchor), expected)


def total_cents(series: tuple[int, ...], quantity: int) -> int:
    return sum(series) * quantity


def pool_names(room: Room, catalog: Catalog, sell: SellBook) -> tuple[str, ...]:
    pool = catalog.pool(room)
    names: list[str] = []
    for other in catalog.rooms.values():
        if other.id == room.id:
            continue
        if catalog.pool(other) == pool and sell.sells(other.id):
            names.append(other.name)
    return tuple(names)


@dataclass(frozen=True)
class _View:
    stay: Stay
    room: Room
    nights: tuple[SleptNight, ...]
    facts: StayFacts
    sell: SellBook
    requested: bool
    series: tuple[int, ...] | None


def _unsold(view: _View) -> str | None:
    if view.requested and not view.sell.sells(view.room.id):
        return "pas en vente"
    return None


def _too_many_people(view: _View) -> str | None:
    if view.stay.party() > view.room.occupancy:
        return f"trop de personnes pour {view.room.occupancy} places"
    return None


def _closed_nights(view: _View) -> str | None:
    closed = [_when(night.date) for night in view.nights if night.closed]
    if not closed:
        return None
    return "fermée le " + ", ".join(closed)


def _closed_arrival(view: _View) -> str | None:
    if not view.nights or not view.nights[0].closed_arrival:
        return None
    return f"arrivée fermée le {_when(view.stay.check_in)}"


def _closed_departure(view: _View) -> str | None:
    if not view.facts.departure_closed.get(view.room.id, False):
        return None
    return f"départ fermé le {_when(view.stay.check_out)}"


def _min_stay(view: _View) -> str | None:
    required = max((night.min_stay for night in view.nights), default=0)
    if required > 0 and view.stay.nights < required:
        return f"minimum {required} nuits"
    return None


def _min_stay_arrival(view: _View) -> str | None:
    if not view.nights:
        return None
    required = view.nights[0].min_stay_arrival
    if required > 0 and required > view.stay.nights:
        return f"minimum {required} nuits à l'arrivée"
    return None


def _max_stay(view: _View) -> str | None:
    positive = [night.max_stay for night in view.nights if night.max_stay > 0]
    if not positive:
        return None
    limit = min(positive)
    if view.stay.nights > limit:
        return f"maximum {limit} nuits"
    return None


def _availability(view: _View) -> str | None:
    if not view.nights:
        return None
    avail = min(night.avail for night in view.nights)
    if avail < view.stay.quantity:
        return f"il reste {avail}, il en faut {view.stay.quantity}"
    return None


def _missing_price(view: _View) -> str | None:
    if view.series is None:
        return "prix manquant"
    return None


CHECKS: tuple[Callable[[_View], str | None], ...] = (
    _unsold,
    _too_many_people,
    _closed_nights,
    _closed_arrival,
    _closed_departure,
    _min_stay,
    _min_stay_arrival,
    _max_stay,
    _availability,
    _missing_price,
)


def quote(
    stay: Stay,
    catalog: Catalog,
    facts: StayFacts,
    sell: SellBook,
    only: RoomId | None = None,
) -> tuple[Offer | Refusal, ...]:
    if only is not None:
        room = catalog.rooms.get(only)
        if room is None:
            return (Refusal(only, str(int(only)), "pas en vente", ()),)
        rooms: tuple[Room, ...] = (room,)
        requested = True
    else:
        rooms = tuple(room for room in catalog.rooms.values() if sell.sells(room.id))
        requested = False
    return tuple(_one(stay, room, catalog, facts, sell, requested) for room in rooms)


def _one(
    stay: Stay,
    room: Room,
    catalog: Catalog,
    facts: StayFacts,
    sell: SellBook,
    requested: bool,
) -> Offer | Refusal:
    series = series_for(room, facts)
    view = _View(
        stay=stay,
        room=room,
        nights=facts.nights.get(room.id, ()),
        facts=facts,
        sell=sell,
        requested=requested,
        series=series,
    )
    names = pool_names(room, catalog, sell)
    for check in CHECKS:
        reason = check(view)
        if reason is not None:
            return Refusal(room.id, room.name, reason, names)
    if series is None:
        return Refusal(room.id, room.name, "prix manquant", names)
    cents = total_cents(series, stay.quantity)
    euros = Euros(Decimal(cents) / Decimal(100))
    return Offer(room.id, room.name, stay.nights, euros, stay.quantity, names)
