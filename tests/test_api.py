"""API : une appli de l'association crée et lit des factures avec son jeton."""

import re
from datetime import date

import pytest

from app.extensions import db
from app.models import Activity, Customer, Invoice
from app.services import api_tokens, billing

from .conftest import login, make_draft


@pytest.fixture
def token(app):
    token, raw = api_tokens.create("adherents")
    db.session.commit()
    return raw


@pytest.fixture
def issuing_token(app):
    token, raw = api_tokens.create("adherents", can_issue=True)
    db.session.commit()
    return raw


def auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def payload(**overrides):
    data = {
        "idempotency_key": "cotisation-2026-foyer-12",
        "issuer": "ecole",
        "customer": {
            "external_id": "foyer-12",
            "kind": "particulier",
            "name": "Famille Martin",
            "email": "martin@example.test",
            "address": "3 rue des Lilas",
            "postcode": "45000",
            "city": "Orléans",
        },
        "subject": "Cotisation 2026-2027",
        "external_id": "cotisation-12",
        "lines": [
            {"label": "Adhésion", "unit_price": 3000},
            {"label": "Formation musicale", "quantity": "1", "unit_price": 21000, "description": "Élève : Léa"},
            {"label": "Réduction famille", "unit_price": -2000},
        ],
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("header", [{}, {"Authorization": "Bearer fct_faux"}, {"Authorization": "Basic x"}])
def test_a_token_is_required(client, issuer, header):
    response = client.get("/api/v1/emetteurs", headers=header)
    assert response.status_code == 401 and response.json["error"]["code"] == 401
    assert "Bearer" in response.headers["WWW-Authenticate"]


def test_session_cookie_is_not_enough(client, admin, issuer):
    assert client.get("/api/v1/emetteurs").status_code == 401


def test_revoked_token_is_refused(client, issuer, token):
    api_tokens.revoke(api_tokens.find(token))
    db.session.commit()
    assert client.get("/api/v1/emetteurs", headers=auth(token)).status_code == 401


def test_list_issuers(client, issuer, token):
    response = client.get("/api/v1/emetteurs", headers=auth(token))
    assert response.json["issuers"][0]["code"] == "ecole" and response.headers["Cache-Control"] == "no-store"


def test_create_a_draft_with_its_customer(client, issuer, token):
    response = client.post("/api/v1/factures", json=payload(), headers=auth(token))
    assert response.status_code == 201, response.json
    body = response.json
    assert body["status"] == "brouillon" and body["number"] is None and body["total_ttc"] == 22000
    invoice = db.session.get(Invoice, body["id"])
    customer = invoice.customer
    assert (customer.source, customer.external_id, customer.kind) == ("adherents", "foyer-12", Customer.PERSON)
    assert invoice.source == "adherents" and invoice.external_id == "cotisation-12"
    activity = db.session.scalar(db.select(Activity).filter_by(action="invoice.create"))
    assert activity.account_id is None and activity.api_token.name == "adherents" and activity.invoice_id == invoice.id


def test_retry_with_the_same_key_creates_nothing(client, issuer, token):
    first = client.post("/api/v1/factures", json=payload(), headers=auth(token))
    again = client.post("/api/v1/factures", json=payload(subject="autre"), headers=auth(token))
    assert again.status_code == 200 and again.json["id"] == first.json["id"]
    assert db.session.scalar(db.select(db.func.count(Invoice.id))) == 1


def test_customer_is_updated_not_duplicated(client, issuer, token):
    client.post("/api/v1/factures", json=payload(), headers=auth(token))
    moved = payload(idempotency_key="autre")
    moved["customer"] = {**moved["customer"], "address": "8 avenue Neuve"}
    client.post("/api/v1/factures", json=moved, headers=auth(token))
    (customer,) = db.session.scalars(db.select(Customer)).all()
    assert customer.address == "8 avenue Neuve" and customer.invoices.count() == 2


def test_a_draft_only_token_cannot_issue(client, issuer, token):
    response = client.post("/api/v1/factures", json=payload(issue=True), headers=auth(token))
    assert response.status_code == 403 and db.session.scalar(db.select(Invoice)) is None


def test_an_issuing_token_issues_and_serves_the_pdf(client, issuer, issuing_token):
    response = client.post("/api/v1/factures", json=payload(issue=True), headers=auth(issuing_token))
    body = response.json
    assert response.status_code == 201 and body["number"].startswith("FA-") and body["state"] == billing.DUE
    pdf = client.get(body["pdf_url"], headers=auth(issuing_token))
    assert pdf.mimetype == "application/pdf" and pdf.data.startswith(b"%PDF")


def test_issue_refusal_creates_nothing(client, issuer, issuing_token):
    data = payload(issue=True)
    data["customer"] = {**data["customer"], "address": None}
    response = client.post("/api/v1/factures", json=data, headers=auth(issuing_token))
    assert response.status_code == 422 and "adresse du client" in response.json["error"]["message"]
    assert db.session.scalar(db.select(Invoice)) is None


@pytest.mark.parametrize(
    "change, message",
    [
        ({"issuer": "inconnu"}, "Émetteur inconnu"),
        ({"lines": []}, "liste non vide"),
        ({"lines": [{"label": "X", "unit_price": 12.5}]}, "centimes"),
        ({"lines": [{"label": "", "unit_price": 100}]}, "obligatoire"),
        ({"lines": [{"label": "X", "unit_price": 100, "quantity": "beaucoup"}]}, "quantity"),
        ({"service_date": "19/11/2026"}, "AAAA-MM-JJ"),
        ({"customer": "Famille"}, "objet"),
        ({"idempotency_key": None}, "idempotency_key"),
    ],
)
def test_invalid_payloads(client, issuer, token, change, message):
    response = client.post("/api/v1/factures", json=payload(**change), headers=auth(token))
    assert response.status_code in (400, 422) and message in response.json["error"]["message"]
    assert db.session.scalar(db.select(Invoice)) is None


def test_list_and_read_invoices_of_a_customer(client, issuer, customer, token):
    client.post("/api/v1/factures", json=payload(), headers=auth(token))
    make_draft(issuer, customer, 1000)  # autre client
    listing = client.get("/api/v1/factures?client=foyer-12", headers=auth(token)).json
    assert listing["total"] == 1 and listing["invoices"][0]["customer"]["external_id"] == "foyer-12"
    one = client.get(f"/api/v1/factures/{listing['invoices'][0]['id']}", headers=auth(token)).json
    assert [line["unit_price"] for line in one["lines"]] == [3000, 21000, -2000]
    assert client.get("/api/v1/factures/999", headers=auth(token)).status_code == 404


def test_draft_has_no_pdf_through_the_api(client, issuer, token):
    body = client.post("/api/v1/factures", json=payload(), headers=auth(token)).json
    assert client.get(f"/api/v1/factures/{body['id']}/pdf", headers=auth(token)).status_code == 409


def test_draft_created_by_the_api_is_issued_by_a_person(client, issuer, token, accounts):
    body = client.post("/api/v1/factures", json=payload(), headers=auth(token)).json
    browser = client.application.test_client()
    login(browser, accounts["gestionnaire"])
    browser.post(f"/factures/{body['id']}/emettre", data={"issue_date": date.today().isoformat()})
    assert db.session.get(Invoice, body["id"]).number is not None


def test_token_screen_shows_the_token_once(client, admin):
    response = client.post("/parametres/api", data={"name": "adherents", "can_issue": "y"})
    html = response.get_data(as_text=True)
    raw = re.search(r"(fct_[\w-]+)", html).group(1)
    assert api_tokens.find(raw).can_issue
    assert raw not in client.get("/parametres/api").get_data(as_text=True)
