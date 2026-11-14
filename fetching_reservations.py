from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from enum import IntEnum
from math import isfinite
from typing import Literal, NoReturn, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from wubook_api import Wubook

_CLOSED = ConfigDict(extra="forbid")

FETCH_CAP = 120

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

DateAxis = Literal["created", "arrival"]
PayModel = Literal["merchant", "agency"]

_RESERVATION_KEYS = frozenset(
    {
        "reservation_code",
        "status",
        "status_reason",
        "was_modified",
        "modified_reservations",
        "amount",
        "currency",
        "orig_amount",
        "amount_reason",
        "date_received_time",
        "date_arrival",
        "date_departure",
        "arrival_hour",
        "men",
        "children",
        "rooms",
        "booked_rooms",
        "ancillary",
        "channel_reservation_code",
        "id_channel",
        "id_woodoo",
        "fount",
        "customer_name",
        "customer_surname",
        "customer_mail",
        "customer_phone",
        "customer_address",
        "customer_city",
        "customer_zip",
        "customer_country",
        "customer_notes",
        "customer_language",
        "customer_language_iso",
        "device",
        "deleted_at_time",
        "deleted_advance",
        "deleted_from",
        "channel_data",
    }
)

_CHANNEL_KEYS = frozenset(
    {
        "split_reservation",
        "paymodel",
        "vat_included",
        "addons_embedded",
        "vcc_additional_info",
    }
)

_TEXT_KEYS = (
    "status_reason",
    "amount_reason",
    "arrival_hour",
    "customer_name",
    "customer_surname",
    "customer_mail",
    "customer_phone",
    "customer_address",
    "customer_city",
    "customer_zip",
    "customer_country",
    "customer_notes",
    "customer_language_iso",
)

_REQUIRED = ("reservation_code", "status", "date_arrival", "date_departure", "amount")


class ReservationStatus(IntEnum):
    CONFIRMED = 1
    WAITING = 2
    REFUSED = 3
    ACCEPTED = 4
    CANCELLED = 5
    CANCELLED_WITH_PENALTY = 6


class Device(IntEnum):
    NOT_DETECTABLE = -1
    UNKNOWN = 0
    MOBILE = 1
    TABLET = 2
    COMPUTER = 3
    IPHONE = 4
    IPAD = 5
    ANDROID = 6
    BLACKBERRY = 7
    IPOD = 8


class DeletedFrom(IntEnum):
    CUSTOMER = 1
    BACKOFFICE = 2
    CHANNEL = 3
    WUBOOK_STAFF = 4
    ALIEN = 5
    WIRED = 6
    PAYMENT_GATEWAY = 7
    AFTER_MODIFICATION = 8


class Modification(BaseModel):
    model_config = _CLOSED

    replaces: int | None = None
    superseded: bool = False

    @field_validator("replaces", mode="before")
    @classmethod
    def earlier(cls, value: object) -> int | None:
        if value is None:
            return None
        return _code(value)


class BookedDay(BaseModel):
    model_config = _CLOSED

    day: date
    price: float
    rate_id: int
    ancillary: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("day", mode="before")
    @classmethod
    def stay_day(cls, value: object) -> date:
        return _european_date(value)

    @field_validator("price", mode="before")
    @classmethod
    def stay_price(cls, value: object) -> float:
        return _finite(value, "price")

    @field_validator("rate_id", mode="before")
    @classmethod
    def stay_rate(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rate_id")
        return value

    @field_validator("ancillary", mode="before")
    @classmethod
    def stay_ancillary(cls, value: object) -> dict[str, JsonValue]:
        return _bag(value)


class BookedRoom(BaseModel):
    """One booked unit. The same room_id can appear twice."""

    model_config = _CLOSED

    room_id: int
    days: tuple[BookedDay, ...] = ()
    ancillary: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("room_id", mode="before")
    @classmethod
    def room(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("room_id")
        return value

    @field_validator("ancillary", mode="before")
    @classmethod
    def room_ancillary(cls, value: object) -> dict[str, JsonValue]:
        return _bag(value)

    @property
    def prices_generated(self) -> bool:
        return self.ancillary.get("auto_generated") is True


class Deletion(BaseModel):
    model_config = _CLOSED

    at: datetime
    advance: int
    source: DeletedFrom

    @field_validator("at", mode="before")
    @classmethod
    def moment(cls, value: object) -> datetime:
        return _naive_clock(value)

    @field_validator("advance", mode="before")
    @classmethod
    def days_before_arrival(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("advance")
        return value

    @field_validator("source", mode="before")
    @classmethod
    def origin(cls, value: object) -> DeletedFrom:
        return _enum_member(value, DeletedFrom)


class SplitReservation(BaseModel):
    model_config = _CLOSED

    part: int
    count: int

    @field_validator("part", "count", mode="before")
    @classmethod
    def stream(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("split")
        return value

    @model_validator(mode="after")
    def streams(self) -> SplitReservation:
        if self.count < self.part:
            raise ValueError("split")
        return self


class VirtualCard(BaseModel):
    model_config = _CLOSED

    valid_from: date | None = None
    valid_to: date | None = None
    balance: float | None = None
    currency: str | None = None

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def card_day(cls, value: object) -> date | None:
        if value is None or value == "":
            return None
        return _european_date(value)

    @field_validator("balance", mode="before")
    @classmethod
    def card_balance(cls, value: object) -> float | None:
        if value is None or value == "":
            return None
        return _finite(value, "amount")

    @field_validator("currency", mode="before")
    @classmethod
    def card_currency(cls, value: object) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, str) and _letters(value):
            return value
        raise ValueError("currency")


class ChannelData(BaseModel):
    model_config = _CLOSED

    split: SplitReservation | None = None
    paymodel: PayModel | None = None
    vat_included: bool | None = None
    addons_embedded: bool | None = None
    virtual_card: VirtualCard | None = None
    rest: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("rest", mode="before")
    @classmethod
    def extra(cls, value: object) -> dict[str, JsonValue]:
        return _bag(value)


class Reservation(BaseModel):
    model_config = _CLOSED

    code: int
    status: ReservationStatus
    status_reason: str = ""
    modification: Modification
    amount: float
    currency: str | None = None
    orig_amount: float | None = None
    amount_reason: str = ""
    received_at: datetime | None = None
    arrival: date
    departure: date
    arrival_hour: str = ""
    adults: int | None = None
    children: int = 0
    room_ids: tuple[int, ...] = ()
    booked_rooms: tuple[BookedRoom, ...] = ()
    ancillary: dict[str, JsonValue] = Field(default_factory=dict)
    channel_reservation_code: str | None = None
    id_channel: int = 0
    id_woodoo: int = 0
    fount: int | None = None
    customer_name: str = ""
    customer_surname: str = ""
    customer_mail: str = ""
    customer_phone: str = ""
    customer_address: str = ""
    customer_city: str = ""
    customer_zip: str = ""
    customer_country: str = ""
    customer_notes: str = ""
    customer_language: int | None = None
    customer_language_iso: str = ""
    device: Device | None = None
    deletion: Deletion | None = None
    channel_data: ChannelData | None = None
    rest: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("status", mode="before")
    @classmethod
    def status_code(cls, value: object) -> ReservationStatus:
        return _enum_member(value, ReservationStatus)

    @field_validator("device", mode="before")
    @classmethod
    def device_code(cls, value: object) -> Device | None:
        if value is None or value == "":
            return None
        return _enum_member(value, Device)

    @field_validator("code", mode="before")
    @classmethod
    def reservation_code(cls, value: object) -> int:
        return _code(value)

    @field_validator("amount", mode="before")
    @classmethod
    def stay_amount(cls, value: object) -> float:
        return _finite(value, "amount")

    @field_validator("orig_amount", mode="before")
    @classmethod
    def original_amount(cls, value: object) -> float | None:
        if value is None or value == "":
            return None
        return _finite(value, "amount")

    @field_validator("currency", mode="before")
    @classmethod
    def currency_code(cls, value: object) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, str) and _letters(value):
            return value
        raise ValueError("currency")

    @field_validator("received_at", mode="before")
    @classmethod
    def received(cls, value: object) -> datetime | None:
        if value is None or value == "":
            return None
        return _naive_clock(value)

    @field_validator("arrival", "departure", mode="before")
    @classmethod
    def stay(cls, value: object) -> date:
        return _european_date(value)

    @field_validator("adults", mode="before")
    @classmethod
    def adult_count(cls, value: object) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("men")
        if value == -1:
            return None
        if value < 0:
            raise ValueError("men")
        return value

    @field_validator("children", mode="before")
    @classmethod
    def child_count(cls, value: object) -> int:
        if value is None or value == "":
            return 0
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("children")
        return value

    @field_validator("room_ids", mode="before")
    @classmethod
    def room_list(cls, value: object) -> tuple[int, ...]:
        return _room_ids(value)

    @field_validator("channel_reservation_code", mode="before")
    @classmethod
    def channel_code(cls, value: object) -> str | None:
        if isinstance(value, bool):
            raise ValueError("channel_reservation_code")
        if value is None or value == "" or value == 0 or value == "0":
            return None
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str):
            return value
        raise ValueError("channel_reservation_code")

    @field_validator("id_channel", "id_woodoo", mode="before")
    @classmethod
    def channel_id(cls, value: object) -> int:
        if value is None or value == "":
            return 0
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("id")
        return value

    @field_validator("fount", mode="before")
    @classmethod
    def fount_id(cls, value: object) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("fount")
        if value == 0:
            return None
        return value

    @field_validator("customer_language", mode="before")
    @classmethod
    def language_id(cls, value: object) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("customer_language")
        return value

    @field_validator(*_TEXT_KEYS, mode="before")
    @classmethod
    def guest_text(cls, value: object) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("text")
        return value

    @field_validator("ancillary", "rest", mode="before")
    @classmethod
    def bags(cls, value: object) -> dict[str, JsonValue]:
        return _bag(value)

    @model_validator(mode="after")
    def chain(self) -> Reservation:
        if self.modification.superseded and self.status != ReservationStatus.CANCELLED:
            raise ValueError("superseded")
        return self

    @property
    def created_at(self) -> datetime:
        return datetime.fromtimestamp(self.code, timezone.utc)


class ReservationBatch(BaseModel):
    model_config = _CLOSED

    reservations: tuple[Reservation, ...] = ()

    @model_validator(mode="after")
    def unique_codes(self) -> ReservationBatch:
        seen: set[int] = set()
        for item in self.reservations:
            if item.code in seen:
                raise ValueError("duplicate code")
            seen.add(item.code)
        return self

    @property
    def overflow(self) -> bool:
        return len(self.reservations) >= FETCH_CAP


class UnmarkedBatch(ReservationBatch):
    """Rows from fetch_new_bookings. overflow means mark those codes and call again."""


class DatedBatch(ReservationBatch):
    """Rows from a dated fetch. overflow means narrow the dates."""


class CodeBatch(BaseModel):
    """Codes from fetch_bookings_codes. overflow means narrow the dates."""

    model_config = _CLOSED

    codes: tuple[int, ...] = ()

    @model_validator(mode="after")
    def unique_codes(self) -> CodeBatch:
        seen: set[int] = set()
        for code in self.codes:
            if code in seen:
                raise ValueError("duplicate code")
            seen.add(code)
        return self

    @property
    def overflow(self) -> bool:
        return len(self.codes) >= FETCH_CAP


_BatchT = TypeVar("_BatchT", UnmarkedBatch, DatedBatch)


class FetchingReservations(Wubook):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def push_activation(self, url: str, *, test: bool = False) -> None:
        if not isinstance(url, str):
            raise ValueError("url")
        if type(test) is not bool:
            raise ValueError("test")
        if test and url == "":
            raise ValueError("url")
        self._call("push_activation", (url, int(test)))

    def push_url(self) -> str:
        payload = self._call("push_url", ())
        if not isinstance(payload, str):
            raise ConnectionError(f"push_url: expected a string, got {payload!r}")
        return payload

    def fetch_booking(self, code: int) -> Reservation:
        reservation_code = _code(code)
        payload = self._call("fetch_booking", (reservation_code, 1))
        if not isinstance(payload, dict):
            _fail("fetch_booking", f"expected a reservation, got {payload!r}")
        return _reservation("fetch_booking", payload)

    def fetch_new_bookings(self) -> UnmarkedBatch:
        payload = self._call("fetch_new_bookings", (1, 0))
        return _reservations("fetch_new_bookings", payload, UnmarkedBatch)

    def mark_bookings(self, codes: Sequence[int]) -> None:
        if isinstance(codes, (str, bytes)) or not isinstance(codes, Sequence) or len(codes) == 0:
            raise ValueError("codes")
        parsed: list[int] = []
        seen: set[int] = set()
        for item in codes:
            try:
                code = _code(item)
            except ValueError:
                raise ValueError("codes") from None
            if code in seen:
                raise ValueError("duplicate code")
            seen.add(code)
            parsed.append(code)
        self._call("mark_bookings", (parsed,))

    def mark_collected(self) -> None:
        # The empty array marks every reservation this property has collected.
        self._call("mark_bookings", ([],))

    def fetch_bookings(
        self,
        dfrom: date,
        dto: date,
        *,
        axis: DateAxis = "created",
    ) -> DatedBatch:
        start, end = _span(dfrom, dto)
        payload = self._call("fetch_bookings", (start, end, _axis(axis), 1))
        return _reservations("fetch_bookings", payload, DatedBatch)

    def fetch_deleted_bookings(self, dfrom: date, dto: date) -> DatedBatch:
        start, end = _span(dfrom, dto)
        payload = self._call("fetch_deleted_bookings", (start, end, 1))
        return _reservations("fetch_deleted_bookings", payload, DatedBatch)

    def fetch_bookings_codes(
        self,
        dfrom: date,
        dto: date,
        *,
        axis: DateAxis = "created",
    ) -> CodeBatch:
        start, end = _span(dfrom, dto)
        payload = self._call("fetch_bookings_codes", (start, end, _axis(axis)))
        return _code_batch("fetch_bookings_codes", payload)

    def get_fount_symbols(self) -> dict[int, str]:
        # Wired's get_fount_symbols takes the token and no property code.
        return _fount_symbols(self._call_token("get_fount_symbols", ()))

    def _call(self, method: str, args: tuple[object, ...]) -> object:
        if any(arg is None for arg in args):
            raise ValueError("none")
        remote = getattr(self.server, method)
        return self._payload(method, remote(self.token, self.lcode, *args))

    def _call_token(self, method: str, args: tuple[object, ...]) -> object:
        if any(arg is None for arg in args):
            raise ValueError("none")
        remote = getattr(self.server, method)
        return self._payload(method, remote(self.token, *args))

    def _payload(self, method: str, result: object) -> object:
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise ConnectionError(f"{method}: unexpected response {result!r}")
        code, data = result
        if code != 0:
            raise ConnectionError(f"{method} failed ({code}): {data}")
        return data


def _fail(method: str, detail: str, exc: BaseException | None = None) -> NoReturn:
    if exc is None:
        raise ConnectionError(f"{method}: {detail}")
    raise ConnectionError(f"{method}: {detail}") from exc


def _letters(value: str) -> bool:
    return len(value) == 3 and value.isascii() and value.isalpha()


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name)
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(name) from None
    if not isfinite(number):
        raise ValueError(name)
    return number


def _code(value: object) -> int:
    if isinstance(value, str):
        if not value.isascii() or not value.isdigit():
            raise ValueError("code")
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("code")
    return value


def _enum_member[T: IntEnum](value: object, kind: type[T]) -> T:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("enum")
    try:
        return kind(value)
    except ValueError:
        raise ValueError("enum") from None


def _plain_date(value: object) -> date:
    if type(value) is not date:
        raise ValueError("date")
    return value


def _wire_date(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def _european_date(value: object) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError("date")
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        raise ValueError("date") from None


def _clock(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("clock")
    for pattern in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    raise ValueError("clock")


def _naive_clock(value: object) -> datetime:
    if type(value) is datetime:
        if value.tzinfo is not None:
            raise ValueError("clock")
        return value
    return _clock(value)


def _span(dfrom: object, dto: object) -> tuple[str, str]:
    start = _plain_date(dfrom)
    end = _plain_date(dto)
    if start > end:
        raise ValueError("dfrom is after dto")
    return _wire_date(start), _wire_date(end)


def _axis(axis: object) -> int:
    if axis == "created":
        return 1
    if axis == "arrival":
        return 0
    raise ValueError("axis")


def _json(value: object) -> JsonValue:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("json")
        return value
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        parsed: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("json")
            parsed[key] = _json(item)
        return parsed
    raise ValueError("json")


def _bag(value: object) -> dict[str, JsonValue]:
    if value is None or value == "":
        return {}
    parsed = _json(value)
    if not isinstance(parsed, dict):
        raise ValueError("json")
    return parsed


def _room_ids(value: object) -> tuple[int, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        ids: list[int] = []
        for piece in value.split(","):
            token = piece.strip()
            if not token.isascii() or not token.isdigit() or int(token) <= 0:
                raise ValueError("rooms")
            ids.append(int(token))
        return tuple(ids)
    if isinstance(value, Sequence):
        ids = []
        for item in value:
            if type(item) is not int or item <= 0:
                raise ValueError("rooms")
            ids.append(item)
        return tuple(ids)
    raise ValueError("rooms")


def _modified_flag(method: str, payload: dict[str, object]) -> bool:
    if "was_modified" not in payload:
        return False
    value = payload["was_modified"]
    if value is False or (type(value) is int and value == 0):
        return False
    if value is True or (type(value) is int and value == 1):
        return True
    _fail(method, "was_modified")


def _modified_link(method: str, payload: dict[str, object]) -> int | None:
    if "modified_reservations" not in payload or payload["modified_reservations"] is None:
        return None
    raw = payload["modified_reservations"]
    if not isinstance(raw, list):
        _fail(method, "modified_reservations")
    if len(raw) == 0:
        return None
    if len(raw) > 1:
        _fail(method, "modified_reservations")
    try:
        return _code(raw[0])
    except ValueError as exc:
        _fail(method, "modified_reservations", exc)


def _modification(method: str, code: int, payload: dict[str, object]) -> Modification:
    superseded = _modified_flag(method, payload)
    link = _modified_link(method, payload)
    if link is None:
        if superseded:
            _fail(method, "modified_reservations")
        return Modification()
    if link == code:
        return Modification(replaces=code, superseded=True)
    return Modification(replaces=link, superseded=superseded)


def _booked_rooms(method: str, value: object) -> tuple[BookedRoom, ...]:
    if value is None or value == []:
        return ()
    if not isinstance(value, list):
        _fail(method, "booked_rooms")
    rooms: list[BookedRoom] = []
    for item in value:
        if not isinstance(item, dict) or "room_id" not in item or "roomdays" not in item:
            _fail(method, "booked_rooms")
        raw_days = item["roomdays"]
        if not isinstance(raw_days, list):
            _fail(method, "booked_rooms")
        days: list[dict[str, object]] = []
        for raw in raw_days:
            if not isinstance(raw, dict) or any(key not in raw for key in ("day", "price", "rate_id")):
                _fail(method, "booked_rooms")
            day: dict[str, object] = {
                "day": raw["day"],
                "price": raw["price"],
                "rate_id": raw["rate_id"],
            }
            if "ancillary" in raw:
                day["ancillary"] = raw["ancillary"]
            days.append(day)
        room: dict[str, object] = {"room_id": item["room_id"], "days": days}
        if "ancillary" in item:
            room["ancillary"] = item["ancillary"]
        try:
            rooms.append(BookedRoom.model_validate(room))
        except (ValidationError, ValueError) as exc:
            _fail(method, "booked_rooms", exc)
    return tuple(rooms)


def _deletion(method: str, payload: dict[str, object]) -> Deletion | None:
    keys = ("deleted_at_time", "deleted_advance", "deleted_from")
    present = [key for key in keys if key in payload and payload[key] not in (None, "")]
    if not present:
        return None
    if len(present) != 3:
        _fail(method, "deletion")
    try:
        return Deletion.model_validate(
            {
                "at": payload["deleted_at_time"],
                "advance": payload["deleted_advance"],
                "source": payload["deleted_from"],
            }
        )
    except (ValidationError, ValueError) as exc:
        _fail(method, "deletion", exc)


def _flag(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise ValueError("flag")


def _paymodel(value: object) -> PayModel | None:
    if value is None or value == "":
        return None
    if value == "merchant":
        return "merchant"
    if value == "agency":
        return "agency"
    raise ValueError("paymodel")


def _split(value: object) -> SplitReservation | None:
    if value is None or value == "" or value == {}:
        return None
    if not isinstance(value, dict) or "n_stream" not in value or "tot_stream" not in value:
        raise ValueError("split")
    return SplitReservation.model_validate({"part": value["n_stream"], "count": value["tot_stream"]})


def _card(value: object) -> VirtualCard | None:
    if value is None or value == "" or value == {}:
        return None
    if not isinstance(value, dict):
        raise ValueError("card")
    card = VirtualCard.model_validate(
        {
            "valid_from": value.get("vcc_valid_from"),
            "valid_to": value.get("vcc_valid_til"),
            "balance": value.get("vcc_balance"),
            "currency": value.get("vcc_currency_code"),
        }
    )
    if (
        card.valid_from is None
        and card.valid_to is None
        and card.balance is None
        and card.currency is None
    ):
        return None
    return card


def _channel_data(method: str, value: object) -> ChannelData | None:
    if value is None or value == "" or value == {}:
        return None
    if not isinstance(value, dict):
        _fail(method, "channel_data")
    try:
        split = _split(value["split_reservation"]) if "split_reservation" in value else None
        paymodel = _paymodel(value["paymodel"]) if "paymodel" in value else None
        vat = _flag(value["vat_included"]) if "vat_included" in value else None
        addons = _flag(value["addons_embedded"]) if "addons_embedded" in value else None
        card = _card(value["vcc_additional_info"]) if "vcc_additional_info" in value else None
        rest: dict[str, JsonValue] = {}
        for key, item in value.items():
            if key in _CHANNEL_KEYS:
                continue
            if not isinstance(key, str):
                raise ValueError("json")
            rest[key] = _json(item)
    except (ValidationError, ValueError) as exc:
        _fail(method, "channel_data", exc)
    if (
        split is None
        and paymodel is None
        and vat is None
        and addons is None
        and card is None
        and not rest
    ):
        return None
    return ChannelData(
        split=split,
        paymodel=paymodel,
        vat_included=vat,
        addons_embedded=addons,
        virtual_card=card,
        rest=rest,
    )


def _rest(payload: dict[str, object]) -> dict[str, JsonValue]:
    rest: dict[str, JsonValue] = {}
    for key, value in payload.items():
        if key in _RESERVATION_KEYS:
            continue
        if not isinstance(key, str):
            raise ValueError("json")
        rest[key] = _json(value)
    return rest


def _fields(method: str, payload: dict[str, object]) -> dict[str, object]:
    for key in _REQUIRED:
        if key not in payload or payload[key] is None:
            _fail(method, f"missing {key}")
    try:
        code = _code(payload["reservation_code"])
    except ValueError as exc:
        _fail(method, "reservation_code", exc)
    try:
        _enum_member(payload["status"], ReservationStatus)
    except ValueError as exc:
        _fail(method, "status", exc)
    fields: dict[str, object] = {
        "code": code,
        "status": payload["status"],
        "modification": _modification(method, code, payload),
        "amount": payload["amount"],
        "arrival": payload["date_arrival"],
        "departure": payload["date_departure"],
        "rest": _rest(payload),
    }
    if "orig_amount" in payload:
        fields["orig_amount"] = payload["orig_amount"]
    if "currency" in payload:
        fields["currency"] = payload["currency"]
    if "date_received_time" in payload:
        fields["received_at"] = payload["date_received_time"]
    if "men" in payload:
        fields["adults"] = payload["men"]
    if "children" in payload:
        fields["children"] = payload["children"]
    if "rooms" in payload:
        fields["room_ids"] = payload["rooms"]
    if "booked_rooms" in payload:
        fields["booked_rooms"] = _booked_rooms(method, payload["booked_rooms"])
    if "ancillary" in payload:
        fields["ancillary"] = payload["ancillary"]
    if "channel_reservation_code" in payload:
        fields["channel_reservation_code"] = payload["channel_reservation_code"]
    for key in ("id_channel", "id_woodoo", "fount", "customer_language", "device"):
        if key in payload:
            fields[key] = payload[key]
    for key in _TEXT_KEYS:
        if key in payload:
            fields[key] = payload[key]
    if "channel_data" in payload:
        fields["channel_data"] = _channel_data(method, payload["channel_data"])
    fields["deletion"] = _deletion(method, payload)
    return fields


def _reservation(method: str, payload: object) -> Reservation:
    if not isinstance(payload, dict):
        _fail(method, f"expected a reservation, got {payload!r}")
    try:
        return Reservation.model_validate(_fields(method, payload))
    except (ValidationError, ValueError) as exc:
        _fail(method, "reservation", exc)


def _reservations(method: str, payload: object, batch: type[_BatchT]) -> _BatchT:
    if not isinstance(payload, list):
        _fail(method, f"expected a list, got {payload!r}")
    rows = tuple(_reservation(method, item) for item in payload)
    try:
        return batch(reservations=rows)
    except ValidationError as exc:
        _fail(method, "duplicate code", exc)


def _code_batch(method: str, payload: object) -> CodeBatch:
    if not isinstance(payload, list):
        _fail(method, f"expected a list, got {payload!r}")
    parsed: list[int] = []
    for item in payload:
        try:
            parsed.append(_code(item))
        except ValueError as exc:
            _fail(method, "code", exc)
    try:
        return CodeBatch(codes=tuple(parsed))
    except ValidationError as exc:
        _fail(method, "duplicate code", exc)


def _fount_id(value: object) -> int:
    try:
        return _code(value)
    except ValueError as exc:
        _fail("get_fount_symbols", "id", exc)


def _remember(found: dict[int, str], fount_id: int, name: object) -> None:
    if not isinstance(name, str):
        _fail("get_fount_symbols", "name")
    if fount_id in found:
        _fail("get_fount_symbols", f"duplicate id {fount_id}")
    found[fount_id] = name


def _fount_symbols(payload: object) -> dict[int, str]:
    if isinstance(payload, dict):
        found: dict[int, str] = {}
        for key, name in payload.items():
            _remember(found, _fount_id(key), name)
        return found
    if isinstance(payload, list):
        found = {}
        for item in payload:
            if not isinstance(item, dict) or "id" not in item or "name" not in item:
                _fail("get_fount_symbols", f"expected founts, got {payload!r}")
            _remember(found, _fount_id(item["id"]), item["name"])
        return found
    _fail("get_fount_symbols", f"expected founts, got {payload!r}")
