from __future__ import annotations

from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from booking.model import (
    Guest,
    IdempotencyKey,
    Offer,
    Party,
    Reservation,
    ReservationCode,
    RoomId,
    Stay,
    dump_refusal,
)
from booking.service import (
    Counter,
    InFlight,
    KeyUsed,
    NotDirect,
    Rejected,
    ReserveResult,
    WireShape,
    WiredDown,
    WiredRejected,
)


def build_stay(
    check_in: date,
    checkout: date,
    adults: int,
    children: int,
    quantity: int,
) -> Stay:
    return Stay(
        check_in=check_in,
        checkout=checkout,
        party=Party(adults=adults, children=children),
        quantity=quantity,
    )


class GuestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_name: str
    first_name: str
    email: str
    city: str = ""
    phone: str = ""
    street: str = ""
    country: str = ""
    arrival_hour: str = ""
    notes: str = ""

    def to_guest(self) -> Guest:
        return Guest(
            last_name=self.last_name,
            first_name=self.first_name,
            email=self.email,
            city=self.city,
            phone=self.phone,
            street=self.street,
            country=self.country,
            arrival_hour=self.arrival_hour,
            notes=self.notes,
        )


class ReserveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_in: date
    checkout: date
    room_id: int
    adults: int
    children: int
    quantity: int
    guest: GuestBody
    idempotency_key: str


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = ""


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({"message": message}, status_code=422)


def _wired_status(code: int) -> int:
    if code in (-9, -19):
        return 422
    if code in (-18, -20, -21):
        return 429
    return 502


def _offer_json(offer: Offer) -> dict[str, int]:
    return {
        "room_id": int(offer.room_id),
        "nights": offer.nights,
        "total_cents": int(offer.total_cents),
        "quantity": offer.quantity,
    }


def _guest_json(guest: Guest) -> dict[str, str]:
    return {
        "last_name": guest.last_name,
        "first_name": guest.first_name,
        "email": guest.email,
        "city": guest.city,
        "phone": guest.phone,
        "street": guest.street,
        "country": guest.country,
        "arrival_hour": guest.arrival_hour,
        "notes": guest.notes,
    }


def _reservation_json(reservation: Reservation) -> dict[str, object]:
    party = None
    if reservation.party is not None:
        party = {"adults": reservation.party.adults, "children": reservation.party.children}
    return {
        "code": str(reservation.code),
        "status": int(reservation.status),
        "check_in": reservation.check_in.isoformat(),
        "checkout": reservation.checkout.isoformat(),
        "party": party,
        "room_ids": [int(room_id) for room_id in reservation.room_ids],
        "guest": _guest_json(reservation.guest),
        "total_cents": int(reservation.total_cents),
        "currency": reservation.currency,
        "direct": reservation.direct,
    }


def _reserve_response(result: ReserveResult) -> JSONResponse:
    if isinstance(result, Reservation):
        return JSONResponse(_reservation_json(result))
    if isinstance(result, InFlight):
        return JSONResponse({"key": str(result.key)}, status_code=503)
    if isinstance(result, Rejected):
        return JSONResponse(
            {"message": result.message, "wired_code": result.wired_code},
            status_code=422,
        )
    if isinstance(result, KeyUsed):
        return JSONResponse({"reason": "key_used", "key": str(result.key)}, status_code=409)
    return JSONResponse(dump_refusal(result), status_code=409)


def _on_down(request: Request, exc: WiredDown) -> JSONResponse:
    return JSONResponse({"message": "Wired is unavailable"}, status_code=503)


def _on_shape(request: Request, exc: WireShape) -> JSONResponse:
    return JSONResponse({"message": "Wired returned an unexpected payload"}, status_code=502)


def _on_rejected(request: Request, exc: WiredRejected) -> JSONResponse:
    return JSONResponse({"code": exc.code, "message": exc.message}, status_code=_wired_status(exc.code))


def create_app(counter: Counter) -> FastAPI:
    """Dates are YYYY-MM-DD."""

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/rooms")
    def list_rooms() -> JSONResponse:
        body = [
            {
                "id": int(room.id),
                "name": room.name,
                "occupancy": room.occupancy,
                "availability_room_id": int(room.availability_room_id),
            }
            for room in counter.rooms()
        ]
        return JSONResponse(body)

    @app.get("/rooms/{room_id}/price")
    def room_price(
        room_id: int,
        check_in: date,
        checkout: date,
        adults: int,
        children: int = 0,
        quantity: int = 1,
    ) -> JSONResponse:
        try:
            stay = build_stay(check_in, checkout, adults, children, quantity)
            result = counter.price(stay, RoomId(room_id), date.today())
        except ValueError as exc:
            return _bad_request(str(exc))
        if isinstance(result, Offer):
            return JSONResponse(_offer_json(result))
        return JSONResponse(dump_refusal(result))

    @app.post("/reservations")
    def post_reservation(body: ReserveBody) -> JSONResponse:
        try:
            stay = build_stay(body.check_in, body.checkout, body.adults, body.children, body.quantity)
            result = counter.reserve(
                stay,
                RoomId(body.room_id),
                body.guest.to_guest(),
                IdempotencyKey(body.idempotency_key),
                date.today(),
            )
        except ValueError as exc:
            return _bad_request(str(exc))
        return _reserve_response(result)

    @app.get("/reservations/{code}")
    def read_reservation(code: str) -> JSONResponse:
        try:
            found = counter.reservation(ReservationCode(code))
        except ValueError as exc:
            return _bad_request(str(exc))
        return JSONResponse(_reservation_json(found))

    @app.post("/reservations/{code}/cancellation")
    def post_cancellation(code: str, body: CancelBody) -> JSONResponse:
        try:
            result = counter.cancel(ReservationCode(code), body.reason)
        except ValueError as exc:
            return _bad_request(str(exc))
        if isinstance(result, NotDirect):
            return JSONResponse({"reason": "not_direct", "code": str(result.code)}, status_code=409)
        return JSONResponse(_reservation_json(result))

    app.add_exception_handler(WiredDown, _on_down)
    app.add_exception_handler(WireShape, _on_shape)
    app.add_exception_handler(WiredRejected, _on_rejected)
    return app
