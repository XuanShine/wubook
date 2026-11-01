from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from math import isfinite
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from wubook_api import Wubook

_WRITE = ConfigDict(extra="forbid")

VariationType = Literal[-2, -1, 1, 2]
WeekPrices = tuple[float, float, float, float, float, float, float]

_VARIATION_CODES = frozenset({-2, -1, 1, 2})
_DAILY_FLOOR = Decimal("0.01")
_WEEK_LENGTH = 7


class PlanVariation(BaseModel):
    """The type carries the sign, so the amount is greater than 0."""

    model_config = _WRITE

    variation_type: VariationType
    variation: float

    @field_validator("variation_type", mode="before")
    @classmethod
    def code(cls, value: object) -> int:
        return _variation_code(value)

    @field_validator("variation", mode="before")
    @classmethod
    def amount(cls, value: object) -> float:
        return _amount(value)


class PricingPlan(BaseModel):
    """vpid 0 means the parent is parity.

    The parent, the amount, and the type are all present or all absent.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    daily: Literal[0, 1]
    vpid: int | None = None
    variation: float | None = None
    variation_type: VariationType | None = None

    @field_validator("id", "vpid", mode="before")
    @classmethod
    def plan_id(cls, value: object, info: ValidationInfo) -> object:
        name = info.field_name or "id"
        return _plan_id(value, name, allow_none=name == "vpid")

    @field_validator("daily", mode="before")
    @classmethod
    def daily_flag(cls, value: object) -> int:
        return _daily(value)

    @field_validator("variation_type", mode="before")
    @classmethod
    def code(cls, value: object) -> int | None:
        if value is None:
            return None
        return _variation_code(value)

    @field_validator("variation", mode="before")
    @classmethod
    def amount(cls, value: object) -> float | None:
        if value is None:
            return None
        return _amount(value)

    @model_validator(mode="after")
    def virtual_fields(self) -> PricingPlan:
        present = (
            self.vpid is not None,
            self.variation is not None,
            self.variation_type is not None,
        )
        if any(present) and not all(present):
            raise ValueError("virtual")
        if self.vpid is None:
            return self
        if self.id == 0:
            raise ValueError("parity")
        if self.vpid == self.id:
            raise ValueError("vpid")
        return self

    @property
    def parity(self) -> bool:
        return self.id == 0

    @property
    def virtual(self) -> bool:
        return self.vpid is not None


class PlanRack(BaseModel):
    """Seven prices per room, each greater than 0.

    The weekday at index 0 is unnamed.
    """

    model_config = _WRITE

    rooms: dict[int, WeekPrices]

    @field_validator("rooms", mode="before")
    @classmethod
    def weekdays(cls, value: object) -> dict[int, tuple[float, ...]]:
        return _price_rows(value, _open_price, _WEEK_LENGTH)


class DailyPrices(BaseModel):
    """Index 0 of each room is start. A shorter tuple sends fewer days."""

    model_config = _WRITE

    start: date
    rooms: dict[int, tuple[float, ...]]

    @field_validator("start", mode="before")
    @classmethod
    def plain_start(cls, value: object) -> date:
        return _plain_date(value)

    @field_validator("rooms", mode="before")
    @classmethod
    def room_prices(cls, value: object) -> dict[int, tuple[float, ...]]:
        return _price_rows(value, _daily_price, None)


class HandlingPrices(Wubook):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def add_pricing_plan(self, name: str) -> int:
        return _assigned_id("add_pricing_plan", self._call("add_pricing_plan", (_name(name),)))

    def add_vplan(self, name: str, parent_id: int, variation: PlanVariation) -> int:
        wired_name = _name(name)
        parent = _pid(parent_id, "parent_id")
        if not isinstance(variation, PlanVariation):
            raise ValueError("variation")
        return _assigned_id(
            "add_vplan",
            self._call(
                "add_vplan",
                (wired_name, parent, variation.variation_type, variation.variation),
            ),
        )

    def del_plan(self, pid: int) -> None:
        self._call("del_plan", (_pid_not_parity(pid),))

    def update_plan_name(self, pid: int, name: str) -> None:
        self._call("update_plan_name", (_pid(pid, "pid"), _name(name)))

    def get_pricing_plans(self) -> list[PricingPlan]:
        return _parse_plans(self._call("get_pricing_plans", ()))

    def update_plan_rack(self, pid: int, rack: PlanRack) -> None:
        plan = _pid_not_parity(pid)
        if not isinstance(rack, PlanRack):
            raise ValueError("rack")
        self._call("update_plan_rack", (plan, _wire_rooms(rack.rooms)))

    def mod_vplans(self, plans: dict[int, PlanVariation]) -> None:
        self._call("mod_vplans", (_wire_vplans(_vplan_updates(plans)),))

    def update_plan_prices(self, pid: int, prices: DailyPrices) -> None:
        plan = _pid(pid, "pid")
        if not isinstance(prices, DailyPrices):
            raise ValueError("prices")
        self._call(
            "update_plan_prices",
            (plan, _wire_date(prices.start), _wire_rooms(prices.rooms)),
        )

    def fetch_plan_prices(
        self,
        pid: int,
        dfrom: date,
        dto: date,
        rooms: Sequence[int] | None = None,
    ) -> dict[int, dict[date, float]]:
        """Both ends are included."""
        plan = _pid(pid, "pid")
        start = _plain_date(dfrom)
        end = _plain_date(dto)
        if start > end:
            raise ValueError("dfrom is after dto")
        ids = _room_ids(rooms)
        args: tuple[object, ...] = (plan, _wire_date(start), _wire_date(end))
        if ids is not None:
            args = (*args, ids)
        return _parse_daily(self._call("fetch_plan_prices", args), start, end)

    def _call(self, method: str, args: tuple[object, ...]) -> object:
        remote = getattr(self.server, method)
        return self._payload(method, remote(self.token, self.lcode, *args))

    def _payload(self, method: str, result: object) -> object:
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise ConnectionError(f"{method}: unexpected response {result!r}")
        code, data = result
        if code != 0:
            raise ConnectionError(f"{method} failed ({code}): {data}")
        return data


def _plain_date(value: object) -> date:
    # datetime subclasses date, so only an exact date is a Wired day.
    if type(value) is not date:
        raise ValueError("date")
    return value


def _name(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError("name")
    return value


def _pid(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name)
    return value


def _pid_not_parity(value: object) -> int:
    pid = _pid(value, "pid")
    if pid == 0:
        raise ValueError("parity")
    return pid


def _plan_id(value: object, name: str, *, allow_none: bool) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name)
    return value


def _daily(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError("daily")


def _variation_code(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in _VARIATION_CODES:
        raise ValueError("variation_type")
    return value


def _amount(value: object) -> float:
    number = _finite_number(value, "variation")
    if number <= 0:
        raise ValueError("variation")
    return number


def _open_price(value: object) -> float:
    number = _finite_number(value, "price")
    if number <= 0:
        raise ValueError("price")
    return number


def _daily_price(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("price")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("price")
    if Decimal(str(value)) <= _DAILY_FLOOR:
        raise ValueError("price")
    try:
        return float(value)
    except OverflowError:
        raise ValueError("price") from None


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name)
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(name)
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(name) from None
    if not isfinite(number):
        raise ValueError(name)
    return number


def _price_rows(
    value: object,
    parse_price: Callable[[object], float],
    exact_length: int | None,
) -> dict[int, tuple[float, ...]]:
    if not isinstance(value, dict) or not value:
        raise ValueError("rooms")
    rows: dict[int, tuple[float, ...]] = {}
    for room_id, raw in value.items():
        if isinstance(room_id, bool) or not isinstance(room_id, int) or room_id <= 0:
            raise ValueError("room_id")
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ValueError("prices")
        if exact_length is None:
            if len(raw) == 0:
                raise ValueError("prices")
        elif len(raw) != exact_length:
            raise ValueError("prices")
        rows[room_id] = tuple(parse_price(item) for item in raw)
    return rows


def _room_ids(rooms: Sequence[int] | None) -> list[int] | None:
    if rooms is None:
        return None
    if isinstance(rooms, (str, bytes)) or not isinstance(rooms, Sequence):
        raise ValueError("rooms")
    ids: list[int] = []
    seen: set[int] = set()
    for room_id in rooms:
        if isinstance(room_id, bool) or not isinstance(room_id, int) or room_id <= 0:
            raise ValueError("room_id")
        if room_id in seen:
            raise ValueError("duplicate room id")
        seen.add(room_id)
        ids.append(room_id)
    if not ids:
        raise ValueError("rooms")
    return ids


def _wire_date(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def _wire_rooms(rooms: dict[int, tuple[float, ...]]) -> dict[str, list[float]]:
    # xmlrpc.client rejects a struct whose key is an int.
    return {str(room_id): list(amounts) for room_id, amounts in rooms.items()}


def _vplan_updates(plans: object) -> dict[int, PlanVariation]:
    if not isinstance(plans, dict) or not plans:
        raise ValueError("plans")
    checked: dict[int, PlanVariation] = {}
    for pid, variation in plans.items():
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
            raise ValueError("pid")
        if pid == 0:
            raise ValueError("parity")
        if not isinstance(variation, PlanVariation):
            raise ValueError("variation")
        checked[pid] = variation
    return checked


def _wire_vplans(plans: dict[int, PlanVariation]) -> list[dict[str, int | float]]:
    return [
        {
            "pid": pid,
            "variation": variation.variation,
            "variation_type": variation.variation_type,
        }
        for pid, variation in plans.items()
    ]


def _parse_plans(payload: object) -> list[PricingPlan]:
    if not isinstance(payload, list):
        raise ConnectionError(f"get_pricing_plans: expected a list, got {payload!r}")
    plans: list[PricingPlan] = []
    seen: set[int] = set()
    for item in payload:
        try:
            plan = PricingPlan.model_validate(item)
        except ValidationError as exc:
            raise ConnectionError(f"get_pricing_plans: plan {item!r}") from exc
        if plan.id in seen:
            raise ConnectionError(f"get_pricing_plans: duplicate plan id {plan.id}")
        seen.add(plan.id)
        plans.append(plan)
    return plans


def _parse_room_key(key: object) -> int:
    if isinstance(key, str) and key.isascii() and key.isdigit():
        room_id = int(key)
    elif isinstance(key, bool) or not isinstance(key, int):
        raise ConnectionError(f"fetch_plan_prices: room id {key!r}")
    else:
        room_id = key
    if room_id <= 0:
        raise ConnectionError(f"fetch_plan_prices: room id {key!r}")
    return room_id


def _fetched_price(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("price")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("price") from None
    if not isfinite(number):
        raise ValueError("price")
    return number


def _parse_daily(
    payload: object,
    dfrom: date,
    dto: date,
) -> dict[int, dict[date, float]]:
    if not isinstance(payload, dict):
        raise ConnectionError(f"fetch_plan_prices: expected a dict, got {payload!r}")
    span = (dto - dfrom).days + 1
    parsed: dict[int, dict[date, float]] = {}
    for key, cells in payload.items():
        room_id = _parse_room_key(key)
        if room_id in parsed:
            raise ConnectionError(f"fetch_plan_prices: duplicate room id {room_id}")
        if not isinstance(cells, list) or len(cells) != span:
            got = len(cells) if isinstance(cells, list) else type(cells).__name__
            raise ConnectionError(
                f"fetch_plan_prices: room {room_id} expected {span} days, got {got}"
            )
        room_days: dict[date, float] = {}
        for offset, cell in enumerate(cells):
            day = dfrom + timedelta(days=offset)
            try:
                room_days[day] = _fetched_price(cell)
            except ValueError as exc:
                raise ConnectionError(
                    f"fetch_plan_prices: room {room_id} on {_wire_date(day)}"
                ) from exc
        parsed[room_id] = room_days
    return parsed


def _assigned_id(method: str, payload: object) -> int:
    if isinstance(payload, bool) or not isinstance(payload, int) or payload <= 0:
        raise ConnectionError(f"{method}: expected a plan id, got {payload!r}")
    return payload
