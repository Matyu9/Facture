"""Écrans de facturation : émetteurs, clients, articles, brouillons, émission, PDF, envoi, règlements, export."""

import csv
import io
import re
from datetime import date, timedelta

import pytest
from pypdf import PdfReader

from app.extensions import db
from app.models import Activity, Customer, Invoice, Issuer, Payment, Product, Role
from app.services import billing

from .conftest import login, make_draft
from .test_emails import smtp  # noqa: F401  (fixture)


def pdf_text(content):
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


def last(model):
    return db.session.scalar(db.select(model).order_by(model.id.desc()))


ISSUER_FORM = {
    "code": "ecole",
    "name": "École",
    "legal_name": "École de musique du Val",
    "legal_form": "Association loi 1901",
    "address": "1 rue du Port",
    "postcode": "45000",
    "city": "Orléans",
    "country": "France",
    "siret": "123 456 789 00012",
    "vat_regime": "franchise",
    "invoice_prefix": "FA-",
    "credit_prefix": "AV-",
    "number_digits": "4",
    "payment_terms_days": "30",
    "iban": "FR76 3000 6000 0112 3456 7890 189",
}


# ---------------------------------------------------------------------------
# Émetteurs, clients, articles
# ---------------------------------------------------------------------------


def test_admin_creates_an_issuer(client, admin):
    response = client.post("/emetteurs/nouveau", data=ISSUER_FORM)
    issuer = last(Issuer)
    assert response.status_code == 302 and issuer.siret == "12345678900012"
    assert issuer.iban == "FR7630006000011234567890189" and issuer.vat_regime == Issuer.FRANCHISE
    assert "issuer.create" in [a.action for a in db.session.scalars(db.select(Activity))]


@pytest.mark.parametrize(
    "field, value, error",
    [
        ("code", "École", "minuscules"),
        ("siret", "123", "14 chiffres"),
        ("payment_terms_days", "90", "60 jours"),
        ("iban", "pas un iban", "IBAN invalide"),
    ],
)
def test_issuer_validation(client, admin, field, value, error):
    html = client.post("/emetteurs/nouveau", data={**ISSUER_FORM, field: value}).get_data(as_text=True)
    assert error in html and db.session.scalar(db.select(Issuer)) is None


def test_vat_issuer_needs_a_vat_number(client, admin):
    html = client.post("/emetteurs/nouveau", data={**ISSUER_FORM, "vat_regime": "assujetti"}).get_data(as_text=True)
    assert "Obligatoire pour un émetteur assujetti" in html


def test_issuer_code_is_frozen_once_used(client, admin, issued_invoice):
    issuer = issued_invoice.issuer
    data = {**ISSUER_FORM, "code": "autre", "name": issuer.name}
    html = client.post(f"/emetteurs/{issuer.id}/modifier", data=data).get_data(as_text=True)
    assert "ne change plus" in html and db.session.get(Issuer, issuer.id).code == "ecole"


def test_manager_cannot_manage_issuers_but_sees_them(client, manager, issuer):
    assert client.get("/emetteurs").status_code == 200
    assert client.get(f"/emetteurs/{issuer.id}/modifier").status_code == 403


def test_customer_crud_and_delete_rules(client, manager, issuer):
    client.post(
        "/clients/nouveau",
        data={"kind": "structure", "name": "  Comité   des fêtes ", "email": "CF@Test.fr", "city": "Briare"},
    )
    customer = last(Customer)
    assert customer.name == "Comité des fêtes" and customer.email == "cf@test.fr"
    make_draft(issuer, customer, 1000)
    client.post(f"/clients/{customer.id}/supprimer")
    assert db.session.get(Customer, customer.id) is not None  # a une pièce : on archive plutôt
    client.post(f"/clients/{customer.id}/archiver")
    assert db.session.get(Customer, customer.id).archived


def test_customer_search_ignores_accents(client, reader, customer):
    html = client.get("/clients?q=gièn").get_data(as_text=True)
    assert "Mairie de Gien" in html


def test_product_prices_are_parsed_the_french_way(client, manager, issuer):
    client.post(
        "/articles/nouveau",
        data={"label": "Concert", "unit_price": "1 200,50", "unit": "forfait", "issuer_id": str(issuer.id)},
    )
    product = last(Product)
    assert product.unit_price_cents == 120050 and product.issuer_id == issuer.id and product.vat_rate is None
    html = client.post("/articles/nouveau", data={"label": "X", "unit_price": "12,345", "issuer_id": "0"}).get_data(
        as_text=True
    )
    assert "deux chiffres" in html


# ---------------------------------------------------------------------------
# Brouillons
# ---------------------------------------------------------------------------


def lines_form(invoice, *rows, **header):
    data = {
        "issuer_id": str(invoice.issuer_id),
        "customer_id": str(invoice.customer_id),
        "subject": "Concert",
        "service_date": date.today().isoformat(),
        **header,
    }
    form = []
    for key, value in data.items():
        form.append((key, value))
    for row in rows:
        for name in ("product_id", "label", "description", "quantity", "unit", "unit_price", "vat_rate"):
            form.append((name, row.get(name, "")))
    return form


def post_form(client, url, pairs):
    from werkzeug.datastructures import MultiDict

    return client.post(url, data=MultiDict(pairs))


def test_new_invoice_flow(client, manager, issuer, customer):
    response = client.post("/factures/nouvelle", data={"issuer_id": issuer.id, "customer_id": customer.id})
    invoice = last(Invoice)
    assert response.location.endswith(f"/factures/{invoice.id}/modifier") and invoice.is_draft
    form = lines_form(
        invoice,
        {"label": "Concert", "quantity": "1", "unit_price": "800"},
        {"label": "Déplacement", "quantity": "42,5", "unit": "km", "unit_price": "0,6"},
        {"label": "", "unit_price": ""},  # ligne vide ignorée
    )
    response = post_form(client, f"/factures/{invoice.id}/modifier", form)
    invoice = db.session.get(Invoice, invoice.id)
    assert response.status_code == 302
    assert [(row.label, row.total_ht_cents) for row in invoice.lines] == [("Concert", 80000), ("Déplacement", 2550)]
    assert invoice.total_ttc_cents == 82550 and invoice.subject == "Concert"


def test_product_fills_an_empty_line_without_javascript(client, manager, draft):
    product = Product(label="Animation", unit_price_cents=45000, unit="forfait", description="2 heures")
    db.session.add(product)
    db.session.commit()
    post_form(client, f"/factures/{draft.id}/modifier", lines_form(draft, {"product_id": str(product.id)}))
    line = db.session.get(Invoice, draft.id).lines[0]
    assert (line.label, line.unit_price_cents, line.unit, line.description, line.product_id) == (
        "Animation",
        45000,
        "forfait",
        "2 heures",
        product.id,
    )


def test_line_errors_are_shown_and_nothing_is_saved(client, manager, draft):
    html = post_form(
        client, f"/factures/{draft.id}/modifier", lines_form(draft, {"label": "Concert", "unit_price": "douze"})
    ).get_data(as_text=True)
    assert "Ligne 1, prix" in html and 'value="douze"' in html
    assert db.session.get(Invoice, draft.id).total_ttc_cents == 42050


def test_reader_cannot_edit(client, reader, draft):
    assert client.get(f"/factures/{draft.id}/modifier").status_code == 403
    assert client.get(f"/factures/{draft.id}").status_code == 200


def test_an_issued_invoice_cannot_be_edited_or_deleted(client, manager, issued_invoice):
    response = client.get(f"/factures/{issued_invoice.id}/modifier")
    assert response.status_code == 302 and response.location.endswith(f"/factures/{issued_invoice.id}")
    client.post(f"/factures/{issued_invoice.id}/supprimer")
    assert db.session.get(Invoice, issued_invoice.id) is not None


def test_draft_can_be_deleted(client, manager, draft):
    client.post(f"/factures/{draft.id}/supprimer")
    assert db.session.get(Invoice, draft.id) is None


# ---------------------------------------------------------------------------
# Émission, PDF
# ---------------------------------------------------------------------------


def test_issue_from_the_page(client, manager, draft):
    page = client.get(f"/factures/{draft.id}").get_data(as_text=True)
    assert "Émettre la facture" in page
    response = client.post(f"/factures/{draft.id}/emettre", data={"issue_date": date.today().isoformat()})
    invoice = db.session.get(Invoice, draft.id)
    assert response.status_code == 302 and invoice.number == f"FA-{date.today().year}-0001"
    assert invoice.issued_by_id == manager.id


def test_issue_problems_are_listed_before_issuing(client, manager, draft):
    draft.customer.address = None
    db.session.commit()
    page = client.get(f"/factures/{draft.id}").get_data(as_text=True)
    assert "adresse du client est incomplète" in page and "Émettre la facture" not in page
    client.post(f"/factures/{draft.id}/emettre", data={"issue_date": date.today().isoformat()})
    assert db.session.get(Invoice, draft.id).is_draft


def test_reader_cannot_issue(client, reader, draft):
    assert (
        client.post(f"/factures/{draft.id}/emettre", data={"issue_date": date.today().isoformat()}).status_code == 403
    )
    assert db.session.get(Invoice, draft.id).is_draft


def test_draft_pdf_is_marked_as_draft(client, reader, draft):
    response = client.get(f"/factures/{draft.id}/pdf")
    text = pdf_text(response.data)
    assert response.mimetype == "application/pdf" and "BROUILLON" in text and "Brouillon (sans numéro)" in text
    assert response.headers["Cache-Control"] == "private, no-store"


def test_issued_pdf_carries_the_legal_mentions(client, reader, issued_invoice):
    response = client.get(f"/factures/{issued_invoice.id}/pdf?telecharger=1")
    text = pdf_text(response.data)
    assert response.data == (client.application.config["ARCHIVE_DIR"] / issued_invoice.pdf_path).read_bytes()
    assert "attachment" in response.headers["Content-Disposition"]
    for expected in (
        issued_invoice.number,
        "École de musique",
        "SIRET 12345678900012",
        "Mairie de Gien",
        "Place de la Mairie",
        "TVA non applicable, art. 293 B du CGI.",
        "420,50",  # 300 + 120,50 €
        "FR7630006000011234567890189",
        "40 €",  # indemnité forfaitaire, client professionnel
        "DATE DE LA PRESTATION",
        "Échéance",
    ):
        assert expected.replace(" ", " ") in text.replace(" ", " "), expected
    assert "BROUILLON" not in text


def test_vat_invoice_pdf_shows_the_breakdown(client, reader, vat_issuer, customer):
    invoice = make_draft(vat_issuer, customer, 10000)
    billing.issue(invoice)
    db.session.commit()
    text = pdf_text(client.get(f"/factures/{invoice.id}/pdf").data).replace(" ", " ")
    assert "Total HT" in text and "TVA 20 %" in text and "Total TTC" in text and "120,00 €" in text
    assert "FR12345678901" in text


def test_long_invoice_spans_several_pages(client, reader, issuer, customer):
    invoice = make_draft(issuer, customer, *([1000] * 60))
    reader_pdf = PdfReader(io.BytesIO(client.get(f"/factures/{invoice.id}/pdf").data))
    assert len(reader_pdf.pages) >= 2 and "Ligne 60" in reader_pdf.pages[-1].extract_text()


def test_unicode_names_survive_in_the_pdf(client, reader, issuer):
    customer = Customer(
        name="Łucja Brzęczyszczykiewicz – Œuvre",
        address="1 ulica",
        postcode="00-001",
        city="Warszawa",
        country="Pologne",
    )
    db.session.add(customer)
    invoice = make_draft(issuer, customer, 1000)
    text = pdf_text(client.get(f"/factures/{invoice.id}/pdf").data)
    assert "Łucja Brzęczyszczykiewicz – Œuvre" in text and "Pologne" in text


# ---------------------------------------------------------------------------
# Envoi
# ---------------------------------------------------------------------------


def test_send_attaches_the_archived_pdf(client, manager, issued_invoice, smtp):  # noqa: F811
    page = client.get(f"/factures/{issued_invoice.id}/envoyer").get_data(as_text=True)
    assert "culture@gien.test" in page and issued_invoice.number in page
    response = client.post(
        f"/factures/{issued_invoice.id}/envoyer",
        data={"to": "culture@gien.test", "subject": "Votre facture", "body": "Bonjour,\nci-joint."},
    )
    assert response.status_code == 302
    message = smtp.sent[-1]
    (attachment,) = list(message.iter_attachments())
    assert attachment.get_filename() == f"{issued_invoice.number}.pdf"
    assert attachment.get_content() == (client.application.config["ARCHIVE_DIR"] / issued_invoice.pdf_path).read_bytes()
    assert message["Reply-To"] == "tresorier@ecole.test" and "École de musique" in message["From"]
    invoice = db.session.get(Invoice, issued_invoice.id)
    assert invoice.sent_to == "culture@gien.test" and invoice.sent_at


def test_send_without_smtp_says_so(client, manager, issued_invoice):
    response = client.post(
        f"/factures/{issued_invoice.id}/envoyer",
        data={"to": "culture@gien.test", "subject": "Votre facture", "body": "Bonjour"},
        follow_redirects=True,
    )
    assert "pas configuré" in response.get_data(as_text=True)
    assert db.session.get(Invoice, issued_invoice.id).sent_at is None


def test_reminder_mentions_what_remains(client, manager, issued_invoice):
    billing.add_payment(issued_invoice, 10000, date.today(), "cheque")
    db.session.commit()
    page = client.get(f"/factures/{issued_invoice.id}/envoyer?relance=1").get_data(as_text=True)
    assert "Rappel" in page and "320,50" in page.replace(" ", " ")


# ---------------------------------------------------------------------------
# Règlements, avoirs, export
# ---------------------------------------------------------------------------


def test_payment_from_the_page(client, manager, issued_invoice):
    client.post(
        f"/factures/{issued_invoice.id}/reglements",
        data={"amount": "420,50", "paid_on": date.today().isoformat(), "method": "virement", "reference": "VIR 12"},
    )
    invoice = db.session.get(Invoice, issued_invoice.id)
    assert billing.state(invoice) == billing.PAID
    payment = invoice.payments[0]
    client.post(f"/factures/{invoice.id}/reglements/{payment.id}/supprimer")
    assert db.session.get(Payment, payment.id) is None


def test_payment_errors_are_flashed(client, manager, issued_invoice):
    response = client.post(
        f"/factures/{issued_invoice.id}/reglements",
        data={"amount": "999", "paid_on": date.today().isoformat(), "method": "cheque"},
        follow_redirects=True,
    )
    assert "dépasse le reste à payer" in response.get_data(as_text=True)


def test_payment_of_another_invoice_cannot_be_deleted_through_this_one(client, manager, issuer, customer):
    first, second = make_draft(issuer, customer, 1000), make_draft(issuer, customer, 1000)
    billing.issue(first)
    billing.issue(second)
    payment = billing.add_payment(second, 500, date.today(), "cheque")
    db.session.commit()
    assert client.post(f"/factures/{first.id}/reglements/{payment.id}/supprimer").status_code == 404


def test_credit_note_flow(client, manager, issued_invoice):
    response = client.post(f"/factures/{issued_invoice.id}/avoir")
    credit = last(Invoice)
    assert credit.is_credit and response.location.endswith(f"/factures/{credit.id}/modifier")
    client.post(f"/factures/{credit.id}/emettre", data={"issue_date": date.today().isoformat()})
    invoice = db.session.get(Invoice, issued_invoice.id)
    assert billing.state(invoice) == billing.CANCELLED
    text = pdf_text(client.get(f"/factures/{credit.id}/pdf").data)
    assert "AVOIR" in text and issued_invoice.number in text and "FACTURE CORRIGÉE" in text


def test_export_is_a_sales_journal(client, reader, issued_invoice):
    credit = billing.new_credit_note(issued_invoice)
    billing.set_lines(credit, [billing.LineInput(label="Geste", quantity=1, unit_price_cents=5000)])
    billing.issue(credit)
    make_draft(issued_invoice.issuer, issued_invoice.customer, 777)  # brouillon : jamais exporté
    db.session.commit()
    response = client.get("/factures/export.csv")
    rows = list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip("﻿")), delimiter=";"))
    assert rows[0][:3] == ["Date", "Numéro", "Type"]
    assert [(r[1], r[9]) for r in rows[1:]] == [(issued_invoice.number, "420,50"), (credit.number, "-50,00")]
    assert rows[1][12] == "370,50"  # reste à payer, avoir déduit


def test_list_filters_by_state(client, reader, issuer, customer):
    late = make_draft(issuer, customer, 1000)
    billing.issue(late, issue_date=date.today() - timedelta(days=60))
    make_draft(issuer, customer, 2000)
    db.session.commit()
    html = client.get("/factures?etat=en_retard").get_data(as_text=True)
    assert late.number in html and "Brouillon</a>" not in html
    html = client.get("/factures?etat=brouillon").get_data(as_text=True)
    assert late.number not in html and "Brouillon</a>" in html


def test_dashboard_shows_the_year_and_late_invoices(client, manager, issuer, customer):
    late = make_draft(issuer, customer, 1000)
    billing.issue(late, issue_date=date.today() - timedelta(days=60))
    db.session.commit()
    html = client.get("/").get_data(as_text=True)
    assert late.number in html and "En retard" in html and issuer.name in html


def test_dashboard_invites_to_create_an_issuer(client, admin):
    assert "Créer un émetteur" in client.get("/").get_data(as_text=True)


def test_journal_links_to_the_invoice(client, admin, draft):
    client.post(f"/factures/{draft.id}/emettre", data={"issue_date": date.today().isoformat()})
    html = client.get("/journal?type=factures").get_data(as_text=True)
    assert re.search(rf'href="/factures/{draft.id}">Facture FA-\d{{4}}-0001 émis', html)


def test_html_is_escaped_on_the_invoice_page(client, reader, issuer):
    customer = Customer(name="<script>x</script>", address="a", postcode="1", city="c")
    db.session.add(customer)
    invoice = make_draft(issuer, customer, 1000)
    assert "<script>x" not in client.get(f"/factures/{invoice.id}").get_data(as_text=True)


@pytest.mark.parametrize("role", [Role.LECTEUR, Role.GESTIONNAIRE])
def test_no_issuer_management_link_without_the_right(app, accounts, role, issuer):
    client = app.test_client()
    login(client, accounts[role])
    assert "/emetteurs/nouveau" not in client.get("/emetteurs").get_data(as_text=True)
