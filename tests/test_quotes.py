"""Devis (numérotation, décision, passage en facture, PDF, écrans) et API d'émission : émettre, envoyer, devis."""

import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pypdf import PdfReader

from app.extensions import db
from app.models import Activity, Customer, Invoice, Issuer
from app.services import api_tokens, billing, pdf

from .conftest import make_draft
from .test_emails import smtp  # noqa: F401  (fixture)


def pdf_text(content):
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


def last(model):
    return db.session.scalar(db.select(model).order_by(model.id.desc()))


def make_quote(issuer, customer, *prices, issue=False):
    quote = billing.new_draft(issuer, customer, kind=Invoice.QUOTE)
    quote.subject = "Concert de printemps"
    billing.set_lines(
        quote,
        [
            billing.LineInput(label=f"Ligne {n}", quantity=Decimal("1"), unit_price_cents=p)
            for n, p in enumerate(prices or (50000,), start=1)
        ],
    )
    if issue:
        billing.issue(quote)
    db.session.commit()
    return quote


# ---------------------------------------------------------------------------
# Métier
# ---------------------------------------------------------------------------


def test_quote_has_its_own_numbering_and_validity(app, issuer, customer):
    invoice = make_draft(issuer, customer)
    billing.issue(invoice)
    quote = make_quote(issuer, customer, issue=True)
    year = date.today().year
    assert invoice.number == f"FA-{year}-0001" and quote.number == f"DE-{year}-0001"
    assert quote.valid_until == date.today() + timedelta(days=30) and quote.due_date is None
    assert quote.service_date is None  # pas obligatoire sur un devis
    assert billing.state(quote) == billing.PENDING and quote.balance_cents == 0


def test_quote_is_not_an_accounting_piece(app, issuer, customer):
    quote = make_quote(issuer, customer, issue=True)
    with pytest.raises(billing.BillingError):
        billing.add_payment(quote, 1000, date.today(), "cheque")
    with pytest.raises(billing.BillingError):
        billing.new_credit_note(quote)


def test_quote_decisions(app, issuer, customer):
    quote = make_quote(issuer, customer, issue=True)
    billing.decide(quote, Invoice.REFUSED)
    assert billing.state(quote) == billing.REFUSED and quote.decided_at
    with pytest.raises(billing.BillingError, match="refusé"):
        billing.invoice_from_quote(quote)
    billing.decide(quote, None)
    assert billing.state(quote) == billing.PENDING and quote.decided_at is None


def test_draft_quote_cannot_be_decided(app, issuer, customer):
    with pytest.raises(billing.BillingError):
        billing.decide(make_quote(issuer, customer), Invoice.ACCEPTED)


def test_expired_quote(app, issuer, customer):
    quote = make_quote(issuer, customer, issue=True)
    quote.valid_until = date.today() - timedelta(days=1)
    assert billing.state(quote) == billing.EXPIRED


def test_invoice_from_quote_copies_it_and_accepts_it(app, issuer, customer):
    quote = make_quote(issuer, customer, 40000, 2500, issue=True)
    invoice = billing.invoice_from_quote(quote)
    db.session.commit()
    assert invoice.kind == Invoice.INVOICE and invoice.is_draft and invoice.quote is quote
    assert invoice.customer is customer and invoice.subject == quote.subject and invoice.total_ttc_cents == 42500
    assert quote.decision == Invoice.ACCEPTED and billing.state(quote) == billing.INVOICED
    with pytest.raises(billing.BillingError, match="déjà sa facture"):
        billing.invoice_from_quote(quote)
    with pytest.raises(billing.BillingError, match="déjà été facturé"):
        billing.decide(quote, Invoice.REFUSED)
    invoice.service_date = date.today()
    billing.issue(invoice)
    assert "SELON DEVIS" in pdf_text(pdf.render(invoice)) and quote.number in pdf_text(pdf.render(invoice))


def test_invoice_from_quote_keeps_a_one_off_recipient(app, issuer):
    quote = billing.new_draft(
        issuer, None, kind=Invoice.QUOTE, recipient={"name": "Jeanne", "address": "1 rue", "postcode": "1", "city": "X"}
    )
    billing.set_lines(quote, [billing.LineInput(label="Cours", quantity=Decimal("1"), unit_price_cents=5000)])
    billing.issue(quote)
    invoice = billing.invoice_from_quote(quote)
    assert invoice.is_one_off and invoice.recipient_name == "Jeanne"


def test_quote_vat_rates_follow_to_the_invoice(app, vat_issuer, customer):
    quote = billing.new_draft(vat_issuer, customer, kind=Invoice.QUOTE)
    billing.set_lines(
        quote, [billing.LineInput(label="Livre", quantity=Decimal("2"), unit_price_cents=1000, vat_rate=Decimal("5.5"))]
    )
    billing.issue(quote)
    invoice = billing.invoice_from_quote(quote)
    assert invoice.lines[0].vat_rate == Decimal("5.5") and invoice.total_ttc_cents == quote.total_ttc_cents == 2110


def test_duplicate_quote_stays_a_quote(app, issuer, customer):
    copy = billing.duplicate(make_quote(issuer, customer, issue=True))
    assert copy.is_quote and copy.is_draft


def test_quote_pdf(app, issuer, customer):
    quote = make_quote(issuer, customer, issue=True)
    text = pdf_text(pdf.render(quote))
    assert "DEVIS" in text and quote.number in text and "Valable jusqu'au" in text
    assert "Bon pour accord" in text and "IBAN" not in text and "Retard de paiement".upper() not in text


def test_quote_validity_cannot_precede_its_date(app, issuer, customer):
    quote = make_quote(issuer, customer)
    quote.valid_until = date.today() - timedelta(days=2)
    assert any("validité" in p for p in billing.problems(quote))


def test_quotes_stay_out_of_the_sales_journal_and_totals(client, manager, issuer, customer):
    make_quote(issuer, customer, 99900, issue=True)
    invoice = make_draft(issuer, customer, 10000)
    billing.issue(invoice)
    db.session.commit()
    export = client.get("/factures/export.csv").get_data(as_text=True)
    assert "DE-" not in export and invoice.number in export
    listing = client.get("/factures").get_data(as_text=True)
    assert "999,00" not in listing and "DE-" not in listing
    assert "999,00" not in client.get("/").get_data(as_text=True).split("Devis en attente")[0]


# ---------------------------------------------------------------------------
# Écrans
# ---------------------------------------------------------------------------


def test_quote_screens_from_creation_to_invoice(client, manager, issuer, customer):
    assert client.get("/devis").status_code == 200
    page = client.get("/devis/nouveau").get_data(as_text=True)
    assert "Nouveau devis" in page
    response = client.post("/devis/nouveau", data={"issuer_id": issuer.id, "customer_id": customer.id})
    quote = last(Invoice)
    assert response.status_code == 302 and quote.is_quote and quote.valid_until
    edit = client.get(f"/factures/{quote.id}/modifier").get_data(as_text=True)
    assert "Devis en brouillon" in edit and 'name="valid_until"' in edit and 'name="due_date"' not in edit
    form = {
        "issuer_id": issuer.id,
        "recipient_mode": "client",
        "customer_id": customer.id,
        "valid_until": (date.today() + timedelta(days=15)).isoformat(),
        "label": ["Concert"],
        "quantity": ["1"],
        "unit_price": ["800,00"],
    }
    assert client.post(f"/factures/{quote.id}/modifier", data=form).status_code == 302
    client.post(f"/factures/{quote.id}/emettre", data={"issue_date": date.today().isoformat()})
    quote = db.session.get(Invoice, quote.id)
    assert quote.number.startswith("DE-") and quote.valid_until == date.today() + timedelta(days=15)
    detail = client.get(f"/factures/{quote.id}").get_data(as_text=True)
    assert "Transformer en facture" in detail and "Règlements" not in detail and "Faire un avoir" not in detail
    assert quote.number in client.get("/devis").get_data(as_text=True)
    assert quote.number in client.get("/").get_data(as_text=True)  # devis en attente sur l'accueil
    response = client.post(f"/devis/{quote.id}/facturer")
    invoice = last(Invoice)
    assert response.location.endswith(f"/factures/{invoice.id}/modifier") and invoice.quote_id == quote.id
    assert "tirée du devis" in client.get(response.location).get_data(as_text=True)
    assert "Facturé" in client.get(f"/factures/{quote.id}").get_data(as_text=True)


def test_quote_decision_screens(client, manager, issuer, customer):
    quote = make_quote(issuer, customer, issue=True)
    client.post(f"/devis/{quote.id}/refuser")
    assert db.session.get(Invoice, quote.id).decision == Invoice.REFUSED
    assert "Transformer en facture" not in client.get(f"/factures/{quote.id}").get_data(as_text=True)
    client.post(f"/devis/{quote.id}/attente")
    client.post(f"/devis/{quote.id}/accepter")
    assert db.session.get(Invoice, quote.id).decision == Invoice.ACCEPTED
    assert "quote.decision" in {a.action for a in db.session.scalars(db.select(Activity))}
    assert client.get("/devis?etat=accepte").get_data(as_text=True).count(quote.number) >= 1
    assert quote.number not in client.get("/devis?etat=refuse").get_data(as_text=True)


def test_invoice_routes_do_not_take_a_quote_for_an_invoice(client, manager, issuer, customer):
    invoice = make_draft(issuer, customer)
    billing.issue(invoice)
    db.session.commit()
    assert client.post(f"/devis/{invoice.id}/accepter").status_code == 404
    assert client.post(f"/devis/{invoice.id}/facturer").status_code == 404


def test_reader_cannot_decide(client, reader, issuer, customer):
    quote = make_quote(issuer, customer, issue=True)
    assert client.post(f"/devis/{quote.id}/accepter").status_code == 403
    assert client.get("/devis").status_code == 200


def test_quote_email_text(client, manager, issuer, customer, smtp):  # noqa: F811
    quote = make_quote(issuer, customer, issue=True)
    page = client.get(f"/factures/{quote.id}/envoyer").get_data(as_text=True)
    assert "notre devis" in page and "valable jusqu" in page and "Relancer" not in page


# ---------------------------------------------------------------------------
# Logo : un seul formulaire avec la mise en page
# ---------------------------------------------------------------------------


def test_changing_the_logo_keeps_the_other_settings(client, admin, issuer):
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (300, 100)).save(image, "PNG")
    image.seek(0)
    form = {
        "pdf_layout": "bandeau",
        "pdf_color": "#123456",
        "logo_size": "grand",
        "logo_position": "droite",
        "intro_text": "Bonjour",
        "logo": (image, "logo.png"),
    }
    client.post(f"/emetteurs/{issuer.id}/mise-en-page", data=form, content_type="multipart/form-data")
    issuer = db.session.get(Issuer, issuer.id)
    assert issuer.logo and issuer.pdf_layout == "bandeau" and issuer.intro_text == "Bonjour"
    client.post(f"/emetteurs/{issuer.id}/mise-en-page", data={**form, "logo": None, "remove_logo": "y"})
    issuer = db.session.get(Issuer, issuer.id)
    assert issuer.logo is None and issuer.pdf_layout == "bandeau" and issuer.pdf_color == "#123456"


def test_bad_logo_keeps_the_form(client, admin, issuer):
    form = {"pdf_layout": "sobre", "logo_size": "moyen", "logo_position": "gauche", "intro_text": "Texte en cours"}
    response = client.post(
        f"/emetteurs/{issuer.id}/mise-en-page",
        data={**form, "logo": (io.BytesIO(b"pas une image"), "x.png")},
        content_type="multipart/form-data",
    )
    page = response.get_data(as_text=True)
    assert response.status_code == 200 and "Texte en cours" in page and 'aria-invalid="true"' in page
    assert db.session.get(Issuer, issuer.id).pdf_layout == "classique"


def test_preview_with_a_new_logo_saves_nothing(client, admin, issuer, app):
    from pathlib import Path

    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (300, 100)).save(image, "PNG")
    image.seek(0)
    form = {"pdf_layout": "classique", "logo_size": "moyen", "logo_position": "gauche", "logo": (image, "l.png")}
    response = client.post(f"/emetteurs/{issuer.id}/apercu.pdf", data=form, content_type="multipart/form-data")
    assert response.mimetype == "application/pdf"
    assert sum(len(p.images) for p in PdfReader(io.BytesIO(response.data)).pages) == 1
    folder = Path(app.config["UPLOAD_DIR"]) / "logo"
    assert not list(folder.glob("*.png")) and db.session.get(Issuer, issuer.id).logo is None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def raw(app):
    _, raw = api_tokens.create("adherents", can_issue=True)
    db.session.commit()
    return raw


@pytest.fixture
def draft_only(app):
    _, raw = api_tokens.create("lecture")
    db.session.commit()
    return raw


def auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def body(**overrides):
    return {
        "idempotency_key": "k-1",
        "issuer": "ecole",
        "customer": {"name": "Jeanne Durand", "address": "4 rue des Lilas", "postcode": "45100", "city": "Orléans"},
        "lines": [{"label": "Cours", "unit_price": 5000}],
        **overrides,
    }


def test_api_creates_an_invoice_for_a_one_off_recipient(client, issuer, raw):
    response = client.post("/api/v1/factures", json=body(issue=True), headers=auth(raw))
    invoice = last(Invoice)
    assert response.status_code == 201 and invoice.is_one_off and invoice.number
    assert response.json["customer"]["id"] is None and response.json["customer"]["name"] == "Jeanne Durand"
    assert db.session.scalar(db.select(db.func.count(Customer.id))) == 0


def test_api_uses_an_existing_customer_by_id(client, issuer, customer, raw):
    response = client.post("/api/v1/factures", json=body(customer={"id": customer.id}), headers=auth(raw))
    assert response.status_code == 201 and response.json["customer"]["id"] == customer.id
    bad = client.post("/api/v1/factures", json=body(idempotency_key="k-2", customer={"id": 999}), headers=auth(raw))
    assert bad.status_code == 422


def test_api_one_off_recipient_needs_a_name(client, issuer, raw):
    response = client.post("/api/v1/factures", json=body(customer={"city": "Orléans"}), headers=auth(raw))
    assert response.status_code == 422 and last(Invoice) is None


def test_api_issues_a_draft_once(client, issuer, raw):
    created = client.post("/api/v1/factures", json=body(), headers=auth(raw)).json
    first = client.post(f"/api/v1/factures/{created['id']}/emettre", headers=auth(raw))
    again = client.post(f"/api/v1/factures/{created['id']}/emettre", headers=auth(raw))
    assert first.status_code == again.status_code == 200 and first.json["number"] == again.json["number"]
    assert client.get(first.json["pdf_url"], headers=auth(raw)).mimetype == "application/pdf"


def test_api_issue_reports_what_is_missing(client, issuer, raw):
    created = client.post("/api/v1/factures", json=body(customer={"name": "Sans adresse"}), headers=auth(raw)).json
    response = client.post(f"/api/v1/factures/{created['id']}/emettre", headers=auth(raw))
    assert response.status_code == 422 and "adresse" in response.json["error"]["message"]


def test_draft_only_token_cannot_act(client, issuer, draft_only):
    created = client.post("/api/v1/devis", json=body(), headers=auth(draft_only))
    assert created.status_code == 201
    for action in ("emettre", "envoyer", "accepter", "facturer"):
        response = client.post(f"/api/v1/devis/{created.json['id']}/{action}", headers=auth(draft_only))
        assert response.status_code == 403, action


def test_api_quote_lifecycle(client, issuer, raw):
    created = client.post(
        "/api/v1/devis",
        json=body(issue=True, valid_until=(date.today() + timedelta(days=10)).isoformat()),
        headers=auth(raw),
    )
    quote = created.json
    assert created.status_code == 201 and quote["kind"] == "devis" and quote["number"].startswith("DE-")
    assert quote["state"] == "en_attente" and quote["pdf_url"] == f"/api/v1/devis/{quote['id']}/pdf"
    assert client.get(f"/api/v1/factures/{quote['id']}", headers=auth(raw)).status_code == 404
    assert [q["id"] for q in client.get("/api/v1/devis", headers=auth(raw)).json["invoices"]] == [quote["id"]]
    assert client.get("/api/v1/factures", headers=auth(raw)).json["total"] == 0
    accepted = client.post(f"/api/v1/devis/{quote['id']}/accepter", headers=auth(raw))
    assert accepted.json["decision"] == "accepte"
    invoice = client.post(f"/api/v1/devis/{quote['id']}/facturer", json={"issue": True}, headers=auth(raw))
    assert invoice.status_code == 201 and invoice.json["kind"] == "facture" and invoice.json["number"]
    assert invoice.json["quote"]["number"] == quote["number"]
    again = client.post(f"/api/v1/devis/{quote['id']}/facturer", headers=auth(raw))
    assert again.status_code == 200 and again.json["id"] == invoice.json["id"]
    refused = client.post(f"/api/v1/devis/{quote['id']}/refuser", headers=auth(raw))
    assert refused.status_code == 409


def test_api_idempotency_key_is_per_kind(client, issuer, raw):
    client.post("/api/v1/devis", json=body(), headers=auth(raw))
    assert client.post("/api/v1/factures", json=body(), headers=auth(raw)).status_code == 409


def test_api_sends_by_email(client, issuer, raw, smtp):  # noqa: F811
    created = client.post(
        "/api/v1/factures",
        json=body(issue=True, customer={**body()["customer"], "email": "jeanne@exemple.test"}),
        headers=auth(raw),
    ).json
    response = client.post(f"/api/v1/factures/{created['id']}/envoyer", headers=auth(raw))
    assert response.status_code == 200 and response.json["sent_at"]
    assert smtp.sent and "jeanne@exemple.test" in str(smtp.sent[-1])


def test_api_send_needs_an_address_and_an_issued_piece(client, issuer, raw, smtp):  # noqa: F811
    created = client.post("/api/v1/factures", json=body(), headers=auth(raw)).json
    assert client.post(f"/api/v1/factures/{created['id']}/envoyer", headers=auth(raw)).status_code == 422
    drafted = client.post(f"/api/v1/factures/{created['id']}/envoyer", json={"to": "a@b.test"}, headers=auth(raw))
    assert drafted.status_code == 409
