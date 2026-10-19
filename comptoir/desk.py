from __future__ import annotations

import json
import os
import threading
import xmlrpc.client
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml
from dotenv import load_dotenv

from comptoir.quote import (
    Booking,
    Blocked,
    Catalog,
    Euros,
    Guest,
    IdempotencyKey,
    InclusiveNights,
    Offer,
    Origin,
    Refusal,
    ReservationSpan,
    Room,
    RoomId,
    SellBook,
    ShelfRow,
    SleptNight,
    Stay,
    StayFacts,
    price_ids,
    quote,
    series_for,
    total_cents,
)


class Flood(Exception):
    pass


class WiredFault(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _SellLoader(yaml.SafeLoader):
    pass


def _keep_word(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    if not isinstance(node, yaml.ScalarNode):
        raise yaml.constructor.ConstructorError(
            None, None, "sell.yml attend un mot", node.start_mark
        )
    return node.value


# PyYAML 1.1 tags the words yes and no as booleans, which would hide a typo.
_SellLoader.add_constructor("tag:yaml.org,2002:bool", _keep_word)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _euro(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def _flag(value: object) -> bool:
    # closed_departure arrives as the string "0". bool("0") would mark the room closed.
    if isinstance(value, str):
        return value.strip() not in {"", "0"}
    return bool(value)


def cents_from_wire(value: object) -> int | None:
    if value is None or value == "":
        return None
    amount = Decimal(str(value))
    if amount < Decimal("0.01"):
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _read_sell(path: Path) -> dict[RoomId, str]:
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_SellLoader)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("sell.yml illisible")
    choice: dict[RoomId, str] = {}
    for key, word in raw.items():
        if word not in ("yes", "no"):
            raise ValueError(f"mot interdit dans sell.yml: {word!r}")
        choice[RoomId(int(key))] = str(word)
    return choice


def _format_sell(choice: dict[RoomId, str]) -> str:
    return "".join(f"{int(room_id)}: {word}\n" for room_id, word in choice.items())


def _sell_book(choice: dict[RoomId, str]) -> SellBook:
    return SellBook({room_id: word == "yes" for room_id, word in choice.items()})


def _room_from_wire(raw: dict) -> Room:
    parent = int(raw.get("subroom") or 0)
    anchor = int(raw.get("anchorate") or 0)
    return Room(
        id=RoomId(int(raw["id"])),
        name=str(raw.get("name") or ""),
        occupancy=int(raw.get("occupancy") or 0),
        parent=None if parent == 0 else RoomId(parent),
        price_anchor=None if anchor == 0 else RoomId(anchor),
    )


def _fingerprint(room_id: RoomId, stay: Stay, guest: Guest, cents: int | None) -> str:
    return "|".join(
        (
            str(int(room_id)),
            stay.check_in.isoformat(),
            stay.check_out.isoformat(),
            str(stay.quantity),
            guest.lname,
            guest.email,
            "" if cents is None else str(cents),
        )
    )


def _booking_from(record: dict, replayed: bool) -> Booking:
    cents = int(record["cents"])
    return Booking(
        code=str(record["code"]),
        room_id=RoomId(int(record["room_id"])),
        name=str(record["name"]),
        nights=int(record["nights"]),
        euros=Euros(Decimal(cents) / Decimal(100)),
        replayed=replayed,
    )


@dataclass(frozen=True)
class ReservationOrder:
    stay: Stay
    room_id: RoomId
    quantity: int
    cents: int
    guest: Guest
    origin: Origin


class _Wired:
    url = "https://wired.wubook.net/xrws/"
    parity_pid = 0
    board = "nb"
    status_confirmed = 1
    ccard = 0
    ignore_restrs = 0
    ignore_avail = 0

    def __init__(self) -> None:
        self._rpc = None
        self._token = ""
        self._lcode: object = 0
        self.owns_token = False
        self._rpc_at: list[datetime] = []
        self._values_at: list[datetime] = []
        self._rooms_at: list[datetime] = []

    @classmethod
    def open(cls, rpc: object | None) -> _Wired:
        wired = cls()
        if rpc is not None:
            wired._rpc = rpc
            wired._token = "test"
            wired._lcode = 0
            wired.owns_token = False
            return wired
        load_dotenv()
        wired._rpc = xmlrpc.client.ServerProxy(cls.url, allow_none=True)
        lcode = os.environ["WUBOOK_LCODE"]
        wired._lcode = int(lcode) if str(lcode).isdigit() else lcode
        token = os.environ.get("WUBOOK_TOKEN") or ""
        if token:
            wired._token = token
            wired.owns_token = False
            return wired
        code, payload = wired._rpc.acquire_token(
            os.environ["WUBOOK_USER"],
            os.environ["WUBOOK_PASSWORD"],
            os.environ["WUBOOK_PKEY"],
        )
        if code != 0:
            message = payload if isinstance(payload, str) else str(payload)
            raise WiredFault(int(code), message)
        wired._token = str(payload)
        wired.owns_token = True
        return wired

    def close(self) -> None:
        if not self.owns_token:
            return
        try:
            self._rpc.release_token(self._token)
        finally:
            self.owns_token = False

    def _charge(self, method: str) -> None:
        now = _now()
        self._rpc_at = [stamp for stamp in self._rpc_at if now - stamp < timedelta(seconds=3600)]
        self._values_at = [stamp for stamp in self._values_at if now - stamp < timedelta(seconds=43200)]
        self._rooms_at = [stamp for stamp in self._rooms_at if now - stamp < timedelta(seconds=86400)]
        if len(self._rpc_at) >= 600:
            raise Flood()
        if method == "fetch_rooms_values" and len(self._values_at) >= 240:
            raise Flood()
        if method == "fetch_rooms" and len(self._rooms_at) >= 240:
            raise Flood()
        self._rpc_at.append(now)
        if method == "fetch_rooms_values":
            self._values_at.append(now)
        elif method == "fetch_rooms":
            self._rooms_at.append(now)

    def _call(self, method: str, *args: object) -> object:
        self._charge(method)
        result = getattr(self._rpc, method)(self._token, self._lcode, *args)
        if not isinstance(result, (list, tuple)) or len(result) < 2:
            raise WiredFault(-1, "réponse WuBook illisible")
        code, payload = result[0], result[1]
        if code == 0:
            return payload
        message = payload if isinstance(payload, str) else str(payload)
        raise WiredFault(int(code) if isinstance(code, int) else -1, message)

    def fetch_rooms(self) -> list:
        payload = self._call("fetch_rooms")
        if not isinstance(payload, list):
            raise WiredFault(-1, "réponse WuBook illisible")
        return payload

    def fetch_rooms_values(self, span: InclusiveNights) -> dict:
        if not isinstance(span, InclusiveNights):
            raise TypeError("fetch_rooms_values attend InclusiveNights")
        payload = self._call("fetch_rooms_values", _euro(span.first), _euro(span.last))
        if not isinstance(payload, dict):
            raise WiredFault(-1, "réponse WuBook illisible")
        return payload

    def fetch_plan_prices(self, span: InclusiveNights, rooms: tuple[RoomId, ...]) -> dict:
        if not isinstance(span, InclusiveNights):
            raise TypeError("fetch_plan_prices attend InclusiveNights")
        payload = self._call(
            "fetch_plan_prices",
            self.parity_pid,
            _euro(span.first),
            _euro(span.last),
            [int(room_id) for room_id in rooms],
        )
        if not isinstance(payload, dict):
            raise WiredFault(-1, "réponse WuBook illisible")
        return payload

    def new_reservation(self, span: ReservationSpan, order: ReservationOrder, key: IdempotencyKey) -> str:
        if not isinstance(span, ReservationSpan):
            raise TypeError("new_reservation attend ReservationSpan")
        customer = {
            field: value
            for field, value in (
                ("lname", order.guest.lname),
                ("fname", order.guest.fname),
                ("email", order.guest.email),
                ("city", order.guest.city),
                ("phone", order.guest.phone),
                ("street", order.guest.street),
                ("country", order.guest.country),
                ("arrival_hour", order.guest.arrival_hour),
                ("notes", order.guest.notes),
            )
            if value
        }
        payload = self._call(
            "new_reservation",
            _euro(span.dfrom),
            _euro(span.dto),
            {str(int(order.room_id)): [order.quantity, self.board]},
            customer,
            float(Decimal(order.cents) / Decimal(100)),
            order.origin.value,
            self.ccard,
            {"idempotency_key": str(key)},
            {"men": order.stay.adults, "children": order.stay.children},
            self.ignore_restrs,
            self.ignore_avail,
            self.status_confirmed,
        )
        return str(payload)

    def find_existing(self, span: ReservationSpan, key: IdempotencyKey) -> str | None:
        if not isinstance(span, ReservationSpan):
            raise TypeError("fetch_bookings attend ReservationSpan")
        try:
            payload = self._call(
                "fetch_bookings",
                _euro(span.dfrom),
                _euro(span.dto),
                0,
                1,
            )
        except WiredFault:
            return None
        if not isinstance(payload, list):
            return None
        wanted = str(key)
        for booking in payload:
            if not isinstance(booking, dict):
                continue
            ancillary = booking.get("ancillary")
            if not isinstance(ancillary, dict):
                continue
            if str(ancillary.get("idempotency_key", "")) != wanted:
                continue
            code = booking.get("rcode")
            if code is None or code == "":
                continue
            return str(code)
        return None


class Desk:
    def __init__(
        self,
        sell_path: Path | None = None,
        receipt_path: Path | None = None,
        rpc: object | None = None,
    ) -> None:
        self._sell_path = sell_path or Path(__file__).with_name("sell.yml")
        self._receipt_path = receipt_path or Path(__file__).with_name("receipts.json")
        self._lock = threading.Lock()
        self._facts: dict[tuple[date, date, tuple[RoomId, ...]], tuple[datetime, StayFacts]] = {}
        self._catalog_cache: Catalog | None = None
        self._catalog_at: datetime | None = None
        _read_sell(self._sell_path)
        self._wired: _Wired | None = None
        try:
            self._wired = _Wired.open(rpc)
            self._catalog()
        except Exception:
            if self._wired is not None:
                self._wired.close()
            raise

    def close(self) -> None:
        if self._wired is None:
            return
        with self._lock:
            self._wired.close()

    def rooms(self) -> tuple[ShelfRow, ...]:
        with self._lock:
            catalog = self._catalog()
            sell = self._sell()
            rows: list[ShelfRow] = []
            for room in catalog.rooms.values():
                parent_name = ""
                if room.parent is not None:
                    parent = catalog.rooms.get(room.parent)
                    parent_name = "" if parent is None else parent.name
                rows.append(ShelfRow(room.id, room.name, room.parent, parent_name, sell.sells(room.id)))
            return tuple(rows)

    def set_sell(self, room_id: RoomId, sell: bool) -> None:
        with self._lock:
            choice = _read_sell(self._sell_path)
            choice[room_id] = "yes" if sell else "no"
            _atomic(self._sell_path, _format_sell(choice))

    def search(self, stay: Stay) -> Blocked | tuple[Offer | Refusal, ...]:
        with self._lock:
            try:
                catalog = self._catalog()
                sell = self._sell()
                selling = tuple(room.id for room in catalog.rooms.values() if sell.sells(room.id))
                if not selling:
                    return ()
                facts = self._search_facts(stay, catalog, selling)
            except Flood:
                return Blocked("WuBook limité, réessayez plus tard.")
            return quote(stay, catalog, facts, sell, only=None)

    def book(
        self,
        stay: Stay,
        room_id: RoomId,
        guest: Guest,
        key: IdempotencyKey,
        origin: Origin,
    ) -> Booking | Refusal | Blocked:
        if not str(key).strip():
            return Blocked("clé manquante")
        with self._lock:
            try:
                return self._book(stay, room_id, guest, key, origin)
            except Flood:
                return Blocked("WuBook limité, réessayez plus tard.")

    def _sell(self) -> SellBook:
        return _sell_book(_read_sell(self._sell_path))

    def _catalog(self) -> Catalog:
        now = _now()
        if (
            self._catalog_cache is not None
            and self._catalog_at is not None
            and now - self._catalog_at < timedelta(hours=6)
        ):
            return self._catalog_cache
        assert self._wired is not None
        rooms = {parsed.id: parsed for parsed in (_room_from_wire(raw) for raw in self._wired.fetch_rooms())}
        self._catalog_cache = Catalog(rooms)
        self._catalog_at = now
        return self._catalog_cache

    def _search_facts(self, stay: Stay, catalog: Catalog, selling: tuple[RoomId, ...]) -> StayFacts:
        key = (stay.check_in, stay.check_out, selling)
        cached = self._facts.get(key)
        now = _now()
        if cached is not None and now - cached[0] < timedelta(minutes=15):
            return cached[1]
        facts = self._load(stay, catalog, selling)
        self._facts[key] = (now, facts)
        return facts

    def _load(self, stay: Stay, catalog: Catalog, selling: tuple[RoomId, ...]) -> StayFacts:
        nights, departure = self._grid(stay, catalog)
        return StayFacts(nights, self._prices(stay, catalog, selling), departure)

    def _grid(
        self,
        stay: Stay,
        catalog: Catalog,
    ) -> tuple[dict[RoomId, tuple[SleptNight, ...]], dict[RoomId, bool]]:
        assert self._wired is not None
        raw = self._wired.fetch_rooms_values(stay.values_span())
        by_id = {RoomId(int(key)): list(days) for key, days in raw.items()}
        slept = stay.slept()
        nights: dict[RoomId, tuple[SleptNight, ...]] = {}
        departure: dict[RoomId, bool] = {}
        for room_id in catalog.rooms:
            days = by_id.get(room_id, [])
            built: list[SleptNight] = []
            for index, day in enumerate(slept):
                cell = days[index] if index < len(days) else {}
                built.append(
                    SleptNight(
                        date=day,
                        avail=int(cell.get("avail") or 0),
                        closed=_flag(cell.get("closed")),
                        closed_arrival=_flag(cell.get("closed_arrival")),
                        min_stay=int(cell.get("min_stay") or 0),
                        max_stay=int(cell.get("max_stay") or 0),
                        min_stay_arrival=int(cell.get("min_stay_arrival") or 0),
                    )
                )
            extra = days[len(slept)] if len(days) > len(slept) else {}
            departure[room_id] = _flag(extra.get("closed_departure"))
            nights[room_id] = tuple(built)
        for room in catalog.rooms.values():
            if room.parent is None:
                continue
            parent_nights = nights.get(room.parent)
            if not parent_nights:
                continue
            nights[room.id] = tuple(
                replace(night, avail=parent.avail)
                for night, parent in zip(nights[room.id], parent_nights, strict=True)
            )
        return nights, departure

    def _prices(
        self,
        stay: Stay,
        catalog: Catalog,
        selling: tuple[RoomId, ...],
    ) -> dict[RoomId, tuple[int | None, ...]]:
        assert self._wired is not None
        raw = self._wired.fetch_plan_prices(stay.price_span(), price_ids(catalog, selling))
        return {
            RoomId(int(key)): tuple(cents_from_wire(item) for item in series)
            for key, series in raw.items()
        }

    def _book(
        self,
        stay: Stay,
        room_id: RoomId,
        guest: Guest,
        key: IdempotencyKey,
        origin: Origin,
    ) -> Booking | Refusal | Blocked:
        assert self._wired is not None
        catalog = self._catalog()
        room = catalog.rooms.get(room_id)
        if room is None:
            return Blocked("chambre inconnue")
        sell = self._sell()
        facts = self._load(stay, catalog, (room_id,))
        verdict = quote(stay, catalog, facts, sell, only=room_id)[0]
        series = series_for(room, facts)
        cents = None if series is None else total_cents(series, stay.quantity)
        fingerprint = _fingerprint(room_id, stay, guest, cents)
        receipts = self._read_receipts()
        current = receipts.get(str(key))
        if current is not None and current["fingerprint"] != fingerprint:
            return Blocked("clé déjà utilisée pour une autre demande.")
        if current is not None and current["state"] == "booked":
            return _booking_from(current, replayed=True)
        if current is not None and current["state"] == "sent":
            found = self._wired.find_existing(stay.reservation_span(), key)
            if found is None:
                return Blocked("envoi incertain, vérifiez WuBook.")
            current["state"] = "booked"
            current["code"] = found
            receipts[str(key)] = current
            self._write_receipts(receipts)
            return _booking_from(current, replayed=False)
        if isinstance(verdict, Refusal):
            return verdict
        if cents is None:
            return verdict
        record = {
            "fingerprint": fingerprint,
            "state": "open",
            "code": None,
            "room_id": int(room_id),
            "name": room.name,
            "nights": stay.nights,
            "cents": cents,
        }
        receipts[str(key)] = record
        self._write_receipts(receipts)
        record["state"] = "sent"
        receipts[str(key)] = record
        self._write_receipts(receipts)
        order = ReservationOrder(stay, room_id, stay.quantity, cents, guest, origin)
        try:
            code = self._wired.new_reservation(stay.reservation_span(), order, key)
        except WiredFault as err:
            record["state"] = "rejected"
            receipts[str(key)] = record
            self._write_receipts(receipts)
            return Blocked(err.message)
        record["state"] = "booked"
        record["code"] = code
        receipts[str(key)] = record
        self._write_receipts(receipts)
        return _booking_from(record, replayed=False)

    def _read_receipts(self) -> dict:
        if not self._receipt_path.exists():
            return {}
        data = json.loads(self._receipt_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("receipts.json illisible")
        return data

    def _write_receipts(self, data: dict) -> None:
        _atomic(self._receipt_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
