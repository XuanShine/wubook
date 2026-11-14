from __future__ import annotations

from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wubook_api import Wubook

BoardCode = Literal["bb", "fb", "hb", "nb", "ai"]


class RoomType(IntEnum):
    """rtype values from the Wired rooms chapter."""

    ROOM = 1
    APARTMENT = 2
    BED = 3
    UNIT = 4
    BUNGALOW = 5
    TENT = 6
    VILLA = 7
    CHALET = 8
    RV_PARK = 9


class BoardVariation(BaseModel):
    """Optional meal-board price change on the booking engine.

    dtype is 2 (static increase), 1 (percentage increase),
    -1 (percentage decrease) or -2 (static decrease).
    """

    model_config = ConfigDict(extra="ignore")

    dtype: Literal[-2, -1, 1, 2]
    value: float


class Room(BaseModel):
    """One room from fetch_rooms or fetch_single_room.

    A virtual room has subroom set to its availability parent.
    A room with price anchorage has anchorate set to its price parent.
    Those two parents can differ. Availability sent for a virtual room is
    ignored, and prices sent for an anchored room are ignored.

    ancillary=1 also returns names and descriptions per language, when set.
    """

    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="Room id")
    name: str = Field(description="Main name of the room")
    shortname: str = Field(description="Unique shortname for this property. 4 characters max.")
    occupancy: int = Field(ge=0, description="Adults plus children")
    men: int = Field(ge=0, description="Adults")
    children: int = Field(ge=0, description="Children")
    subroom: int = Field(
        ge=0,
        description="Parent room id when virtual, otherwise 0",
    )
    anchorate: int = Field(
        ge=0,
        description="Price-parent room id, otherwise 0",
    )
    price: float = Field(description="Default price")
    availability: int = Field(ge=0, description="Default availability")
    board: BoardCode = Field(description="Default board: bb, fb, hb, nb or ai")
    boards: dict[str, BoardVariation] = Field(
        default_factory=dict,
        description="Optional boards and their price variation",
    )
    woodoo: int = Field(
        description="1 when the room is WooDoo only, not sold on the booking engine",
    )
    dec_avail: int = Field(
        ge=0,
        description="Availability decreased on the parent room",
    )
    min_price: float = Field(
        description="Minimum price. 0, or at least 5 when a limit is set",
    )
    max_price: float = Field(
        description="Maximum price. 0, or at least 5 when a limit is set",
    )
    rtype: int = Field(description="Product type id")
    rtype_name: str = Field(description="Product type label")
    names: dict[str, str] | None = Field(
        default=None,
        description="Localized names, present when ancillary=1 and previously set",
    )
    descriptions: dict[str, str] | None = Field(
        default=None,
        description="Localized descriptions, present when ancillary=1 and previously set",
    )

    @field_validator("boards", mode="before")
    @classmethod
    def empty_boards(cls, value: object) -> object:
        if value in (None, "", False):
            return {}
        return value

    @field_validator("woodoo", mode="before")
    @classmethod
    def woodoo_flag(cls, value: object) -> object:
        if isinstance(value, bool):
            return int(value)
        return value

    @field_validator("min_price", "max_price")
    @classmethod
    def price_bound(cls, value: float) -> float:
        if value < 0 or 0 < value < 5:
            raise ValueError("must be 0, or greater than or equal to 5")
        return value

    @model_validator(mode="after")
    def occupancy_matches_party(self) -> Room:
        if self.men + self.children != self.occupancy:
            raise ValueError("men + children must equal occupancy")
        return self

    @property
    def virtual(self) -> bool:
        return self.subroom != 0

    @property
    def anchored(self) -> bool:
        return self.anchorate != 0


class RoomImage(BaseModel):
    """One image from room_images. main_image is 1 for the default image."""

    model_config = ConfigDict(extra="ignore")

    id_room: int
    image_link: str
    main_image: int

    @field_validator("main_image", mode="before")
    @classmethod
    def main_flag(cls, value: object) -> object:
        if isinstance(value, bool):
            return int(value)
        return value

    @property
    def main(self) -> bool:
        return self.main_image == 1


class HandlingRooms(Wubook):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def fetch_rooms(self, ancillary: int = 0) -> list[Room]:
        """Rooms of the property.

        ancillary=1 also returns names and descriptions for each language,
        when those translations were set.
        """
        payload = self._payload(
            "fetch_rooms",
            self.server.fetch_rooms(self.token, self.lcode, _ancillary(ancillary)),
        )
        if not isinstance(payload, list):
            raise ConnectionError(f"fetch_rooms: expected a list, got {payload!r}")
        return [Room.model_validate(item) for item in payload]

    def fetch_single_room(self, room_id: int, ancillary: int = 0) -> Room:
        """One room. ancillary=1 adds localized names and descriptions."""
        payload = self._payload(
            "fetch_single_room",
            self.server.fetch_single_room(
                self.token,
                self.lcode,
                room_id,
                _ancillary(ancillary),
            ),
        )
        if not isinstance(payload, dict):
            raise ConnectionError(f"fetch_single_room: expected a room, got {payload!r}")
        return Room.model_validate(payload)

    def fetch_room(self, room_id: int, ancillary: int = 0) -> Room:
        return self.fetch_single_room(room_id, ancillary)

    def new_room(
        self,
        *,
        woodoo: int,
        name: str,
        beds: int,
        price: float,
        availability: int,
        shortname: str,
        board: BoardCode,
        names: dict[str, str] | None = None,
        descriptions: dict[str, str] | None = None,
        boards: dict[str, BoardVariation] | None = None,
        rtype: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> int:
        """Create a standard room. Wired new_room. Returns the assigned room id.

        price is defprice, availability is avail and board is defboard.
        Arguments after board follow the Wired order: names, descriptions,
        boards, rtype, min_price, max_price. A gap is sent as {} or, for the
        two price limits, 0. rtype has no default, so it is required when
        min_price or max_price is set.
        """
        extra = _tail(
            [
                ("names", _langs(names, "names"), {}),
                ("descriptions", _langs(descriptions, "descriptions"), {}),
                ("boards", _boards(boards), {}),
                ("rtype", _rtype(rtype), _REQUIRED),
                ("min_price", _price_limit(min_price, "min_price"), 0),
                ("max_price", _price_limit(max_price, "max_price"), 0),
            ]
        )
        payload = self._call(
            "new_room",
            (
                _flag(woodoo, "woodoo"),
                _text_name(name),
                _beds(beds),
                _price(price),
                _availability(availability),
                _shortname(shortname),
                _board(board),
                *extra,
            ),
        )
        return _assigned_id("new_room", payload)

    def new_virtual_room(
        self,
        *,
        parent_id: int,
        woodoo: int,
        name: str,
        beds: int,
        children: int,
        price: float,
        shortname: str,
        board: BoardCode,
        names: dict[str, str] | None = None,
        descriptions: dict[str, str] | None = None,
        boards: dict[str, BoardVariation] | None = None,
        dec_avail: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> int:
        """Create a virtual room. Wired new_virtual_room. Returns the assigned room id.

        parent_id is the Wired rid of the availability parent. beds includes
        children. A gap in the optional tail is sent as {}, or 0 for
        dec_avail, min_price and max_price.
        """
        occupancy = _beds(beds)
        extra = _tail(
            [
                ("names", _langs(names, "names"), {}),
                ("descriptions", _langs(descriptions, "descriptions"), {}),
                ("boards", _boards(boards), {}),
                ("dec_avail", _count(dec_avail, "dec_avail"), 0),
                ("min_price", _price_limit(min_price, "min_price"), 0),
                ("max_price", _price_limit(max_price, "max_price"), 0),
            ]
        )
        payload = self._call(
            "new_virtual_room",
            (
                _room_id(parent_id, "parent_id"),
                _flag(woodoo, "woodoo"),
                _text_name(name),
                occupancy,
                _children(children, occupancy),
                _price(price),
                _shortname(shortname),
                _board(board),
                *extra,
            ),
        )
        return _assigned_id("new_virtual_room", payload)

    def mod_room(
        self,
        room_id: int,
        *,
        name: str,
        beds: int,
        price: float,
        availability: int,
        shortname: str,
        board: BoardCode,
        names: dict[str, str] | None = None,
        descriptions: dict[str, str] | None = None,
        boards: dict[str, BoardVariation] | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        rtype: int | None = None,
        woodoo_only: int | None = None,
    ) -> int:
        """Modify a standard room. Wired mod_room. Returns the room id.

        Optional arguments stay in Wired order. XML-RPC cannot skip one of
        them, so a later value requires every earlier one. Nothing is filled
        in: an omitted min_price stays omitted instead of being reset to 0.
        """
        extra = _explicit(
            [
                ("names", _langs(names, "names")),
                ("descriptions", _langs(descriptions, "descriptions")),
                ("boards", _boards(boards)),
                ("min_price", _price_limit(min_price, "min_price")),
                ("max_price", _price_limit(max_price, "max_price")),
                ("rtype", _rtype(rtype)),
                ("woodoo_only", _optional_flag(woodoo_only, "woodoo_only")),
            ]
        )
        payload = self._call(
            "mod_room",
            (
                _room_id(room_id),
                _text_name(name),
                _beds(beds),
                _price(price),
                _availability(availability),
                _shortname(shortname),
                _board(board),
                *extra,
            ),
        )
        return _assigned_id("mod_room", payload)

    def mod_virtual_room(
        self,
        room_id: int,
        *,
        name: str,
        beds: int,
        children: int,
        price: float,
        shortname: str,
        board: BoardCode,
        names: dict[str, str] | None = None,
        descriptions: dict[str, str] | None = None,
        boards: dict[str, BoardVariation] | None = None,
        dec_avail: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        woodoo_only: int | None = None,
    ) -> int:
        """Modify a virtual room. Wired mod_virtual_room. Returns the room id.

        shortname is the Wired argument short. The same positional rule as
        mod_room applies to the optional tail.
        """
        occupancy = _beds(beds)
        extra = _explicit(
            [
                ("names", _langs(names, "names")),
                ("descriptions", _langs(descriptions, "descriptions")),
                ("boards", _boards(boards)),
                ("dec_avail", _count(dec_avail, "dec_avail")),
                ("min_price", _price_limit(min_price, "min_price")),
                ("max_price", _price_limit(max_price, "max_price")),
                ("woodoo_only", _optional_flag(woodoo_only, "woodoo_only")),
            ]
        )
        payload = self._call(
            "mod_virtual_room",
            (
                _room_id(room_id),
                _text_name(name),
                occupancy,
                _children(children, occupancy),
                _price(price),
                _shortname(shortname),
                _board(board),
                *extra,
            ),
        )
        return _assigned_id("mod_virtual_room", payload)

    def del_room(self, room_id: int) -> None:
        """Delete a room. Wired del_room."""
        self._call("del_room", (_room_id(room_id),))

    def room_images(self, room_id: int) -> list[RoomImage]:
        """Images of one room. main_image is 1 on the default image."""
        payload = self._call("room_images", (_room_id(room_id),))
        if not isinstance(payload, list):
            raise ConnectionError(f"room_images: expected a list, got {payload!r}")
        return [RoomImage.model_validate(item) for item in payload]

    def push_update_activation(self, url: str) -> str:
        """Register the URL Wired POSTs when room values change.

        The POST carries lcode, dfrom and dto. Pass url="" to disable it.
        """
        if not isinstance(url, str):
            raise ValueError("url")
        payload = self._call("push_update_activation", (url,))
        return _message("push_update_activation", payload)

    def push_update_url(self) -> str:
        """URL currently registered for update notifications. Empty when disabled."""
        payload = self._call("push_update_url", ())
        return _message("push_update_url", payload)

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


_REQUIRED = object()
_BOARD_CODES = ("bb", "fb", "hb", "nb", "ai")


def _ancillary(value: int) -> int:
    if value not in (0, 1):
        raise ValueError("ancillary must be 0 or 1")
    return value


def _tail(slots: list[tuple[str, object, object]]) -> list[object]:
    """Keep a Wired optional suffix, filling only documented create defaults."""
    last = -1
    for index, (_, value, _) in enumerate(slots):
        if value is not None:
            last = index
    sent: list[object] = []
    for name, value, default in slots[: last + 1]:
        if value is None:
            if default is _REQUIRED:
                raise ValueError(f"{name} is required when a later argument is set")
            sent.append(default)
        else:
            sent.append(value)
    return sent


def _explicit(slots: list[tuple[str, object]]) -> list[object]:
    """Keep a Wired optional suffix. A hole is an error, so a modify cannot reset a skipped field."""
    last = -1
    for index, (_, value) in enumerate(slots):
        if value is not None:
            last = index
    sent: list[object] = []
    for name, value in slots[: last + 1]:
        if value is None:
            raise ValueError(f"{name} is required when a later argument is set")
        sent.append(value)
    return sent


def _room_id(value: int, name: str = "room_id") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(name)
    return value


def _flag(value: int, name: str) -> int:
    if isinstance(value, bool):
        value = int(value)
    if value not in (0, 1):
        raise ValueError(f"{name} must be 0 or 1")
    return value


def _optional_flag(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    return _flag(value, name)


def _text_name(value: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError("name")
    return value


def _shortname(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 4:
        raise ValueError("shortname must be 1 to 4 characters")
    return value


def _board(value: str) -> str:
    if value not in _BOARD_CODES:
        raise ValueError("board")
    return value


def _beds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("beds")
    return value


def _children(value: int, beds: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > beds:
        raise ValueError("children")
    return value


def _availability(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("availability")
    return value


def _count(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name)
    return value


def _price(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("price")
    return float(value)


def _price_limit(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name)
    number = float(value)
    if number < 0 or 0 < number < 5:
        raise ValueError(f"{name} must be 0, or greater than or equal to 5")
    return number


def _rtype(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("rtype")
    return int(RoomType(value))


def _langs(value: dict[str, str] | None, name: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(name)
    for key, text in value.items():
        if not isinstance(key, str) or not isinstance(text, str):
            raise ValueError(name)
    return value


def _boards(value: dict[str, BoardVariation] | None) -> dict[str, dict[str, int | float]] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("boards")
    wired: dict[str, dict[str, int | float]] = {}
    for key, raw in value.items():
        if key not in _BOARD_CODES:
            raise ValueError(f"unknown board {key!r}")
        variation = raw if isinstance(raw, BoardVariation) else BoardVariation.model_validate(raw)
        wired[key] = {"dtype": variation.dtype, "value": variation.value}
    return wired


def _assigned_id(method: str, payload: object) -> int:
    if isinstance(payload, bool) or not isinstance(payload, int) or payload <= 0:
        raise ConnectionError(f"{method}: expected a room id, got {payload!r}")
    return payload


def _message(method: str, payload: object) -> str:
    if not isinstance(payload, str):
        raise ConnectionError(f"{method}: expected a string, got {payload!r}")
    return payload
