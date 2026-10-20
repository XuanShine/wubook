from datetime import date

from fastapi.testclient import TestClient

from comptoir.app import create_app
from tests.test_desk import FakeRpc, day, open_desk, room_raw

IN = date(2026, 10, 21)
OUT = date(2026, 10, 22)
FORBIDDEN = {"montant", "amount", "carte", "ccard", "ignore_restrs", "ignore_avail"}


def build(tmp_path):
    rpc = FakeRpc(
        [room_raw(329667, "Double Balcon")],
        {"329667": [day(2), day(1)]},
        {"329667": [90]},
    )
    desk = open_desk(tmp_path, rpc, "329667: yes\n")
    return rpc, create_app(desk), desk


def test_search_returns_lignes_and_the_model_has_no_amount(tmp_path):
    rpc, app, desk = build(tmp_path)
    spec = app.openapi()
    for name, schema in spec["components"]["schemas"].items():
        assert FORBIDDEN.isdisjoint(schema.get("properties", {})), name
    assert "lignes" in spec["components"]["schemas"]["Lignes"]["properties"]
    for path in ("/outils/chercher", "/outils/reserver"):
        summary = spec["paths"][path]["post"]["summary"]
        assert "lignes" in summary
        assert "inventer" in summary
    with TestClient(app) as client:
        response = client.post(
            "/outils/chercher",
            json={
                "arrivee": IN.isoformat(),
                "depart": OUT.isoformat(),
                "adultes": 2,
                "enfants": 0,
                "montant": 10,
                "carte": "4111",
                "ignore_restrs": 1,
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["lignes"] == ["Double Balcon, 1 nuit, 90 euros"]
    assert "montant" not in body
    assert [call for call in rpc.calls if call[0] == "new_reservation"] == []
    desk.close()


def test_reserver_speaks_the_booking_and_ignores_an_amount(tmp_path):
    rpc, app, desk = build(tmp_path)
    payload = {
        "cle": "tool-1",
        "chambre": 329667,
        "arrivee": IN.isoformat(),
        "depart": OUT.isoformat(),
        "adultes": 2,
        "enfants": 0,
        "client": {"nom": "Martin", "pays": "fr"},
        "montant": 1,
        "carte": "4111",
    }
    with TestClient(app) as client:
        first = client.post("/outils/reserver", json=payload)
        second = client.post("/outils/reserver", json=payload)
        missing = client.post("/outils/reserver", json={**payload, "cle": "tool-2", "client": {"pays": "FR"}})
        bad_country = client.post(
            "/outils/reserver",
            json={**payload, "cle": "tool-3", "client": {"nom": "Martin", "pays": "FRA"}},
        )
    assert first.status_code == 200
    assert first.json()["lignes"] == ["Réservé, code R42, Double Balcon, 1 nuit, 90 euros"]
    assert second.json()["lignes"] == ["Déjà réservé, code R42, Double Balcon, 1 nuit, 90 euros"]
    assert missing.json()["lignes"] == ["nom manquant"]
    assert bad_country.json()["lignes"] == ["pays invalide"]
    placed = [call[1] for call in rpc.calls if call[0] == "new_reservation"]
    assert len(placed) == 1
    assert placed[0]["origin"] == "voix"
    assert placed[0]["customer"] == {"lname": "Martin", "country": "FR"}
    assert placed[0]["amount"] == 90
    desk.close()
