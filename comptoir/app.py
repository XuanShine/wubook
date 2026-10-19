from __future__ import annotations

import uuid
from collections.abc import Mapping

from fastapi import FastAPI
from nicegui import ui
from pydantic import BaseModel, ConfigDict, Field

from comptoir.desk import Desk
from comptoir.quote import (
    Blocked,
    Guest,
    IdempotencyKey,
    Offer,
    Origin,
    RoomId,
    Stay,
)

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

_SPEAK = "Lire les lignes à voix haute. Ne pas inventer de prix."
_desk_box: list[Desk | None] = [None]
_page_added = False


class Lignes(BaseModel):
    lignes: list[str]


class ChercherIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    arrivee: str
    depart: str
    adultes: int
    enfants: int
    quantite: int = 1


class ClientIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    nom: str = ""
    prenom: str = ""
    email: str = ""
    ville: str = ""
    telephone: str = ""
    rue: str = ""
    pays: str = ""
    heure: str = ""
    notes: str = ""


class ReserverIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cle: str
    chambre: int
    arrivee: str
    depart: str
    adultes: int
    enfants: int
    quantite: int = 1
    client: ClientIn = Field(default_factory=ClientIn)


def guest_from_form(fields: Mapping[str, str]) -> Guest | Blocked:
    data: dict[str, str] = {}
    for source, target in CLIENT_FIELDS:
        raw = fields.get(source, "")
        data[target] = "" if raw is None else str(raw).strip()
    if not data["lname"]:
        return Blocked("nom manquant")
    country = data["country"]
    if country and (len(country) != 2 or not country.isalpha()):
        return Blocked("pays invalide")
    if country:
        data["country"] = country.upper()
    return Guest(**data)


def chercher_lignes(desk: Desk, body: ChercherIn) -> dict[str, list[str]]:
    stay = Stay.from_iso(body.arrivee, body.depart, body.adultes, body.enfants, body.quantite)
    if isinstance(stay, Blocked):
        return {"lignes": [stay.line()]}
    found = desk.search(stay)
    if isinstance(found, Blocked):
        return {"lignes": [found.line()]}
    return {"lignes": [item.line() for item in found]}


def reserver_lignes(desk: Desk, body: ReserverIn) -> dict[str, list[str]]:
    stay = Stay.from_iso(body.arrivee, body.depart, body.adultes, body.enfants, body.quantite)
    if isinstance(stay, Blocked):
        return {"lignes": [stay.line()]}
    guest = guest_from_form(body.client.model_dump())
    if isinstance(guest, Blocked):
        return {"lignes": [guest.line()]}
    result = desk.book(stay, RoomId(body.chambre), guest, IdempotencyKey(body.cle), Origin.VOIX)
    return {"lignes": [result.line()]}


def _whole(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def page(desk: Desk) -> None:
    ui.label("Comptoir").classes("text-h5")
    ui.label("Chambres").classes("text-h6")
    shown: dict[RoomId, bool] = {}
    for row in desk.rooms():
        shown[row.room_id] = row.sell
        if row.parent_name:
            ui.label(f"{row.name}, stock de {row.parent_name}")
        else:
            ui.label(row.name)

        def on_switch(event, room_id: RoomId = row.room_id) -> None:
            sell = bool(event.value)
            if shown.get(room_id) == sell:
                return
            shown[room_id] = sell
            desk.set_sell(room_id, sell)

        ui.switch("En vente", value=row.sell, on_change=on_switch)

    ui.label("Séjour").classes("text-h6")
    arrivee = ui.input("Arrivée").props("type=date")
    depart = ui.input("Départ").props("type=date")
    adultes = ui.number("Adultes", value=2, min=0, precision=0)
    enfants = ui.number("Enfants", value=0, min=0, precision=0)
    quantite = ui.number("Quantité", value=1, min=1, precision=0)

    ui.label("Client").classes("text-h6")
    nom = ui.input("Nom")
    prenom = ui.input("Prénom")
    email = ui.input("Email")
    ville = ui.input("Ville")
    telephone = ui.input("Téléphone")
    rue = ui.input("Rue")
    pays = ui.input("Pays")
    heure = ui.input("Heure d'arrivée")
    notes = ui.input("Notes")
    results = ui.column()

    def form_fields() -> dict[str, str]:
        return {
            "nom": nom.value or "",
            "prenom": prenom.value or "",
            "email": email.value or "",
            "ville": ville.value or "",
            "telephone": telephone.value or "",
            "rue": rue.value or "",
            "pays": pays.value or "",
            "heure": heure.value or "",
            "notes": notes.value or "",
        }

    def on_search() -> None:
        results.clear()
        parsed = Stay.from_iso(
            arrivee.value or "",
            depart.value or "",
            _whole(adultes.value, 0),
            _whole(enfants.value, 0),
            _whole(quantite.value, 1),
        )
        if isinstance(parsed, Blocked):
            ui.notify(parsed.line())
            return
        found = desk.search(parsed)
        if isinstance(found, Blocked):
            ui.notify(found.line())
            return
        for verdict in found:
            with results:
                ui.label(verdict.line())
                if isinstance(verdict, Offer):
                    _reserve(desk, parsed, verdict, form_fields)

    ui.button("Chercher", on_click=on_search)


def _reserve(desk: Desk, stay: Stay, offer: Offer, fields) -> None:
    key = IdempotencyKey(uuid.uuid4().hex)

    def on_reserve() -> None:
        guest = guest_from_form(fields())
        if isinstance(guest, Blocked):
            ui.notify(guest.line())
            return
        ui.notify(desk.book(stay, offer.room_id, guest, key, Origin.GUICHET).line())

    ui.button("Réserver", on_click=on_reserve)


def create_app(desk: Desk) -> FastAPI:
    global _page_added
    _desk_box[0] = desk
    app = FastAPI(title="Comptoir")

    @app.post("/outils/chercher", response_model=Lignes, summary=f"Chercher un séjour. {_SPEAK}")
    def chercher(body: ChercherIn) -> dict[str, list[str]]:
        return chercher_lignes(desk, body)

    @app.post("/outils/reserver", response_model=Lignes, summary=f"Réserver une offre. {_SPEAK}")
    def reserver(body: ReserverIn) -> dict[str, list[str]]:
        return reserver_lignes(desk, body)

    if not _page_added:

        @ui.page("/")
        def index() -> None:
            current = _desk_box[0]
            assert current is not None
            page(current)

        _page_added = True

    ui.run_with(
        app,
        title="Comptoir",
        language="fr",
        show_welcome_message=False,
        storage_secret="comptoir-local",
    )
    return app
