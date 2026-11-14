from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
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


class DayPatch(BaseModel):
    """None omits that key. Both None is sent as {}."""

    model_config = _WRITE

    avail: int | None = None
    no_ota: Literal[0, 1] | None = None

    @field_validator("avail", mode="before")
    @classmethod
    def units(cls, value: object) -> int | None:
        return _optional_units(value, "avail")

    @field_validator("no_ota", mode="before")
    @classmethod
    def ota_flag(cls, value: object) -> int | None:
        return _optional_flag(value)


class RoomGrid(BaseModel):
    """start is the first day of every room.

    Every tuple has the same non-zero length.
    """

    model_config = _WRITE

    start: date
    rooms: dict[int, tuple[DayPatch, ...]]

    @field_validator("start", mode="before")
    @classmethod
    def plain_start(cls, value: object) -> date:
        return _plain_date(value)

    @field_validator("rooms", mode="before")
    @classmethod
    def room_ids(cls, value: object) -> object:
        return _room_map(value)

    @model_validator(mode="after")
    def aligned(self) -> RoomGrid:
        length = len(next(iter(self.rooms.values())))
        if length == 0 or any(len(days) != length for days in self.rooms.values()):
            raise ValueError("days")
        for days in self.rooms.values():
            if all(patch.avail is None and patch.no_ota is None for patch in days):
                raise ValueError("room sets neither avail nor no_ota")
        return self

    @property
    def end(self) -> date:
        length = len(next(iter(self.rooms.values())))
        return self.start + timedelta(days=length - 1)


class SparseDay(BaseModel):
    """At least one of avail or no_ota is set."""

    model_config = _WRITE

    date: date
    avail: int | None = None
    no_ota: Literal[0, 1] | None = None

    @field_validator("date", mode="before")
    @classmethod
    def plain_date(cls, value: object) -> date:
        return _plain_date(value)

    @field_validator("avail", mode="before")
    @classmethod
    def units(cls, value: object) -> int | None:
        return _optional_units(value, "avail")

    @field_validator("no_ota", mode="before")
    @classmethod
    def ota_flag(cls, value: object) -> int | None:
        return _optional_flag(value)

    @model_validator(mode="after")
    def changes_a_field(self) -> SparseDay:
        if self.avail is None and self.no_ota is None:
            raise ValueError("set avail, no_ota, or both")
        return self


class SparseUpdate(BaseModel):
    """Dates inside one room are unique."""

    model_config = _WRITE

    rooms: dict[int, tuple[SparseDay, ...]]

    @field_validator("rooms", mode="before")
    @classmethod
    def room_ids(cls, value: object) -> object:
        return _room_map(value)

    @model_validator(mode="after")
    def isolated(self) -> SparseUpdate:
        for days in self.rooms.values():
            if len(days) == 0:
                raise ValueError("days")
            seen: set[date] = set()
            for day in days:
                if day.date in seen:
                    raise ValueError("date")
                seen.add(day.date)
        return self


class RoomDay(BaseModel):
    """Price and restriction keys are dropped."""

    model_config = ConfigDict(extra="ignore")

    avail: int
    no_ota: Literal[0, 1]
    booked: int

    @field_validator("avail", "booked", mode="before")
    @classmethod
    def units(cls, value: object, info: ValidationInfo) -> int:
        return _units(value, info.field_name or "units")

    @field_validator("no_ota", mode="before")
    @classmethod
    def ota_flag(cls, value: object) -> int:
        return _flag(value)


class UpdatingAvailability(Wubook):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def update_avail(self, grid: RoomGrid) -> None:
        if not isinstance(grid, RoomGrid):
            raise ValueError("grid")
        dfrom, rooms = _wire_grid(grid)
        self._call("update_avail", (dfrom, rooms))

    def update_sparse_avail(self, update: SparseUpdate) -> None:
        if not isinstance(update, SparseUpdate):
            raise ValueError("update")
        self._call("update_sparse_avail", (_wire_sparse(update),))

    def fetch_rooms_values(
        self,
        dfrom: date,
        dto: date,
        rooms: Sequence[int] | None = None,
    ) -> dict[int, dict[date, RoomDay]]:
        """Both ends are included.

        A date after the day two years ahead is rejected.
        """
        start = _plain_date(dfrom)
        end = _plain_date(dto)
        if start > end:
            raise ValueError("dfrom is after dto")
        limit = _horizon(date.today())
        if start > limit or end > limit:
            raise ValueError("date is more than 2 years ahead")
        ids = _room_ids(rooms)
        args: tuple[object, ...] = (_wire_date(start), _wire_date(end))
        if ids is not None:
            args = (*args, ids)
        return _parse_values(self._call("fetch_rooms_values", args), start, end)

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


def _units(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name)
    return value


def _optional_units(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _units(value, name)


def _flag(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        raise ValueError("no_ota")
    return value


def _optional_flag(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    return _flag(value)


def _room_map(value: object) -> object:
    if not isinstance(value, dict) or not value:
        raise ValueError("rooms")
    for room_id in value:
        if isinstance(room_id, bool) or not isinstance(room_id, int) or room_id <= 0:
            raise ValueError("room_id")
    return value


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


def _horizon(today: date) -> date:
    try:
        return today.replace(year=today.year + 2)
    except ValueError:
        return today.replace(year=today.year + 2, day=28)


def _wire_date(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def _wire_patch(patch: DayPatch) -> dict[str, int]:
    wire: dict[str, int] = {}
    if patch.avail is not None:
        wire["avail"] = patch.avail
    if patch.no_ota is not None:
        wire["no_ota"] = patch.no_ota
    return wire


def _wire_grid(grid: RoomGrid) -> tuple[str, list[dict[str, object]]]:
    rooms: list[dict[str, object]] = []
    for room_id, days in grid.rooms.items():
        rooms.append({"id": room_id, "days": [_wire_patch(day) for day in days]})
    return _wire_date(grid.start), rooms


def _wire_sparse_day(day: SparseDay) -> dict[str, object]:
    wire: dict[str, object] = {"date": _wire_date(day.date)}
    if day.avail is not None:
        wire["avail"] = day.avail
    if day.no_ota is not None:
        wire["no_ota"] = day.no_ota
    return wire


def _wire_sparse(update: SparseUpdate) -> list[dict[str, object]]:
    rooms: list[dict[str, object]] = []
    for room_id, days in update.rooms.items():
        rooms.append({"id": str(room_id), "days": [_wire_sparse_day(day) for day in days]})
    return rooms


def _parse_room_key(key: object) -> int:
    if isinstance(key, str) and key.isascii() and key.isdigit():
        room_id = int(key)
    elif isinstance(key, bool) or not isinstance(key, int):
        raise ConnectionError(f"fetch_rooms_values: room id {key!r}")
    else:
        room_id = key
    if room_id <= 0:
        raise ConnectionError(f"fetch_rooms_values: room id {key!r}")
    return room_id


def _parse_values(
    payload: object,
    dfrom: date,
    dto: date,
) -> dict[int, dict[date, RoomDay]]:
    if not isinstance(payload, dict):
        raise ConnectionError(f"fetch_rooms_values: expected a dict, got {payload!r}")
    span = (dto - dfrom).days + 1
    parsed: dict[int, dict[date, RoomDay]] = {}
    for key, cells in payload.items():
        room_id = _parse_room_key(key)
        if room_id in parsed:
            raise ConnectionError(f"fetch_rooms_values: duplicate room id {room_id}")
        if not isinstance(cells, list) or len(cells) != span:
            got = len(cells) if isinstance(cells, list) else type(cells).__name__
            raise ConnectionError(
                f"fetch_rooms_values: room {room_id} expected {span} days, got {got}"
            )
        room_days: dict[date, RoomDay] = {}
        for offset, cell in enumerate(cells):
            day = dfrom + timedelta(days=offset)
            try:
                room_days[day] = RoomDay.model_validate(cell)
            except ValidationError as exc:
                raise ConnectionError(
                    f"fetch_rooms_values: room {room_id} on {_wire_date(day)}"
                ) from exc
        parsed[room_id] = room_days
    return parsed
