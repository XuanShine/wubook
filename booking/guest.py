from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Mapping, assert_never
from urllib.parse import parse_qs
from uuid import uuid4

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from booking.http import create_app
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
_HOUR_WITH_SECONDS = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$")
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
    draft: ContactDraft = field(default_factory=lambda: ContactDraft())


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
    if _posted_key(fields) is None:
        return _echo(fields)
    ready = _ready(fields, catalog, hidden)
    if isinstance(ready, _Ready):
        return _at_extras(
            ready.stay,
            ready.room_id,
            ready.room_name,
            catalog,
            ready.selected,
            draft=ready.draft,
        )
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
    draft: ContactDraft | None = None,
) -> ChoosingExtras:
    if notice == "" and not catalog:
        notice = _EMPTY_EXTRAS
    return ChoosingExtras(stay, room_id, room_name, catalog, selected, notice, draft or ContactDraft())


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
    phone = _phone(draft.phone)
    digits = phone[1:] if phone.startswith("+") else phone
    if not digits.isdecimal() or not 8 <= len(digits) <= 15:
        errors.append(("phone", "Indiquez un téléphone de 8 à 15 chiffres."))
    if _arrival(draft.arrival_hour) is None:
        errors.append(("arrival_hour", "Indiquez une heure d'arrivée."))
    if len(draft.remarks) > 500:
        errors.append(("remarks", "Les remarques tiennent en 500 caractères."))
    return tuple(errors)


def _clean_draft(draft: ContactDraft) -> ContactDraft:
    return ContactDraft(
        draft.last_name.strip(),
        draft.first_name.strip(),
        draft.email.strip(),
        _phone(draft.phone),
        _arrival(draft.arrival_hour) or draft.arrival_hour.strip(),
        draft.remarks.strip(),
    )


def _phone(text: str) -> str:
    stripped = text.strip()
    plus = stripped.startswith("+")
    digits = "".join(ch for ch in stripped if ch.isdecimal())
    if plus:
        return f"+{digits}"
    return digits


def _arrival(text: str) -> str | None:
    stripped = text.strip()
    if _HOUR.fullmatch(stripped):
        return stripped
    if _HOUR_WITH_SECONDS.fullmatch(stripped):
        return stripped[:5]
    return None


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
_STEPS = ("Dates", "Chambres", "Extras", "Coordonnées", "Confirmation")
_STYLE = """
:root {
  --bg: #f3efe6;
  --ink: #1e2a24;
  --pine: #1f4d3a;
  --brass: #a6844a;
  --card: #fffcf7;
  --error: #8f2d2d;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: "Avenir Next", "Segoe UI", sans-serif;
  line-height: 1.45;
}
.wrap { max-width: 72rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
.where {
  margin: 0 0 0.35rem;
  color: var(--brass);
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-size: 0.78rem;
}
.hotel {
  margin: 0;
  font-family: "Iowan Old Style", Palatino, Georgia, serif;
  font-size: 3.1rem;
  font-weight: 500;
  line-height: 1.05;
  letter-spacing: -0.02em;
}
.lead { max-width: 38rem; margin: 0.8rem 0 0; }
.rule { height: 1px; margin: 1.2rem 0 1.4rem; border: 0; background: var(--brass); }
.steps {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 1rem;
  margin: 0 0 1.6rem;
  padding: 0;
  list-style: none;
}
.steps li {
  min-height: 48px;
  display: flex;
  align-items: center;
  gap: 0.45rem;
  color: #667068;
  border-bottom: 2px solid transparent;
}
.steps li span {
  width: 1.6rem;
  height: 1.6rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  border: 1px solid currentColor;
  font-size: 0.8rem;
}
.steps .done { color: var(--ink); }
.steps [aria-current="step"] { color: var(--pine); border-bottom-color: var(--brass); font-weight: 600; }
.panel {
  background: var(--card);
  border: 1px solid #e6dcc8;
  border-radius: 18px;
  padding: 1.4rem 1.3rem 1.5rem;
}
.panel h2 {
  margin: 0 0 0.9rem;
  font-family: "Iowan Old Style", Palatino, Georgia, serif;
  font-size: 2rem;
  font-weight: 500;
}
.dates, .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 0.9rem; }
.rooms { display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: 1rem; }
label { display: block; margin-bottom: 0.85rem; }
.label { display: block; margin-bottom: 0.35rem; }
input, select, textarea, button { font: inherit; color: inherit; }
input[type="text"], input[type="email"], input[type="tel"], input[type="date"],
input[type="time"], input[type="number"], textarea, button { min-height: 48px; }
input[type="text"], input[type="email"], input[type="tel"], input[type="date"],
input[type="time"], input[type="number"], textarea {
  width: 100%;
  padding: 0.55rem 0.75rem;
  border: 1px solid #d9d0c0;
  border-radius: 12px;
  background: #fff;
}
textarea { min-height: 7.5rem; resize: vertical; }
button { padding: 0 1.3rem; border-radius: 999px; cursor: pointer; }
button.primary { background: var(--pine); color: var(--card); border: 0; }
button.secondary { background: transparent; color: var(--pine); border: 1px solid var(--pine); }
.actions { display: flex; flex-wrap: wrap; gap: 0.7rem; margin-top: 0.4rem; }
.card {
  background: var(--bg);
  border-radius: 16px;
  padding: 1.1rem 1.1rem 1.2rem;
  display: flex;
  flex-direction: column;
  min-height: 100%;
}
.card h3 {
  margin: 0;
  font-family: "Iowan Old Style", Palatino, Georgia, serif;
  font-size: 1.7rem;
  font-weight: 500;
}
.price { margin: 0.45rem 0; color: var(--pine); font-size: 1.45rem; }
.meta { margin: 0; color: #3d4a43; }
.error { color: var(--error); margin: 0 0 0.8rem; }
.quiet { margin: 0 0 1rem; }
.check {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  min-height: 48px;
  margin: 0 0 0.35rem;
}
.check input { width: 48px; height: 48px; min-height: 48px; accent-color: var(--pine); }
.code {
  margin: 0.2rem 0 1rem;
  font-family: "Iowan Old Style", Palatino, Georgia, serif;
  font-size: 2.4rem;
}
a { color: var(--pine); text-underline-offset: 0.18em; }
input:focus, textarea:focus, button:focus { outline: 2px solid var(--brass); outline-offset: 2px; }
@media (max-width: 700px) {
  .rooms, .dates, .pair { grid-template-columns: 1fr; }
  button.primary { width: 100%; }
  .hotel { font-size: 2.35rem; }
  .wrap { padding-top: 1.3rem; }
}
"""


def render(state: Wizard) -> str:
    return _document(_step_number(state), _main(state))


def guest_app(
    counter: Counter,
    extras_path: Path | None = None,
    rooms_path: Path | None = None,
) -> FastAPI:
    catalog = load_extras(extras_path)
    hidden = hide_rooms(rooms_path)
    app = create_app(counter)

    @app.get("/")
    def guest_home() -> HTMLResponse:
        return HTMLResponse(render(AskingStay()))

    @app.post("/sejour")
    async def guest_stay(request: Request) -> HTMLResponse:
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True)
        try:
            state = step(counter, form, date.today(), catalog=catalog, hidden=hidden)
        except (WiredDown, WireShape, WiredRejected) as exc:
            state = _wire_stay(exc)
        return HTMLResponse(render(state))

    return app


def _wire_stay(exc: WiredDown | WireShape | WiredRejected) -> AskingStay:
    if isinstance(exc, WiredRejected):
        message = exc.message.strip()
        if message == "":
            return AskingStay((_REJECTED,))
        return AskingStay((_REJECTED, message))
    if isinstance(exc, WireShape):
        return AskingStay((_SHAPE,))
    return AskingStay((_DOWN,))


def _document(current: int, main: str) -> str:
    return (
        "<!DOCTYPE html>"
        '<html lang="fr">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Hôtel Panorama, Grasse</title>"
        f"<style>{_STYLE}</style>"
        "</head>"
        "<body>"
        '<div class="wrap">'
        '<p class="where">Grasse</p>'
        '<h1 class="hotel">Hôtel Panorama</h1>'
        '<p class="lead">Réservez une chambre pour votre séjour. '
        "Le petit-déjeuner, le parking et le lit bébé se règlent à l'hôtel.</p>"
        '<hr class="rule">'
        f"{_step_list(current)}"
        f'<div class="panel">{main}</div>'
        "</div>"
        "</body>"
        "</html>"
    )


def _step_number(state: Wizard) -> int:
    if isinstance(state, AskingStay):
        return 1
    if isinstance(state, ChoosingRoom):
        return 2
    if isinstance(state, ChoosingExtras):
        return 3
    if isinstance(state, AskingContact):
        return 4
    return 5


def _step_list(current: int) -> str:
    items: list[str] = []
    for index, label in enumerate(_STEPS, start=1):
        if index == current:
            items.append(f'<li aria-current="step"><span>{index}</span> {_text(label)}</li>')
        elif index < current:
            items.append(f'<li class="done"><span>{index}</span> {_text(label)}</li>')
        else:
            items.append(f"<li><span>{index}</span> {_text(label)}</li>")
    return '<ol class="steps">' + "".join(items) + "</ol>"


def _main(state: Wizard) -> str:
    if isinstance(state, AskingStay):
        return _dates(state)
    if isinstance(state, ChoosingRoom):
        return _rooms(state)
    if isinstance(state, ChoosingExtras):
        return _extras_page(state)
    if isinstance(state, AskingContact):
        return _contact_page(state)
    if isinstance(state, Done):
        return _done(state)
    return _blocked_page(state)


def _dates(state: AskingStay) -> str:
    errors = "".join(_error(item) for item in state.errors)
    return (
        "<h2>Vos dates</h2>"
        f"{errors}"
        '<form method="post" action="/sejour">'
        '<div class="dates">'
        f"{_field('Arrivée', 'check_in', 'date', state.check_in, required=True)}"
        f"{_field('Départ', 'checkout', 'date', state.checkout, required=True)}"
        "</div>"
        '<div class="pair">'
        f"{_field('Adultes', 'adults', 'number', state.adults, required=True, min_value='1')}"
        f"{_field('Enfants', 'children', 'number', state.children, required=True, min_value='0')}"
        "</div>"
        f'<div class="actions">{_button("Voir les chambres", "stay")}</div>'
        "</form>"
    )


def _rooms(state: ChoosingRoom) -> str:
    banner = _error(state.sentence) if state.sentence else ""
    cards = "".join(_room_card(state, room) for room in state.rooms)
    back = (
        '<form method="post" action="/sejour">'
        f"{''.join(_stay_fields(state.stay))}"
        f'<div class="actions">{_button("Retour", "back", primary=False)}</div>'
        "</form>"
    )
    return f"<h2>Chambres</h2>{banner}<div class=\"rooms\">{cards}</div>{back}"


def _room_card(state: ChoosingRoom, room: BookableRoom) -> str:
    people = "1 personne" if room.occupancy == 1 else f"{room.occupancy} personnes"
    nights = "1 nuit" if room.nights == 1 else f"{room.nights} nuits"
    return (
        '<form method="post" action="/sejour" class="card">'
        f"{''.join(_stay_fields(state.stay))}"
        f"{_h('room_id', int(room.id))}"
        f"{_h('room_name', room.name)}"
        f"<h3>{_text(room.name)}</h3>"
        f'<p class="meta">Jusqu\'à {_text(people)}. {_text(nights)}.</p>'
        f'<p class="price">{_text(euros(int(room.total_cents)))}</p>'
        f'<div class="actions">{_button("Choisir", "room")}</div>'
        "</form>"
    )


def _extras_page(state: ChoosingExtras) -> str:
    if state.notice == _EMPTY_EXTRAS:
        note = f'<p class="quiet">{_text(state.notice)}</p>'
    else:
        note = _error(state.notice)
    boxes = "".join(_extra_box(state, extra) for extra in state.catalog)
    hidden = "".join(_stay_fields(state.stay) + _room_fields(state) + _draft_fields(state.draft))
    return (
        "<h2>Extras</h2>"
        '<p class="quiet">Ces prestations se règlent à l\'hôtel, pas avec la chambre.</p>'
        f"{note}"
        '<form method="post" action="/sejour">'
        f"{hidden}{boxes}"
        f'<div class="actions">{_button("Continuer", "extras")}</div>'
        "</form>"
        '<form method="post" action="/sejour">'
        f"{''.join(_stay_fields(state.stay))}"
        f'<div class="actions">{_button("Retour", "back", primary=False)}</div>'
        "</form>"
    )


def _extra_box(state: ChoosingExtras, extra: Extra) -> str:
    checked = " checked" if extra.id in state.selected else ""
    amount = euros(extra.line_cents(state.stay))
    return (
        '<label class="check">'
        f'<input type="checkbox" name="extra" value="{_e(extra.id)}"{checked}>'
        f"<span>{_text(extra.label)} ({_text(amount)}) à régler à l'hôtel</span>"
        "</label>"
    )


def _contact_page(state: AskingContact) -> str:
    errors = dict(state.errors)
    room = f'<p class="quiet">{_text(state.room_name)}</p>' if state.room_name else ""
    when = f"{_when(state.stay.check_in)} au {_when(state.stay.checkout)}"
    extras = _chosen_line(state.catalog, state.selected, state.stay)
    extra_note = f'<p class="quiet">À régler à l\'hôtel. {_text(extras)}</p>' if extras else ""
    hidden = "".join(
        _stay_fields(state.stay) + _room_fields(state) + [_h("extra", item) for item in state.selected] + [_h("key", state.key)]
    )
    back_hidden = "".join(
        _stay_fields(state.stay)
        + _room_fields(state)
        + [_h("extra", item) for item in state.selected]
        + _draft_fields(state.draft)
        + [_h("key", state.key)]
    )
    arrival = _field(
        "Heure d'arrivée",
        "arrival_hour",
        "time",
        state.draft.arrival_hour,
        required=True,
        error=errors.get("arrival_hour", ""),
    )
    return (
        "<h2>Coordonnées</h2>"
        f'<p class="quiet">{_text(when)}</p>'
        f"{room}{extra_note}"
        '<form method="post" action="/sejour">'
        f"{hidden}"
        f"{_field('Nom', 'last_name', 'text', state.draft.last_name, required=True, error=errors.get('last_name', ''), complete='family-name')}"
        f"{_field('Prénom', 'first_name', 'text', state.draft.first_name, required=True, error=errors.get('first_name', ''), complete='given-name')}"
        f"{_field('E-mail', 'email', 'email', state.draft.email, required=True, error=errors.get('email', ''), complete='email')}"
        f"{_field('Téléphone', 'phone', 'tel', state.draft.phone, required=True, error=errors.get('phone', ''), complete='tel')}"
        f"{arrival}"
        f"{_area('Remarques', 'remarks', state.draft.remarks, errors.get('remarks', ''))}"
        f'<div class="actions">{_button("Confirmer la réservation", "contact")}</div>'
        "</form>"
        '<form method="post" action="/sejour">'
        f"{back_hidden}"
        f'<div class="actions">{_button("Retour", "back", primary=False)}</div>'
        "</form>"
    )


def _done(state: Done) -> str:
    total = euros(int(state.reservation.total_cents))
    extras = ""
    if state.extras:
        detail = ", ".join(
            f"{extra.label} ({euros(extra.line_cents(state.stay))})" for extra in state.extras
        )
        extras = f'<h3>À régler à l\'hôtel</h3><p>{_text(detail)}</p>'
    return (
        "<h2>Réservation confirmée</h2>"
        "<p>Votre séjour est enregistré.</p>"
        f'<p class="meta">Code</p><p class="code">{_text(state.reservation.code)}</p>'
        f'<p class="meta">Montant de la réservation</p><p class="price">{_text(total)}</p>'
        f"{extras}"
        f'<p class="quiet">{_text(_when(state.stay.check_in))} au {_text(_when(state.stay.checkout))}. {_text(state.room_name)}</p>'
        '<p><a href="/">Nouvelle recherche</a></p>'
    )


def _blocked_page(state: Blocked) -> str:
    wired = _error(state.wired_message) if state.wired_message else ""
    retry = ""
    if state.retry and state.key is not None and state.stay is not None and state.room_id is not None:
        hidden = "".join(
            _stay_fields(state.stay)
            + [
                _h("room_id", int(state.room_id)),
                _h("room_name", state.room_name),
                *[_h("extra", item) for item in state.selected],
                *_draft_fields(state.draft),
                _h("key", state.key),
                _h("outcome", state.kind.value),
            ]
        )
        retry = (
            '<form method="post" action="/sejour">'
            f"{hidden}"
            f'<div class="actions">{_button("Réessayer", "retry")}</div>'
            "</form>"
        )
    back = ""
    if state.allow_back and state.stay is not None and state.room_id is not None and state.key is not None:
        hidden = "".join(
            _stay_fields(state.stay)
            + [
                _h("room_id", int(state.room_id)),
                _h("room_name", state.room_name),
                *[_h("extra", item) for item in state.selected],
                *_draft_fields(state.draft),
                _h("key", state.key),
            ]
        )
        back = (
            '<form method="post" action="/sejour">'
            f"{hidden}"
            f'<div class="actions">{_button("Retour", "back", primary=False)}</div>'
            "</form>"
        )
    restart = "" if state.kind is BlockKind.IN_FLIGHT else '<p><a href="/">Nouvelle recherche</a></p>'
    return f"<h2>Confirmation</h2>{_error(state.french)}{wired}{retry}{back}{restart}"


def _field(
    label: str,
    name: str,
    kind: str,
    value: str,
    *,
    required: bool = False,
    error: str = "",
    min_value: str = "",
    complete: str = "",
) -> str:
    required_attr = " required" if required else ""
    min_attr = f' min="{_e(min_value)}"' if min_value != "" else ""
    complete_attr = f' autocomplete="{_e(complete)}"' if complete != "" else ""
    invalid = ' aria-invalid="true"' if error else ""
    message = f'<span class="error">{_text(error)}</span>' if error else ""
    return (
        f"<label><span class=\"label\">{_text(label)}</span>"
        f'<input type="{kind}" name="{_e(name)}" value="{_e(value)}"{required_attr}{min_attr}{complete_attr}{invalid}>'
        f"{message}</label>"
    )


def _area(label: str, name: str, value: str, error: str) -> str:
    invalid = ' aria-invalid="true"' if error else ""
    message = f'<span class="error">{_text(error)}</span>' if error else ""
    return (
        f'<label><span class="label">{_text(label)}</span>'
        f'<textarea name="{_e(name)}" maxlength="500"{invalid}>{_text(value)}</textarea>'
        f"{message}</label>"
    )


def _button(label: str, action: str, primary: bool = True) -> str:
    kind = "primary" if primary else "secondary"
    return f'<button class="{kind}" type="submit" name="action" value="{_e(action)}">{_text(label)}</button>'


def _stay_fields(stay: Stay) -> list[str]:
    return [
        _h("check_in", stay.check_in.isoformat()),
        _h("checkout", stay.checkout.isoformat()),
        _h("adults", stay.party.adults),
        _h("children", stay.party.children),
    ]


def _room_fields(state: ChoosingExtras | AskingContact) -> list[str]:
    return [_h("room_id", int(state.room_id)), _h("room_name", state.room_name)]


def _draft_fields(draft: ContactDraft) -> list[str]:
    return [
        _h("last_name", draft.last_name),
        _h("first_name", draft.first_name),
        _h("email", draft.email),
        _h("phone", draft.phone),
        _h("arrival_hour", draft.arrival_hour),
        _h("remarks", draft.remarks),
    ]


def _chosen_line(catalog: tuple[Extra, ...], selected: tuple[str, ...], stay: Stay) -> str:
    wanted = set(selected)
    parts = [
        f"{extra.label} ({euros(extra.line_cents(stay))})" for extra in catalog if extra.id in wanted
    ]
    return ", ".join(parts)


def _when(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def _h(name: str, value: object) -> str:
    return f'<input type="hidden" name="{_e(name)}" value="{_e(value)}">'


def _error(text: str) -> str:
    if text == "":
        return ""
    return f'<p class="error" role="alert">{_text(text)}</p>'


def _text(value: object) -> str:
    return html.escape(str(value), quote=False)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)
