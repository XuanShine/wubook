"""desk. Local reception for one WuBook property. Sketch only. No Wired calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, FrozenSet, Mapping, NewType, Sequence, Union

# ---------------------------------------------------------------------------
# Module map (package desk/)
#
#   __init__.py     re-exports Desk, Stay, Party, Guest, Quote, Offer, Refusal
#   __main__.py     Desk.open(), mount voice + NiceGUI, close on exit
#   _stay.py        Stay, InclusiveNights, ReservationSpan
#   _money.py       Euro cents
#   _catalog.py     Room, Pool, occupancy, parent rewrite of avail
#   _sell.py        sell.yml allow list (the only sell list)
#   _quote.py       CHECKS table, plan0_total, speak. Price lives here.
#   _wired.py       one token, four RPCs, caches. DD/MM/YYYY stays here.
#   _ledger.py      idempotency pending -> done
#   _http.py        /voix/chercher, /voix/reserver
#   _ui.py          French NiceGUI, Chercher button, sell switches
#
# Callers import desk.Desk. Underscore modules fail the public test by name.
# Price: open _quote.py. Unsellable: open _sell.py then CHECKS in _quote.py.
# Pool/occupancy: _catalog.py. That is the three-file cap.
# ---------------------------------------------------------------------------

RoomId = NewType("RoomId", int)
IdempotencyKey = NewType("IdempotencyKey", str)
ReservationCode = NewType("ReservationCode", str)
PROPERTY_BOARD = "nb"  # this property. Not a YAML field.
PLAN0 = 0  # WuBook Parity. The only plan we read.
QUANTITY = 1  # voice and UI row book one unit


# ===== desk/_money.py =====


@dataclass(frozen=True)
class Euro:
    """Integer cents. Wired converts to float at the XML-RPC call only."""

    cents: int

    def __post_init__(self) -> None:
        if self.cents < 0:
            raise ValueError("Euro.cents must be >= 0")

    def is_sellable_rate(self) -> bool:
        return self.cents >= 1  # Wired write floor 0.01. We only read.

    def dit(self) -> str:
        """'180 euros'. Spoken French. No cents unless needed."""
        raise NotImplementedError

    @staticmethod
    def sum(parts: Sequence["Euro"]) -> "Euro":
        raise NotImplementedError


# ===== desk/_stay.py =====


@dataclass(frozen=True)
class Stay:
    """Product stay. check_out is the morning of departure, not a night."""

    check_in: date
    check_out: date

    def __post_init__(self) -> None:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")

    @staticmethod
    def parse(check_in: date, check_out: date) -> "Stay":
        """Boundary. Reject nights=0 and ranges Wired will refuse (2 years)."""
        raise NotImplementedError

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    def inclusive(self) -> "InclusiveNights":
        return InclusiveNights(first=self.check_in, last=self.check_out - timedelta(days=1))

    def reservation(self) -> "ReservationSpan":
        return ReservationSpan(dfrom=self.check_in, dto=self.check_out)


@dataclass(frozen=True)
class InclusiveNights:
    """fetch_rooms_values and fetch_plan_prices. Both ends are nights."""

    first: date
    last: date

    def __post_init__(self) -> None:
        if self.last < self.first:
            raise ValueError("InclusiveNights.last before first")

    def length(self) -> int:
        return (self.last - self.first).days + 1


@dataclass(frozen=True)
class ReservationSpan:
    """new_reservation. dto is check-out morning (official 1-night example)."""

    dfrom: date
    dto: date


@dataclass(frozen=True)
class Party:
    adults: int
    children: int

    def __post_init__(self) -> None:
        if self.adults < 1 or self.children < 0:
            raise ValueError("Party.adults >= 1, children >= 0")

    @property
    def size(self) -> int:
        return self.adults + self.children


# ===== desk/_catalog.py =====


@dataclass(frozen=True)
class Room:
    id: RoomId
    name: str
    occupancy: int
    pool_id: RoomId  # physical room. subroom==0 => self, else parent id


@dataclass(frozen=True)
class Pool:
    """One availability bucket. Spoken when two YAML-true rooms share it."""

    id: RoomId
    room_ids: tuple[RoomId, ...]

    def dit(self, names: Sequence[str]) -> str:
        """'Double Balcon et Airbnb Double partagent le même stock.'"""
        raise NotImplementedError


@dataclass(frozen=True)
class Catalog:
    rooms: tuple[Room, ...]
    pools: tuple[Pool, ...]

    def room(self, room_id: RoomId) -> Room:
        raise NotImplementedError

    def pool(self, room_id: RoomId) -> Pool:
        raise NotImplementedError


@dataclass(frozen=True)
class Night:
    """One room, one night. No price field. Plan0 owns rates."""

    avail: int
    closed: bool
    closed_arrival: bool
    closed_departure: bool
    min_stay: int
    max_stay: int
    min_stay_arrival: int


NightGrid = Mapping[RoomId, tuple[Night, ...]]  # length == InclusiveNights.length
Plan0 = Mapping[RoomId, tuple[Euro, ...]]  # same alignment. pid is always 0.


def bind_parent_avail(catalog: Catalog, grid: NightGrid) -> NightGrid:
    """Copy parent avail onto virtual rooms. Quote then has no parent branch."""
    raise NotImplementedError


def parse_catalog(raw_rooms: list[dict]) -> Catalog:
    """XML-RPC structs -> Room. subroom 0 is physical. Keys become int RoomId."""
    raise NotImplementedError


# ===== desk/_sell.py =====
# Unsellable mark #1. Absent or false => not sold. UI writes this file.


SellMap = Mapping[RoomId, bool]


def load_sell(path: Path) -> SellMap:
    raise NotImplementedError


def write_sell(path: Path, mapping: SellMap) -> None:
    """Atomic temp + rename. One list. No second copy in code."""
    raise NotImplementedError


@dataclass(frozen=True)
class SellSnapshot:
    """Frozen trues. False and absent are the same for quote."""

    for_sale: FrozenSet[RoomId]

    def offers(self, room_id: RoomId) -> bool:
        return room_id in self.for_sale


# ===== desk/_quote.py =====
# Price source and refusal table. Engineer questions #1 and #2 land here.


Predicate = Callable[
    [Room, Stay, Party, Sequence[Night], Sequence[Euro], SellSnapshot],
    bool,
]


@dataclass(frozen=True)
class Check:
    """One refusal. id is the name in DESIGN.md. dit is spoken French."""

    id: str
    dit: str
    fails: Predicate


def fails_pas_en_vente(room, stay, party, nights, prices, sell) -> bool:
    raise NotImplementedError


def fails_occupancy(room, stay, party, nights, prices, sell) -> bool:
    raise NotImplementedError


def fails_closed(room, stay, party, nights, prices, sell) -> bool:
    raise NotImplementedError


def fails_closed_arrival(room, stay, party, nights, prices, sell) -> bool:
    raise NotImplementedError


def fails_closed_departure(room, stay, party, nights, prices, sell) -> bool:
    raise NotImplementedError


def fails_min_stay(room, stay, party, nights, prices, sell) -> bool:
    """True if any night min_stay or first-night min_stay_arrival exceeds stay.nights."""
    raise NotImplementedError


def fails_max_stay(room, stay, party, nights, prices, sell) -> bool:
    raise NotImplementedError


def fails_avail(room, stay, party, nights, prices, sell) -> bool:
    """min(nights.avail) < QUANTITY. Avail already parent-bound."""
    raise NotImplementedError


def fails_plan0(room, stay, party, nights, prices, sell) -> bool:
    raise NotImplementedError


# Single list. Adding a refusal is one tuple item, not an enum plus a parallel table.
CHECKS: tuple[Check, ...] = (
    Check("pas_en_vente", "cette chambre n'est pas en vente", fails_pas_en_vente),
    Check("trop_de_personnes", "trop de personnes", fails_occupancy),
    Check("fermee", "chambre fermée ces nuits-là", fails_closed),
    Check("arrivee_fermee", "arrivée fermée ce jour-là", fails_closed_arrival),
    Check("depart_ferme", "départ fermé ce jour-là", fails_closed_departure),
    Check("sejour_trop_court", "séjour trop court", fails_min_stay),
    Check("sejour_trop_long", "séjour trop long", fails_max_stay),
    Check("plus_de_stock", "plus de stock", fails_avail),
    Check("prix_invalide", "prix invalide", fails_plan0),
)


@dataclass(frozen=True)
class Offer:
    room: Room
    stay: Stay
    party: Party
    nightly: tuple[Euro, ...]  # plan 0, one per inclusive night
    total: Euro
    pool: Pool

    @property
    def nights(self) -> int:
        return self.stay.nights


@dataclass(frozen=True)
class Refusal:
    room: Room
    stay: Stay
    party: Party
    reason: Check
    pool: Pool


Row = Union[Offer, Refusal]


@dataclass(frozen=True)
class Quote:
    stay: Stay
    party: Party
    rows: tuple[Row, ...]
    dit: str  # full spoken French. Voice reads this. Model does not compose euros.
    pool_notes: tuple[str, ...]

    def offer_for(self, room_id: RoomId) -> Row:
        raise NotImplementedError


def plan0_total(nightly: tuple[Euro, ...]) -> Euro:
    """Sum of WuBook Parity (pid 0) over InclusiveNights. Never Night.price."""
    raise NotImplementedError


def quote_room(
    room: Room,
    stay: Stay,
    party: Party,
    nights: Sequence[Night],
    prices: Sequence[Euro],
    sell: SellSnapshot,
    pool: Pool,
) -> Row:
    """Walk CHECKS. First failure is the Refusal. Else Offer with plan0_total."""
    raise NotImplementedError


def speak(rows: Sequence[Row], pool_notes: Sequence[str]) -> str:
    """Offers first (name, nights, euros), then pool notes, else first refusals."""
    raise NotImplementedError


def build_quote(
    catalog: Catalog,
    sell: SellSnapshot,
    stay: Stay,
    party: Party,
    grid: NightGrid,
    plan0: Plan0,
) -> Quote:
    raise NotImplementedError


# ===== desk/_ledger.py =====


class LedgerState(Enum):
    PENDING = "pending"
    DONE = "done"


@dataclass(frozen=True)
class LedgerRow:
    key: IdempotencyKey
    state: LedgerState
    code: ReservationCode | None


class Ledger:
    """One writer. pending before Wired. done after. Repeat of done returns code."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, key: IdempotencyKey) -> LedgerRow | None:
        raise NotImplementedError

    def mark_pending(self, key: IdempotencyKey) -> None:
        """TODO crash window: pending + no Wired call yet is safe to leave."""
        raise NotImplementedError

    def mark_done(self, key: IdempotencyKey, code: ReservationCode) -> None:
        """Temp file + rename. Repeat book returns this code."""
        raise NotImplementedError


# ===== public types used by Wired and Desk =====


class Origin(Enum):
    VOIX = "voix"
    COMPTOIR = "comptoir"


@dataclass(frozen=True)
class Guest:
    """Wired identity. Logistics optional. Voice can book the subset."""

    lname: str
    fname: str
    email: str
    country: str  # ISO-2
    city: str | None = None
    phone: str | None = None
    street: str | None = None
    arrival_hour: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class Booking:
    code: ReservationCode
    offer: Offer
    guest: Guest
    dit: str


class StaleOffer(Exception):
    def __init__(self, row: Refusal) -> None:
        super().__init__(row.reason.dit)
        self.row = row


class PendingBooking(Exception):
    """Same key is in-flight. Do not call Wired again."""

    def __init__(self, key: IdempotencyKey) -> None:
        super().__init__("réservation déjà en cours")
        self.key = key


# ===== desk/_wired.py =====


def european_date(day: date) -> str:
    """DD/MM/YYYY. The only formatter. Not imported by quote or HTTP."""
    raise NotImplementedError


def euro_to_wired(amount: Euro) -> float:
    """Float exists on this call only."""
    raise NotImplementedError


def guest_to_wired(guest: Guest) -> dict[str, str]:
    """Omit empty optionals. Never a card field."""
    raise NotImplementedError


@dataclass(frozen=True)
class Creds:
    lcode: int
    token: str | None  # WUBOOK_TOKEN when set
    user: str | None
    password: str | None
    pkey: str | None

    @staticmethod
    def from_env() -> "Creds":
        raise NotImplementedError


class WiredError(Exception):
    """code < 0. payload is the human-readable string."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Wired:
    """Process-lifetime XML-RPC at https://wired.wubook.net/xrws/ (slash required).

    Four methods only. No ignore flags, no card, no price writes.
    """

    URL = "https://wired.wubook.net/xrws/"

    def __init__(self, creds: Creds) -> None:
        self.creds = creds
        self._token: str | None = None
        self._owns_session = False

    def open(self) -> None:
        """Use WUBOOK_TOKEN, else acquire_token once. Never per HTTP request."""
        raise NotImplementedError

    def close(self) -> None:
        """release_token only if we acquired. Permanent tokens stay."""
        raise NotImplementedError

    def fetch_catalog(self) -> Catalog:
        """fetch_rooms. Cache hours. Limit 240 / 86400s."""
        raise NotImplementedError

    def fetch_nights(self, span: InclusiveNights) -> NightGrid:
        """fetch_rooms_values. Drop the price key at parse. Limit 240 / 43200s."""
        raise NotImplementedError

    def fetch_plan0(self, span: InclusiveNights, rooms: Sequence[RoomId]) -> Plan0:
        """fetch_plan_prices pid=0. List length must equal span.length."""
        raise NotImplementedError

    def nights_and_plan0(
        self, span: InclusiveNights, rooms: Sequence[RoomId]
    ) -> tuple[NightGrid, Plan0]:
        """One cache key (first, last). Coalesce in-flight. Two RPCs max."""
        raise NotImplementedError

    def create_reservation(
        self,
        span: ReservationSpan,
        room_id: RoomId,
        guest: Guest,
        amount: Euro,
        origin: Origin,
        party: Party,
        key: IdempotencyKey,
    ) -> ReservationCode:
        """Hardcodes rooms={id:[1,'nb']}, ccard=0, ignore_restrs=0,
        ignore_avail=0, status=1. ancillary stores the idempotency key.
        Flags are not parameters. An agent cannot pass them.
        """
        raise NotImplementedError


# ===== desk/__init__.py (public) =====


class Desk:
    """The deep module. HTTP and NiceGUI call this. They do not call Wired."""

    def __init__(self, wired: Wired, sell_path: Path, ledger: Ledger) -> None:
        self._wired = wired
        self._sell_path = sell_path
        self._ledger = ledger

    @staticmethod
    def open(sell_path: Path, ledger_path: Path) -> "Desk":
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def rooms(self) -> tuple[tuple[Room, bool], ...]:
        """Catalog rooms with current sell flag. For the UI switches."""
        raise NotImplementedError

    def set_sell(self, room_id: RoomId, sell: bool) -> None:
        """UI only. Writes sell.yml. Voice routes do not expose this."""
        raise NotImplementedError

    def quote(self, stay: Stay, party: Party) -> Quote:
        """Every room is Offer or Refusal. Uses plan 0 and YAML snapshot."""
        raise NotImplementedError

    def book(
        self,
        stay: Stay,
        party: Party,
        room_id: RoomId,
        guest: Guest,
        key: IdempotencyKey,
        origin: Origin,
    ) -> Booking:
        """Quote again. Book only if that row is still an Offer.
        Amount from the fresh plan-0 total. Ledger pending -> Wired -> done.
        Repeat key returns the stored code and does not call Wired.
        """
        raise NotImplementedError

    def refresh_catalog(self) -> Catalog:
        """UI Rafraîchir. Search does not call fetch_rooms."""
        raise NotImplementedError


# ===== desk/_http.py =====


def mount_voice(app: object, desk: Desk) -> None:
    """POST /voix/chercher  JSON arrivee, depart, adultes, enfants -> Quote json
    POST /voix/reserver   JSON id + stay + guest, header Idempotency-Key
    Parse ISO dates to Stay. Never take amount, ccard, ignore_*, status.
    Room names come from chercher. No second list route.
    """
    raise NotImplementedError


def chercher_body(quote: Quote) -> dict:
    raise NotImplementedError


def reserver_body(booking: Booking) -> dict:
    raise NotImplementedError


# ===== desk/_ui.py =====


def mount_ui(desk: Desk) -> None:
    """French. Page chambres (switches). Page séjour (pickers + Chercher).
    Search is a submit, not a keystroke. Réserver on an Offer row only.
    """
    raise NotImplementedError


# ===== desk/__main__.py =====


def main() -> None:
    """One process, one Desk, one token. NiceGUI and /voix/* share memory."""
    raise NotImplementedError
