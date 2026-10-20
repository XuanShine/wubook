"""Type sketch for comptoir. Bodies are unimplemented on purpose.

Module map. Production callers use Desk. app.py does not import quote.

comptoir/quote.py
    Stay, room, sell book, slept nights, plan-0 cents, quote, series_for.
    This file answers "why was this room refused?" and "which cents are summed?"
comptoir/desk.py
    Desk, the token, the flood memory, sell.yml, receipts, XML-RPC.
    This file answers "which call filled those cents?" and "who copied parent avail?"
comptoir/sell.yml
    The only room list. A missing id is not sold. Legal words are yes and no.
comptoir/app.py
    POST /outils/chercher, POST /outils/reserver, the NiceGUI page, guest_from_form.
comptoir/__main__.py
    One process. python -m comptoir.
comptoir/__init__.py
    Exports Desk, Stay, Guest, Blocked, Offer, Refusal, Booking, ShelfRow,
    Origin, RoomId, IdempotencyKey. Nothing from the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Mapping, NewType


RoomId = NewType("RoomId", int)
IdempotencyKey = NewType("IdempotencyKey", str)
Euros = NewType("Euros", Decimal)


class Origin(Enum):
    """Which desk sent the booking. Wired stores the value. It does not change the rules."""

    VOIX = "voix"
    GUICHET = "guichet"


# --- quote.py ---------------------------------------------------------------


@dataclass(frozen=True)
class Blocked:
    """The stay never reached a room rule. reason is already a short French sentence."""

    reason: str

    def line(self) -> str:
        """Return reason unchanged so the route can speak every result the same way."""
        raise NotImplementedError


@dataclass(frozen=True)
class Stay:
    """check_out is the departure morning and is not a night the guest sleeps.

    from_iso is the only constructor callers use. ISO dates, never DD/MM/YYYY.
    """

    check_in: date
    check_out: date
    adults: int
    children: int
    quantity: int

    @classmethod
    def from_iso(
        cls,
        arrivee: str,
        depart: str,
        adults: int,
        children: int,
        quantity: int = 1,
    ) -> Stay | Blocked:
        """Reject a past arrival, a departure that is not after arrival, a span
        over two years, a party of zero, and a quantity below 1.
        Speech: "arrivée déjà passée", "départ avant l'arrivée", "séjour trop long",
        "indiquez les personnes", "quantité invalide", "nombre invalide".
        """
        raise NotImplementedError

    def party(self) -> int:
        """adults + children. quote compares this with Room.occupancy and nothing else."""
        raise NotImplementedError

    def slept(self) -> tuple[date, ...]:
        """check_in inclusive, check_out exclusive."""
        raise NotImplementedError

    def values_span(self) -> tuple[date, date]:
        """Inclusive window for fetch_rooms_values. Ends on check_out.

        The last date exists so closed_departure can be read. It is not a slept night
        and it is not priced.
        """
        raise NotImplementedError

    def price_span(self) -> tuple[date, date]:
        """Inclusive window for fetch_plan_prices. Ends on the last slept night."""
        raise NotImplementedError

    def reservation_span(self) -> tuple[date, date]:
        """dfrom is check_in and dto is check_out.

        Matches the Wired example. One night uses dfrom 21/02/2025 and dto 22/02/2025.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class Guest:
    """Wired customer keys. Empty strings are omitted on the wire, not sent as blanks.

    Callers build a Guest through guest_from_form in app.py.
    """

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
    """Catalog fields the quote needs.

    The parse drops wire price, min_price, max_price, board, shortname, men,
    children, and woodoo. A catalog price on this type would get spoken by accident.
    parent is None when subroom is 0. price_anchor is None when anchorate is 0.
    """

    id: RoomId
    name: str
    occupancy: int
    parent: RoomId | None
    price_anchor: RoomId | None


@dataclass(frozen=True)
class Catalog:
    rooms: Mapping[RoomId, Room]

    def pool(self, room: Room) -> RoomId:
        """Parent id for a virtual room. The room id when the room is physical."""
        raise NotImplementedError


@dataclass(frozen=True)
class SellBook:
    """Parsed sell.yml. Absence is already False by the time quote sees the mapping."""

    choice: Mapping[RoomId, bool]

    def sells(self, room_id: RoomId) -> bool:
        """True only for an explicit yes. A missing id is False."""
        raise NotImplementedError


@dataclass(frozen=True)
class SleptNight:
    """One night inside check_in inclusive, check_out exclusive.

    avail is already the parent room's avail when Room.parent is set.
    closed_departure is not a field here. It lives on StayFacts.departure_closed,
    taken from the checkout date. min_stay_arrival, booked, and no_ota are not fields.
    """

    date: date
    avail: int
    closed: bool
    closed_arrival: bool
    min_stay: int
    max_stay: int


@dataclass(frozen=True)
class StayFacts:
    """One search window. nights, cents, and departure_closed share room ids and night order.

    cents is plan 0 in euro cents. None means the night was missing or below 0.01.
    Read cents through series_for. A direct sum skips the anchor and ignores None.
    """

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
        """"{name}, {n} nuit" or "nuits", "{euros} euros".

        Comma decimals, no euro sign. Append ", {quantity} chambres" when quantity is above 1.
        Append ", même stock que {names}" when same_pool is not empty.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class Refusal:
    room_id: RoomId
    name: str
    reason: str
    same_pool: tuple[str, ...]

    def line(self) -> str:
        """"{name} refusée, {reason}" plus the same pool clause as Offer.line."""
        raise NotImplementedError


@dataclass(frozen=True)
class Booking:
    code: str
    room_id: RoomId
    name: str
    nights: int
    euros: Euros
    replayed: bool

    def line(self) -> str:
        """"Réservé, code {code}, {name}, {n} nuits, {euros} euros".

        When replayed, the first word is "Déjà réservé".
        """
        raise NotImplementedError


@dataclass(frozen=True)
class ShelfRow:
    room_id: RoomId
    name: str
    parent: RoomId | None
    parent_name: str
    sell: bool


@dataclass(frozen=True)
class ReservationOrder:
    """What _place sends, minus the constants that must not be parameters.

    No ccard, no ignore_restrs, no ignore_avail, no status, no board.
    Those stay on _Wired. The route never sees this type.
    """

    stay: Stay
    room_id: RoomId
    quantity: int
    euros: Euros
    guest: Guest
    origin: Origin


def price_ids(catalog: Catalog, selling: tuple[RoomId, ...]) -> tuple[RoomId, ...]:
    """Room ids to ask of plan 0. Includes price_anchor when it is set."""
    raise NotImplementedError


def series_for(room: Room, facts: StayFacts) -> tuple[int, ...] | None:
    """Own plan-0 series when every night is an int. Otherwise the anchor series.

    None when both are unusable. This is the only place that chooses the anchor.
    """
    raise NotImplementedError


def total_cents(series: tuple[int, ...], quantity: int) -> int:
    """Sum of slept nights, times quantity."""
    raise NotImplementedError


def pool_names(room: Room, catalog: Catalog, sell: SellBook) -> tuple[str, ...]:
    """Other rooms the file sells in this room's pool. Empty when the room is alone."""
    raise NotImplementedError


def quote(
    stay: Stay,
    catalog: Catalog,
    facts: StayFacts,
    sell: SellBook,
    only: RoomId | None = None,
) -> tuple[Offer | Refusal, ...]:
    """One verdict per sold room, or one verdict for `only`.

    Search passes only=None and unsold rooms are omitted.
    Book passes only=that room. A no or a missing id yields "pas en vente".
    First match wins, in this order. Dates use the day and the French month name.
    "pas en vente" when only is set.
    "trop de personnes pour {occupancy} places". Party size is Stay.party() against occupancy.
    "fermée le {dates}" when any slept night is closed.
    "arrivée fermée le {check_in}" from closed_arrival on the check-in night.
    "départ fermé le {check_out}" from StayFacts.departure_closed.
    "minimum {n} nuits" from the max min_stay across slept nights. 0 adds no limit.
    "maximum {n} nuits" from the smallest positive max_stay. All zeros add no limit.
    "il reste {avail}, il en faut {quantity}" from the minimum avail across slept nights.
    "prix manquant" when series_for returns None.
    same_pool is pool_names, on an offer and on a refusal.
    euros comes from total_cents(series_for(...)).
    """
    raise NotImplementedError


# --- desk.py ----------------------------------------------------------------


class _Wired:
    """The only XML-RPC speaker. https://wired.wubook.net/xrws/ with the slash.

    Public methods of Desk have no way to pass these constants.
    """

    url = "https://wired.wubook.net/xrws/"
    parity_pid = 0
    board = "nb"
    status_confirmed = 1
    ccard = 0
    ignore_restrs = 0
    ignore_avail = 0


class ReceiptState(Enum):
    OPEN = "open"  # intent is on disk, new_reservation has not been called
    SENT = "sent"  # the call started, the code is unknown
    BOOKED = "booked"
    REJECTED = "rejected"  # Wired returned a negative code, so no booking exists


@dataclass(frozen=True)
class Receipt:
    """key -> one booking. fingerprint is room, dates, quantity, lname, email, cents."""

    key: IdempotencyKey
    fingerprint: str
    state: ReceiptState
    code: str | None


@dataclass
class Memory:
    """Exact stays, not an interval index. A miss is a Wired call. A hit inside max_age is not.

    Catalog refresh clears facts. Book does not read facts. Book loads a fresh window.
    """

    catalog: Catalog | None
    catalog_at: datetime | None
    facts: dict[tuple[date, date], tuple[datetime, StayFacts]]
    rpc_at: list[datetime]  # 600 calls / 3600 seconds
    values_at: list[datetime]  # fetch_rooms_values, 240 / 43200 seconds
    rooms_at: list[datetime]  # fetch_rooms, 240 / 86400 seconds
    facts_max_age_s: int = 15 * 60
    catalog_max_age_s: int = 6 * 60 * 60


class WiredFault(Exception):
    """Negative Wired code. Raise as WiredFault(code, message). message is the payload string."""


class Desk:
    """search, book, rooms, set_sell. One instance per process. One lock for the token,
    sell.yml, and the receipt file, because those three are shared by the page and the routes.
    """

    def __init__(
        self,
        sell_path: Path | None = None,
        receipt_path: Path | None = None,
    ) -> None:
        """Open the token once.

        Use WUBOOK_TOKEN when it is set, and do not release that token.
        Otherwise acquire_token(WUBOOK_USER, WUBOOK_PASSWORD, WUBOOK_PKEY) and release
        it from close(). lcode is WUBOOK_LCODE.
        Default paths are comptoir/sell.yml and comptoir/receipts.json.
        A sell.yml value other than yes or no aborts startup.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release the token only when this process acquired it."""
        raise NotImplementedError

    def rooms(self) -> tuple[ShelfRow, ...]:
        """Every catalog room, sold or not, so the switch can turn a missing id on."""
        raise NotImplementedError

    def set_sell(self, room_id: RoomId, sell: bool) -> None:
        """Rewrite sell.yml. True writes yes, False writes no. Atomic rename.

        Does not delete the key. Absence remains the state of a room never toggled.
        """
        raise NotImplementedError

    def search(self, stay: Stay) -> Blocked | tuple[Offer | Refusal, ...]:
        """Reuse StayFacts cached under (check_in, check_out) for 15 minutes, else _load.

        Then quote(..., only=None).
        Flood becomes Blocked("WuBook limité, réessayez plus tard.").
        This is the only search the page and the route call.
        """
        raise NotImplementedError

    def book(
        self,
        stay: Stay,
        room_id: RoomId,
        guest: Guest,
        key: IdempotencyKey,
        origin: Origin,
    ) -> Booking | Refusal | Blocked:
        """Re-quote from a fresh StayFacts. Never from the search cache.

        TODO receipt, under the lock, fingerprint checked first:
        empty key -> Blocked "clé manquante"
        unknown room -> Blocked "chambre inconnue"
        same key, other fingerprint -> Blocked "clé déjà utilisée pour une autre demande."
        BOOKED -> Booking(replayed=True) and do not call Wired
        SENT -> _find_existing. A match becomes BOOKED. No match stays SENT and
                returns Blocked "envoi incertain, vérifiez WuBook." Do not call new_reservation.
        REJECTED or OPEN -> quote(..., only=room_id). A Refusal is returned and Wired is not called.
        An Offer writes SENT, calls _place, then BOOKED with the code, or REJECTED on WiredFault.
        Crash after OPEN and before SENT is safe to repeat. Crash after SENT is not.
        """
        raise NotImplementedError

    def _catalog(self) -> Catalog:
        """fetch_rooms, cached until catalog_max_age_s. Build Room and drop the price fields."""
        raise NotImplementedError

    def _sell(self) -> SellBook:
        """Read sell.yml. The word yes is True. The word no is False."""
        raise NotImplementedError

    def _load(self, stay: Stay, catalog: Catalog, selling: tuple[RoomId, ...]) -> StayFacts:
        """Join _grid and _prices. This is the write of cents, and the only write."""
        raise NotImplementedError

    def _grid(self, stay: Stay, catalog: Catalog) -> tuple[Mapping[RoomId, tuple[SleptNight, ...]], Mapping[RoomId, bool]]:
        """fetch_rooms_values on stay.values_span. Ignore each day's price.

        Copy parent avail onto a virtual room for slept nights only.
        departure_closed comes from the checkout date's closed_departure.
        Discard avail, closed, and price on that extra day.
        """
        raise NotImplementedError

    def _prices(self, stay: Stay, catalog: Catalog, selling: tuple[RoomId, ...]) -> Mapping[RoomId, tuple[int | None, ...]]:
        """fetch_plan_prices(token, lcode, parity_pid, dfrom, dto, price_ids(...)).

        dfrom and dto are stay.price_span in DD/MM/YYYY. Store cents under the id Wired returned.
        A float below 0.01 becomes None. Do not round it up to one cent.
        """
        raise NotImplementedError

    def _place(self, order: ReservationOrder) -> str:
        """new_reservation. Return the reservation code.

        rooms is {room_id: [quantity, board]}. amount is float(euros).
        customer omits empty guest fields. guests is men=adults, children=children.
        origin is order.origin.value. status, ccard, ignore_restrs, ignore_avail
        are the _Wired constants.
        dfrom and dto are reservation_span in DD/MM/YYYY.
        """
        raise NotImplementedError

    def _find_existing(self, order: ReservationOrder) -> str | None:
        """fetch_bookings for the reservation span, oncreated 0.

        Match room id, lname, email, and cents. Return the reservation code, or None.
        """
        raise NotImplementedError

    def _call(self, method: str, *args: object) -> object:
        """Append the flood mark, then call. Code 0 returns the payload.

        A negative code raises WiredFault. When the budget is spent, raise Flood
        and skip the socket. search and book catch Flood and return Blocked.
        """
        raise NotImplementedError


class Flood(Exception):
    """Local stop before Wired's ceiling. Not a Wired error."""


# --- app.py -----------------------------------------------------------------

# The only French-to-Wired name map. guest_from_form is its only reader.
CLIENT_FIELDS = (
    ("nom", "lname"),
    ("prenom", "fname"),
    ("email", "email"),
    ("ville", "city"),
    ("telephone", "phone"),
    ("rue", "street"),
    ("pays", "country"),
    ("heure", "arrival_hour"),
    ("notes", "notes"),
)


def guest_from_form(fields: Mapping[str, str]) -> Guest | Blocked:
    """Build a Guest from CLIENT_FIELDS.

    nom is required ("nom manquant"). pays, when present, is two letters ("pays invalide").
    The page and POST /outils/reserver both call this. Desk.book does not remap names.
    """
    raise NotImplementedError


def chercher(desk: Desk, body: Mapping[str, object]) -> dict[str, list[str]]:
    """POST /outils/chercher.

    Read arrivee, depart, adultes, enfants, and optional quantite.
    Stay.from_iso, then desk.search.
    A Blocked from either step returns {"lignes": [blocked.line()]}.
    A tuple returns {"lignes": [item.line() for item in found]}.
    The body is the stay. The response is lignes.
    """
    raise NotImplementedError


def reserver(desk: Desk, body: Mapping[str, object]) -> dict[str, list[str]]:
    """POST /outils/reserver.

    Read cle, chambre, arrivee, depart, adultes, enfants, optional quantite, client.
    Brand chambre as RoomId and cle as IdempotencyKey.
    {"lignes": [desk.book(...).line()]}.
    The JSON has no montant, no carte, and no ignore flags.
    """
    raise NotImplementedError


def page(desk: Desk) -> None:
    """NiceGUI `/` in French.

    Shelf: desk.rooms(), and the switch calls desk.set_sell once.
    Search runs from the submit control, never from a keystroke.
    A row shows verdict.line(). The reserve control is drawn only for an Offer.
    The control keeps one IdempotencyKey for its lifetime so a second click repeats it.
    Guest goes through guest_from_form. Origin is GUICHET. The voice route uses VOIX.
    """
    raise NotImplementedError


# --- __main__.py ------------------------------------------------------------


def main() -> None:
    """Build one Desk, mount the page and the two routes on 127.0.0.1:8071, close the token on exit."""
    raise NotImplementedError
