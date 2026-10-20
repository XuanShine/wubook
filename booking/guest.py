from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Mapping, assert_never
from uuid import uuid4

import yaml

from booking.model import (
    Cents,
    ClosedArrival,
    ClosedDeparture,
    ClosedNight,
    Guest,
    IdempotencyKey,
    MaxStay,
    MinStay,
    MinStayArrival,
    MissingPrice,
    Party,
    PastArrival,
    Refusal,
    Reservation,
    RoomId,
    ShortAvailability,
    Stay,
    TooLong,
    TooManyPeople,
    UnknownRoom,
)
from booking.service import (
    Board,
    BookableRoom,
    Counter,
    InFlight,
    KeyUsed,
    Rejected,
    WiredDown,
    WiredRejected,
    WireShape,
)

_EXTRAS_PATH = Path(__file__).resolve().parent / "extras.yml"
_ROOMS_PATH = Path(__file__).resolve().parent.parent / "room_config.yml"
_HOUR = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_IGNORED = {"amount", "total_cents", "currency"}
_NONE_BOOKABLE = "Aucune chambre n'est réservable pour ces dates."
_IN_FLIGHT = "Votre demande est déjà en cours. Aucune seconde réservation ne sera créée."
_REJECTED = "La réservation n'a pas été acceptée."
_DOWN = "Le service de réservation est momentanément indisponible."
_SHAPE = "Le service de réservation a renvoyé une réponse inattendue."
_KEY_USED = "Cette demande a déjà été utilisée avec d'autres informations."
_HIDDEN = "Cette chambre n'est pas proposée."
_EMPTY_EXTRAS = "Aucun extra n'est proposé pour le moment."
_PER = ("stay", "person", "person_night")


class BlockKind(Enum):
    IN_FLIGHT = "inflight"
    REJECTED = "rejected"
    KEY_USED = "key_used"
    REFUSAL = "refusal"
    DOWN = "down"
    HIDDEN = "hidden"


@dataclass(frozen=True)
class Extra:
    id: str
    label: str
    cents: Cents
    per: str

    def line_cents(self, stay: Stay) -> int:
        people = stay.party.size()
        nights = stay.night_count()
        scale = {"stay": 1, "person": people, "person_night": people * nights}[self.per]
        return int(self.cents) * scale


@dataclass(frozen=True)
class AskingStay:
    errors: tuple[str, ...] = ()
    check_in: str = ""
    checkout: str = ""
    adults: str = "2"
    children: str = "0"


@dataclass(frozen=True)
class ChoosingRoom:
    stay: Stay
    rooms: tuple[BookableRoom, ...]
    block: Refusal | None
    sentence: str


@dataclass(frozen=True)
class ChoosingExtras:
    stay: Stay
    room_id: RoomId
    room_name: str
    catalog: tuple[Extra, ...]
    selected: tuple[str, ...]
    notice: str = ""


@dataclass(frozen=True)
class ContactDraft:
    last_name: str = ""
    first_name: str = ""
    email: str = ""
    phone: str = ""
    arrival_hour: str = ""
    remarks: str = ""


@dataclass(frozen=True)
class AskingContact:
    stay: Stay
    room_id: RoomId
    room_name: str
    catalog: tuple[Extra, ...]
    selected: tuple[str, ...]
    draft: ContactDraft
    key: IdempotencyKey
    errors: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Done:
    reservation: Reservation
    stay: Stay
    room_id: RoomId
    room_name: str
    extras: tuple[Extra, ...]


@dataclass(frozen=True)
class Blocked:
    kind: BlockKind
    french: str
    stay: Stay | None
    room_id: RoomId | None
    room_name: str
    catalog: tuple[Extra, ...]
    selected: tuple[str, ...]
    draft: ContactDraft
    key: IdempotencyKey | None
    wired_message: str = ""
    retry: bool = False

    @property
    def allow_back(self) -> bool:
        return self.kind is not BlockKind.IN_FLIGHT


Wizard = AskingStay | ChoosingRoom | ChoosingExtras | AskingContact | Done | Blocked


@dataclass(frozen=True)
class _Ready:
    stay: Stay
    room_id: RoomId
    room_name: str
    selected: tuple[str, ...]
    draft: ContactDraft
    extras: tuple[Extra, ...]


def load_extras(path: Path | None = None) -> tuple[Extra, ...]:
    target = _EXTRAS_PATH if path is None else path
    if not target.exists():
        return ()
    try:
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("extras") from exc
    if loaded is None:
        return ()
    if not isinstance(loaded, list):
        raise ValueError("extras")
    seen: set[str] = set()
    catalog: list[Extra] = []
    for item in loaded:
        if not isinstance(item, dict):
            raise ValueError("extras")
        try:
            extra_id = item["id"]
            label = item["label"]
            cents = item["cents"]
            per = item["per"]
        except KeyError as exc:
            raise ValueError("extras") from exc
        if not isinstance(extra_id, str) or extra_id == "" or extra_id in seen:
            raise ValueError("extras")
        if not isinstance(label, str) or label.strip() == "":
            raise ValueError("extras")
        if not isinstance(per, str) or per not in _PER:
            raise ValueError("extras")
        try:
            amount = Cents(cents)
        except ValueError as exc:
            raise ValueError("extras") from exc
        seen.add(extra_id)
        catalog.append(Extra(extra_id, label, amount, per))
    return tuple(catalog)


def hide_rooms(path: Path | None = None) -> frozenset[RoomId]:
    target = _ROOMS_PATH if path is None else path
    if not target.exists():
        return frozenset()
    try:
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("hide_rooms") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("hide_rooms"), list):
        raise ValueError("hide_rooms")
    hidden: list[RoomId] = []
    for item in loaded["hide_rooms"]:
        try:
            hidden.append(RoomId(item))
        except ValueError as exc:
            raise ValueError("hide_rooms") from exc
    return frozenset(hidden)


def euros(cents: int) -> str:
    whole, part = divmod(int(cents), 100)
    return f"{whole},{part:02d} €"


def french_refusal(result: Refusal) -> str:
    if isinstance(result, PastArrival):
        return "La date d'arrivée est passée."
    if isinstance(result, TooLong):
        return "Le séjour dépasse deux ans."
    if isinstance(result, TooManyPeople):
        return f"Cette chambre accueille {result.occupancy} personnes."
    if isinstance(result, ClosedNight):
        return "La chambre est fermée sur ce séjour."
    if isinstance(result, ClosedArrival):
        return "L'arrivée n'est pas possible ce jour-là."
    if isinstance(result, ClosedDeparture):
        return "Le départ n'est pas possible ce jour-là."
    if isinstance(result, MinStay):
        return f"Ce séjour demande au moins {result.required} nuits."
    if isinstance(result, MinStayArrival):
        return f"Une arrivée ce jour-là demande au moins {result.required} nuits."
    if isinstance(result, MaxStay):
        return f"Ce séjour est limité à {result.limit} nuits."
    if isinstance(result, ShortAvailability):
        word = "chambre" if result.available == 1 else "chambres"
        return f"Il ne reste que {result.available} {word} pour ces dates."
    if isinstance(result, MissingPrice):
        return "Cette chambre n'a pas de prix pour ces dates."
    if isinstance(result, UnknownRoom):
        return "Cette chambre n'existe plus."
    assert_never(result)


def desk_line(extras: tuple[Extra, ...], stay: Stay) -> str:
    parts = [f"{extra.label} ({euros(extra.line_cents(stay))})" for extra in extras]
    return "Extras à régler à l'hôtel : " + ", ".join(parts)


def guest_notes(remarks: str, extras: tuple[Extra, ...], stay: Stay) -> str:
    if not extras:
        return remarks
    line = desk_line(extras, stay)
    if remarks == "":
        return line
    return f"{remarks}\n{line}"


def step(
    counter: Counter,
    form: Mapping[str, object],
    today: date,
    *,
    extras_path: Path | None = None,
    rooms_path: Path | None = None,
    catalog: tuple[Extra, ...] | None = None,
    hidden: frozenset[RoomId] | None = None,
) -> Wizard:
    if catalog is None:
        catalog = load_extras(extras_path)
    if hidden is None:
        hidden = hide_rooms(rooms_path)
    fields = _fields(form)
    action = _first(fields, "action")
    if action == "stay":
        return _search(counter, fields, today, hidden)
    if action == "room":
        return _pick(fields, catalog, hidden)
    if action == "extras":
        return _choose_extras(fields, catalog, hidden)
    if action == "contact":
        return _submit_contact(counter, fields, today, catalog, hidden)
    if action == "back":
        return _back(fields, catalog, hidden)
    if action == "retry":
        return _retry(counter, fields, today, catalog, hidden)
    return _echo(fields)


def place(
    counter: Counter,
    stay: Stay,
    room_id: RoomId,
    room_name: str,
    draft: ContactDraft,
    extras: tuple[Extra, ...],
    key: IdempotencyKey,
    today: date,
    hidden: frozenset[RoomId],
) -> Done | Blocked:
    if room_id in hidden:
        return _blocked(BlockKind.HIDDEN, _HIDDEN, stay, room_id, room_name, (), (), draft, key, retry=False)
    guest = Guest(
        last_name=draft.last_name.strip(),
        first_name=draft.first_name.strip(),
        email=draft.email.strip(),
        phone=draft.phone.strip(),
        arrival_hour=draft.arrival_hour.strip(),
        notes=guest_notes(draft.remarks.strip(), extras, stay),
    )
    try:
        result = counter.reserve(stay, room_id, guest, key, today)
    except WiredDown:
        return _blocked(BlockKind.DOWN, _DOWN, stay, room_id, room_name, (), extras, draft, key, retry=True)
    except WireShape:
        return _blocked(BlockKind.DOWN, _SHAPE, stay, room_id, room_name, (), extras, draft, key, retry=True)
    except WiredRejected as rejected:
        return _blocked(
            BlockKind.REJECTED,
            _REJECTED,
            stay,
            room_id,
            room_name,
            (),
            extras,
            draft,
            key,
            wired_message=rejected.message,
            retry=True,
        )
    if isinstance(result, Reservation):
        return Done(result, stay, room_id, room_name, extras)
    if isinstance(result, InFlight):
        return _blocked(
            BlockKind.IN_FLIGHT,
            _IN_FLIGHT,
            stay,
            room_id,
            room_name,
            (),
            extras,
            draft,
            result.key,
            retry=True,
        )
    if isinstance(result, Rejected):
        return _blocked(
            BlockKind.REJECTED,
            _REJECTED,
            stay,
            room_id,
            room_name,
            (),
            extras,
            draft,
            key,
            wired_message=result.message,
            retry=True,
        )
    if isinstance(result, KeyUsed):
        return _blocked(BlockKind.KEY_USED, _KEY_USED, stay, room_id, room_name, (), extras, draft, key, retry=False)
    return _blocked(
        BlockKind.REFUSAL,
        french_refusal(result),
        stay,
        room_id,
        room_name,
        (),
        extras,
        draft,
        key,
        retry=False,
    )


def _blocked(
    kind: BlockKind,
    french: str,
    stay: Stay | None,
    room_id: RoomId | None,
    room_name: str,
    catalog: tuple[Extra, ...],
    extras: tuple[Extra, ...],
    draft: ContactDraft,
    key: IdempotencyKey | None,
    *,
    wired_message: str = "",
    retry: bool,
) -> Blocked:
    selected = tuple(extra.id for extra in extras)
    return Blocked(kind, french, stay, room_id, room_name, catalog, selected, draft, key, wired_message, retry)


def _search(
    counter: Counter,
    fields: dict[str, tuple[str, ...]],
    today: date,
    hidden: frozenset[RoomId],
) -> Wizard:
    parsed = _stay(fields)
    if isinstance(parsed, tuple):
        return _echo(fields, parsed)
    try:
        board = counter.bookable_rooms(parsed, today)
    except WiredDown:
        return _echo(fields, (_DOWN,))
    except WireShape:
        return _echo(fields, (_SHAPE,))
    except WiredRejected as rejected:
        message = rejected.message.strip()
        errors = (_REJECTED,) if message == "" else (_REJECTED, message)
        return _echo(fields, errors)
    if not isinstance(board, Board):
        return _echo(fields, (french_refusal(board),))
    visible = tuple(room for room in board.rooms if room.id not in hidden)
    block = None if visible else board.block
    sentence = ""
    if not visible:
        sentence = french_refusal(block) if block is not None else _NONE_BOOKABLE
    return ChoosingRoom(parsed, visible, block, sentence)


def _pick(
    fields: dict[str, tuple[str, ...]],
    catalog: tuple[Extra, ...],
    hidden: frozenset[RoomId],
) -> Wizard:
    parsed = _stay(fields)
    if isinstance(parsed, tuple):
        return _echo(fields, parsed)
    room_id = _room_id(_first(fields, "room_id"))
    if room_id is None or room_id in hidden:
        return ChoosingRoom(parsed, (), None, _HIDDEN)
    return _at_extras(parsed, room_id, _first(fields, "room_name"), catalog, ())


def _choose_extras(
    fields: dict[str, tuple[str, ...]],
    catalog: tuple[Extra, ...],
    hidden: frozenset[RoomId],
) -> Wizard:
    ready = _ready(fields, catalog, hidden)
    if not isinstance(ready, _Ready):
        return ready
    return AskingContact(
        ready.stay,
        ready.room_id,
        ready.room_name,
        catalog,
        ready.selected,
        ready.draft,
        IdempotencyKey(uuid4().hex),
    )


def _submit_contact(
    counter: Counter,
    fields: dict[str, tuple[str, ...]],
    today: date,
    catalog: tuple[Extra, ...],
    hidden: frozenset[RoomId],
) -> Wizard:
    ready = _ready(fields, catalog, hidden)
    if not isinstance(ready, _Ready):
        return ready
    errors = _contact_errors(ready.draft)
    key = _posted_key(fields)
    if key is None:
        # place never mints. An empty key is a forged post, not a new attempt.
        return AskingContact(
            ready.stay,
            ready.room_id,
            ready.room_name,
            catalog,
            ready.selected,
            ready.draft,
            IdempotencyKey(uuid4().hex),
            errors + (("key", "Envoyez à nouveau le formulaire."),),
        )
    if errors:
        return AskingContact(
            ready.stay,
            ready.room_id,
            ready.room_name,
            catalog,
            ready.selected,
            ready.draft,
            key,
            errors,
        )
    return place(
        counter,
        ready.stay,
        ready.room_id,
        ready.room_name,
        _clean_draft(ready.draft),
        ready.extras,
        key,
        today,
        hidden,
    )


def _back(
    fields: dict[str, tuple[str, ...]],
    catalog: tuple[Extra, ...],
    hidden: frozenset[RoomId],
) -> Wizard:
    if _first(fields, "outcome") == BlockKind.IN_FLIGHT.value:
        ready = _ready(fields, catalog, hidden)
        stay = ready.stay if isinstance(ready, _Ready) else None
        room_id = ready.room_id if isinstance(ready, _Ready) else None
        room_name = ready.room_name if isinstance(ready, _Ready) else ""
        selected = ready.selected if isinstance(ready, _Ready) else ()
        draft = ready.draft if isinstance(ready, _Ready) else _draft(fields)
        return Blocked(
            BlockKind.IN_FLIGHT,
            _IN_FLIGHT,
            stay,
            room_id,
            room_name,
            catalog,
            selected,
            draft,
            _posted_key(fields),
            retry=_posted_key(fields) is not None,
        )
    ready = _ready(fields, catalog, hidden)
    if isinstance(ready, _Ready):
        return _at_extras(ready.stay, ready.room_id, ready.room_name, catalog, ready.selected)
    if isinstance(ready, ChoosingRoom):
        return _echo(fields)
    return ready


def _retry(
    counter: Counter,
    fields: dict[str, tuple[str, ...]],
    today: date,
    catalog: tuple[Extra, ...],
    hidden: frozenset[RoomId],
) -> Wizard:
    if _first(fields, "outcome") not in {
        BlockKind.IN_FLIGHT.value,
        BlockKind.REJECTED.value,
        BlockKind.DOWN.value,
    }:
        return _echo(fields)
    return _submit_contact(counter, fields, today, catalog, hidden)


def _ready(
    fields: dict[str, tuple[str, ...]],
    catalog: tuple[Extra, ...],
    hidden: frozenset[RoomId],
) -> _Ready | Wizard:
    parsed = _stay(fields)
    if isinstance(parsed, tuple):
        return _echo(fields, parsed)
    room_id = _room_id(_first(fields, "room_id"))
    if room_id is None or room_id in hidden:
        return ChoosingRoom(parsed, (), None, _HIDDEN)
    selected = _selected(fields, catalog)
    if selected is None:
        return _at_extras(
            parsed,
            room_id,
            _first(fields, "room_name"),
            catalog,
            (),
            "Cet extra n'est plus proposé.",
        )
    wanted = set(selected)
    extras = tuple(extra for extra in catalog if extra.id in wanted)
    return _Ready(parsed, room_id, _first(fields, "room_name"), selected, _draft(fields), extras)


def _at_extras(
    stay: Stay,
    room_id: RoomId,
    room_name: str,
    catalog: tuple[Extra, ...],
    selected: tuple[str, ...],
    notice: str = "",
) -> ChoosingExtras:
    if notice == "" and not catalog:
        notice = _EMPTY_EXTRAS
    return ChoosingExtras(stay, room_id, room_name, catalog, selected, notice)


def _stay(fields: dict[str, tuple[str, ...]]) -> Stay | tuple[str, ...]:
    check_in = _first(fields, "check_in").strip()
    checkout = _first(fields, "checkout").strip()
    adults = _first(fields, "adults").strip()
    children = _first(fields, "children").strip()
    errors: list[str] = []
    start = _day(check_in)
    end = _day(checkout)
    if start is None:
        errors.append("Indiquez une date d'arrivée.")
    if end is None:
        errors.append("Indiquez une date de départ.")
    grown = _count(adults)
    kids = 0 if children == "" else _count(children)
    if grown is None:
        errors.append("Indiquez le nombre d'adultes.")
    if kids is None:
        errors.append("Indiquez le nombre d'enfants.")
    if errors or start is None or end is None or grown is None or kids is None:
        return tuple(errors)
    if grown + kids < 1:
        return ("Indiquez au moins une personne.",)
    try:
        return Stay(start, end, Party(adults=grown, children=kids), 1)
    except ValueError:
        return ("Le départ est après l'arrivée.",)


def _contact_errors(draft: ContactDraft) -> tuple[tuple[str, str], ...]:
    errors: list[tuple[str, str]] = []
    if draft.last_name.strip() == "":
        errors.append(("last_name", "Indiquez un nom."))
    if draft.first_name.strip() == "":
        errors.append(("first_name", "Indiquez un prénom."))
    email = draft.email.strip()
    local, mark, domain = email.partition("@")
    if mark != "@" or local == "" or "@" in domain or "." not in domain:
        errors.append(("email", "Indiquez un e-mail valide."))
    phone = draft.phone.strip()
    digits = phone[1:] if phone.startswith("+") else phone
    if not digits.isdecimal() or not 8 <= len(digits) <= 15:
        errors.append(("phone", "Indiquez un téléphone de 8 à 15 chiffres."))
    if _HOUR.fullmatch(draft.arrival_hour.strip()) is None:
        errors.append(("arrival_hour", "Indiquez une heure d'arrivée."))
    if len(draft.remarks) > 500:
        errors.append(("remarks", "Les remarques tiennent en 500 caractères."))
    return tuple(errors)


def _clean_draft(draft: ContactDraft) -> ContactDraft:
    return ContactDraft(
        draft.last_name.strip(),
        draft.first_name.strip(),
        draft.email.strip(),
        draft.phone.strip(),
        draft.arrival_hour.strip(),
        draft.remarks.strip(),
    )


def _day(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _count(text: str) -> int | None:
    if not text.isdecimal():
        return None
    return int(text)


def _fields(form: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    parsed: dict[str, tuple[str, ...]] = {}
    for key, raw in form.items():
        if not isinstance(key, str) or key in _IGNORED:
            continue
        parsed[key] = _texts(raw)
    return parsed


def _texts(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    if raw is None:
        return ()
    return (str(raw),)


def _first(fields: dict[str, tuple[str, ...]], key: str) -> str:
    values = fields.get(key, ())
    return values[0] if values else ""


def _room_id(text: str) -> RoomId | None:
    try:
        return RoomId(text.strip())
    except ValueError:
        return None


def _posted_key(fields: dict[str, tuple[str, ...]]) -> IdempotencyKey | None:
    text = _first(fields, "key").strip()
    if text == "":
        return None
    return IdempotencyKey(text)


def _draft(fields: dict[str, tuple[str, ...]]) -> ContactDraft:
    return ContactDraft(
        _first(fields, "last_name"),
        _first(fields, "first_name"),
        _first(fields, "email"),
        _first(fields, "phone"),
        _first(fields, "arrival_hour"),
        _first(fields, "remarks"),
    )


def _selected(fields: dict[str, tuple[str, ...]], catalog: tuple[Extra, ...]) -> tuple[str, ...] | None:
    known = {extra.id for extra in catalog}
    picked: list[str] = []
    for item in fields.get("extra", ()):
        if item == "":
            continue
        if item not in known:
            return None
        if item not in picked:
            picked.append(item)
    return tuple(picked)


def _echo(fields: dict[str, tuple[str, ...]], errors: tuple[str, ...] = ()) -> AskingStay:
    return AskingStay(
        errors,
        _first(fields, "check_in"),
        _first(fields, "checkout"),
        _first(fields, "adults") if "adults" in fields else "2",
        _first(fields, "children") if "children" in fields else "0",
    )
