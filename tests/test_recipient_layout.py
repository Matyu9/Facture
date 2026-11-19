"""Destinataire ponctuel (saisi sur la facture, sans fiche client) et mise en page des factures par émetteur."""

import io
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from app.extensions import db
from app.models import Activity, Customer, Invoice, Issuer
from app.services import billing, pdf

from .conftest import make_draft

ONE_OFF = {
    "recipient_mode": "ponctuel",
    "dest-kind": "particulier",
    "dest-name": "Jeanne Durand",
    "dest-email": "Jeanne@Exemple.test",
    "dest-address": "4 rue des Lilas",
    "dest-postcode": "45100",
    "dest-city": "Orléans",
    "dest-country": "France",
}
RECIPIENT = {"name": "Jeanne Durand", "address": "4 rue des Lilas", "postcode": "45100", "city": "Orléans"}


def pdf_text(content):
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


def last(model):
    return db.session.scalar(db.select(model).order_by(model.id.desc()))


def one_off_draft(issuer, *prices, **recipient):
    invoice = billing.new_draft(issuer, None, recipient={**RECIPIENT, **recipient})
    invoice.service_date = date.today()
    billing.set_lines(
        invoice,
        [billing.LineInput(label="Cours", quantity=Decimal("1"), unit_price_cents=p) for p in prices or (5000,)],
    )
    db.session.commit()
    return invoice


def edit_form(invoice, **overrides):
    data = {
        "issuer_id": invoice.issuer_id,
        "recipient_mode": "client",
        "customer_id": invoice.customer_id or 0,
        "service_date": date.today().isoformat(),
        "label": ["Cours"],
        "quantity": ["1"],
        "unit_price": ["50,00"],
    }
    return {**data, **overrides}


# ---------------------------------------------------------------------------
# Destinataire ponctuel
# ---------------------------------------------------------------------------


def test_new_invoice_for_a_one_off_recipient_creates_no_customer(client, manager, issuer):
    response = client.post("/factures/nouvelle", data={"issuer_id": issuer.id, **ONE_OFF})
    invoice = last(Invoice)
    assert response.status_code == 302 and invoice.customer_id is None and invoice.is_one_off
    assert invoice.recipient["name"] == "Jeanne Durand" and invoice.recipient["email"] == "jeanne@exemple.test"
    assert db.session.scalar(db.select(db.func.count(Customer.id))) == 0


def test_one_off_recipient_can_be_saved_as_customer_at_once(client, manager, issuer):
    client.post("/factures/nouvelle", data={"issuer_id": issuer.id, **ONE_OFF, "save_customer": "y"})
    invoice, customer = last(Invoice), last(Customer)
    assert customer.name == "Jeanne Durand" and customer.kind == Customer.PERSON and customer.city == "Orléans"
    assert invoice.customer_id == customer.id and invoice.customer_snapshot is None
    assert "customer.create" in {a.action for a in db.session.scalars(db.select(Activity))}


def test_one_off_recipient_needs_a_name(client, manager, issuer):
    response = client.post("/factures/nouvelle", data={"issuer_id": issuer.id, **ONE_OFF, "dest-name": ""})
    assert response.status_code == 200 and last(Invoice) is None
    assert 'id="dest-name"' in response.get_data(as_text=True) and 'aria-invalid="true"' in response.get_data(
        as_text=True
    )


def test_registered_client_mode_still_needs_a_client(client, manager, issuer):
    response = client.post("/factures/nouvelle", data={"issuer_id": issuer.id, "customer_id": "0"})
    assert response.status_code == 200 and "Choisissez un client" in response.get_data(as_text=True)
    assert last(Invoice) is None


def test_new_invoice_page_offers_both_modes(client, manager, issuer, customer):
    page = client.get("/factures/nouvelle").get_data(as_text=True)
    assert 'value="ponctuel"' in page and 'name="dest-name"' in page and "Mairie de Gien" in page
    preset = client.get("/factures/nouvelle?destinataire=ponctuel").get_data(as_text=True)
    assert re.search(r'<input checked[^>]*value="ponctuel"', preset)


def test_edit_switches_between_client_and_one_off(client, manager, issuer, customer):
    invoice = make_draft(issuer, customer)
    form = edit_form(invoice, **ONE_OFF)
    assert client.post(f"/factures/{invoice.id}/modifier", data=form).status_code == 302
    invoice = db.session.get(Invoice, invoice.id)
    assert invoice.is_one_off and invoice.recipient_name == "Jeanne Durand"
    page = client.get(f"/factures/{invoice.id}/modifier").get_data(as_text=True)
    assert 'value="Jeanne Durand"' in page and re.search(r'<input checked[^>]*value="ponctuel"', page)
    client.post(f"/factures/{invoice.id}/modifier", data=edit_form(invoice, customer_id=customer.id))
    invoice = db.session.get(Invoice, invoice.id)
    assert invoice.customer_id == customer.id and invoice.customer_snapshot is None


def test_failed_edit_does_not_keep_a_customer_created_on_the_way(client, manager, issuer, customer):
    invoice = make_draft(issuer, customer)
    form = edit_form(invoice, **ONE_OFF, save_customer="y", unit_price=["pas un prix"])
    response = client.post(f"/factures/{invoice.id}/modifier", data=form)
    assert response.status_code == 200 and db.session.scalar(db.select(db.func.count(Customer.id))) == 1


def test_one_off_invoice_is_issued_with_its_recipient(client, manager, issuer):
    invoice = one_off_draft(issuer, 7500)
    assert client.post(f"/factures/{invoice.id}/emettre", data={"issue_date": date.today().isoformat()}).status_code
    invoice = db.session.get(Invoice, invoice.id)
    assert invoice.number and invoice.customer_snapshot["name"] == "Jeanne Durand"
    text = pdf_text(client.get(f"/factures/{invoice.id}/pdf").data)
    assert "Jeanne Durand" in text and "4 rue des Lilas" in text


def test_one_off_recipient_without_address_cannot_be_issued(app, issuer):
    invoice = one_off_draft(issuer, address=None)
    assert any("adresse du client" in p for p in billing.problems(invoice))
    with pytest.raises(billing.BillingError):
        billing.issue(invoice)


def test_organisation_one_off_gets_the_late_penalty_mention(app, issuer):
    invoice = one_off_draft(issuer, kind=Customer.ORGANISATION, name="Comité des fêtes")
    billing.issue(invoice)
    assert invoice.issuer_snapshot["late_penalty"] == billing.DEFAULT_LATE_PENALTY


def test_credit_note_and_copy_keep_the_one_off_recipient(app, issuer):
    invoice = one_off_draft(issuer, 6000)
    billing.issue(invoice)
    db.session.commit()
    credit = billing.new_credit_note(invoice)
    copy = billing.duplicate(invoice)
    db.session.commit()
    assert credit.is_one_off and credit.recipient_name == "Jeanne Durand"
    assert copy.is_one_off and copy.recipient["postcode"] == "45100"
    billing.issue(credit)
    assert credit.customer_snapshot["name"] == "Jeanne Durand"


def test_credit_note_form_keeps_the_one_off_recipient(client, manager, issuer):
    invoice = one_off_draft(issuer, 6000)
    billing.issue(invoice)
    db.session.commit()
    credit = billing.new_credit_note(invoice)
    db.session.commit()
    page = client.get(f"/factures/{credit.id}/modifier").get_data(as_text=True)
    assert "Jeanne Durand" in page and 'name="dest-name"' not in page
    form = edit_form(credit, recipient_mode="ponctuel", customer_id="0", unit_price=["20,00"])
    assert client.post(f"/factures/{credit.id}/modifier", data=form).status_code == 302
    credit = db.session.get(Invoice, credit.id)
    assert credit.is_one_off and credit.recipient_name == "Jeanne Durand" and credit.total_ttc_cents == 2000


def test_one_off_recipient_is_saved_as_customer_later(client, manager, issuer):
    invoice = one_off_draft(issuer, 6000)
    billing.issue(invoice)
    db.session.commit()
    credit = billing.new_credit_note(invoice)
    db.session.commit()
    detail = client.get(f"/factures/{invoice.id}").get_data(as_text=True)
    assert "destinataire ponctuel" in detail and "Enregistrer comme client" in detail
    assert client.post(f"/factures/{invoice.id}/enregistrer-client").status_code == 302
    customer = last(Customer)
    invoice, credit = db.session.get(Invoice, invoice.id), db.session.get(Invoice, credit.id)
    assert customer.name == "Jeanne Durand" and invoice.customer_id == credit.customer_id == customer.id
    assert invoice.customer_snapshot["name"] == "Jeanne Durand"  # la pièce émise garde ses coordonnées
    again = client.post(f"/factures/{invoice.id}/enregistrer-client", follow_redirects=True)
    assert "déjà une fiche client" in again.get_data(as_text=True)


def test_reader_cannot_save_a_one_off_recipient(client, reader, issuer):
    invoice = one_off_draft(issuer)
    assert client.post(f"/factures/{invoice.id}/enregistrer-client").status_code == 403


def test_list_search_and_export_show_one_off_recipients(client, manager, issuer, customer):
    one_off_draft(issuer)
    make_draft(issuer, customer)
    page = client.get("/factures").get_data(as_text=True)
    assert "Jeanne Durand" in page and "Mairie de Gien" in page
    found = client.get("/factures?q=durand").get_data(as_text=True)
    assert "Jeanne Durand" in found and "Mairie de Gien" not in found
    assert client.get("/").status_code == 200


def test_send_form_is_prefilled_with_the_one_off_email(client, manager, issuer):
    invoice = one_off_draft(issuer, email="jeanne@exemple.test")
    billing.issue(invoice)
    db.session.commit()
    assert 'value="jeanne@exemple.test"' in client.get(f"/factures/{invoice.id}/envoyer").get_data(as_text=True)


def test_api_shows_a_one_off_recipient_without_id(app, issuer):
    from app.services import api_tokens

    invoice = one_off_draft(issuer)
    _, raw = api_tokens.create("adherents", False)
    db.session.commit()
    body = app.test_client().get(f"/api/v1/factures/{invoice.id}", headers={"Authorization": f"Bearer {raw}"}).json
    assert body["customer"]["id"] is None and body["customer"]["name"] == "Jeanne Durand"


# ---------------------------------------------------------------------------
# Mise en page
# ---------------------------------------------------------------------------

LAYOUT = {
    "pdf_layout": "bandeau",
    "pdf_color": "#0F6B72",
    "logo_size": "grand",
    "logo_position": "droite",
    "recipient_label": "Destinataire",
    "designation_label": "Prestation",
    "intro_text": "Suite à notre concert, voici le détail.",
    "closing_text": "Merci de votre confiance.",
}


def logo_png(app, size=(600, 200)):
    folder = Path(app.config["UPLOAD_DIR"]) / "logo"
    folder.mkdir(parents=True, exist_ok=True)
    name = "b" * 32 + ".png"
    Image.new("RGBA", size, (200, 30, 30, 255)).save(folder / name)
    return f"logo/{name}"


def images_in(content):
    return sum(len(page.images) for page in PdfReader(io.BytesIO(content)).pages)


def test_layout_page_saves_the_settings(client, admin, issuer):
    assert client.get(f"/emetteurs/{issuer.id}/mise-en-page").status_code == 200
    response = client.post(f"/emetteurs/{issuer.id}/mise-en-page", data=LAYOUT)
    issuer = db.session.get(Issuer, issuer.id)
    assert response.status_code == 302 and issuer.pdf_layout == "bandeau" and issuer.pdf_color == "#0f6b72"
    assert issuer.intro_text == LAYOUT["intro_text"] and issuer.designation_label == "Prestation"
    assert "issuer.layout" in {a.action for a in db.session.scalars(db.select(Activity))}


def test_app_color_box_clears_the_own_color(client, admin, issuer):
    issuer.pdf_color = "#123456"
    db.session.commit()
    client.post(f"/emetteurs/{issuer.id}/mise-en-page", data={**LAYOUT, "app_color": "y"})
    assert db.session.get(Issuer, issuer.id).pdf_color is None


@pytest.mark.parametrize("field, value", [("pdf_color", "rouge"), ("pdf_layout", "baroque"), ("logo_size", "xxl")])
def test_layout_refuses_bad_values(client, admin, issuer, field, value):
    response = client.post(f"/emetteurs/{issuer.id}/mise-en-page", data={**LAYOUT, field: value})
    assert response.status_code == 200 and db.session.get(Issuer, issuer.id).pdf_layout == "classique"


def test_manager_cannot_change_the_layout(client, manager, issuer):
    assert client.post(f"/emetteurs/{issuer.id}/mise-en-page", data=LAYOUT).status_code == 403
    assert client.post(f"/emetteurs/{issuer.id}/apercu.pdf", data=LAYOUT).status_code == 403
    assert client.get(f"/emetteurs/{issuer.id}/apercu.pdf").status_code == 200


def test_preview_uses_the_unsaved_form(client, admin, issuer):
    response = client.post(f"/emetteurs/{issuer.id}/apercu.pdf", data=LAYOUT)
    text = pdf_text(response.data)
    assert response.mimetype == "application/pdf" and "APERÇU" in text
    assert "Suite à notre concert" in text and "PRESTATION" in text and "DESTINATAIRE" in text
    assert db.session.get(Issuer, issuer.id).intro_text is None  # rien d'enregistré
    assert db.session.scalar(db.select(db.func.count(Invoice.id))) == 0
    assert client.post(f"/emetteurs/{issuer.id}/apercu.pdf", data={**LAYOUT, "pdf_color": "x"}).status_code == 400


@pytest.mark.parametrize("layout", list(Issuer.LAYOUTS))
@pytest.mark.parametrize("position", list(Issuer.LOGO_POSITIONS))
@pytest.mark.parametrize("vat", [False, True])
def test_every_layout_renders_with_its_logo(app, issuer, layout, position, vat):
    issuer.logo = logo_png(app)
    issuer.pdf_layout, issuer.logo_position = layout, position
    if vat:
        issuer.vat_regime, issuer.vat_number = Issuer.SUBJECT, "FR12345678901"
    db.session.commit()
    content = pdf.preview(issuer)
    assert images_in(content) == 1 and "Concert" in pdf_text(content)


def test_no_logo_size_hides_the_logo(app, issuer):
    issuer.logo, issuer.logo_size = logo_png(app), "aucun"
    db.session.commit()
    assert images_in(pdf.preview(issuer)) == 0


def test_very_wide_logo_is_kept_within_bounds(app, tmp_path):
    path = tmp_path / "large.png"
    Image.new("RGB", (4000, 100)).save(path)
    width, height = pdf._logo_box(path, 26)
    assert width == pdf.LOGO_MAX_WIDTH and height == pytest.approx(2)


def test_text_on_the_band_stays_readable():
    assert pdf._readable_on((15, 107, 114)) == pdf.WHITE
    assert pdf._readable_on((250, 220, 90)) == pdf.INK


def test_issued_invoice_keeps_its_layout(app, issuer, customer):
    issuer.intro_text, issuer.pdf_layout = "Texte d'origine.", "sobre"
    invoice = make_draft(issuer, customer)
    billing.issue(invoice)
    db.session.commit()
    issuer.intro_text, issuer.pdf_layout = "Nouveau texte.", "bandeau"
    db.session.commit()
    assert invoice.issuer_snapshot["intro_text"] == "Texte d'origine."
    assert invoice.issuer_snapshot["pdf_layout"] == "sobre"
    assert "Texte d'origine." in pdf_text(pdf.render(invoice))


def test_old_snapshot_without_layout_still_renders(app, issuer, customer):
    invoice = make_draft(issuer, customer)
    billing.issue(invoice)
    for key in ("pdf_layout", "pdf_color", "logo_size", "logo_position", "intro_text", "closing_text"):
        invoice.issuer_snapshot.pop(key)
    assert "FACTURE" in pdf_text(pdf.render(invoice))


def test_issuer_logo_is_kept_sharp_enough_to_print(client, admin, issuer):
    image = io.BytesIO()
    Image.new("RGB", (2400, 800), (10, 10, 10)).save(image, "PNG")
    image.seek(0)
    client.post(f"/emetteurs/{issuer.id}/logo", data={"logo": (image, "logo.png")}, content_type="multipart/form-data")
    issuer = db.session.get(Issuer, issuer.id)
    with Image.open(Path(client.application.config["UPLOAD_DIR"]) / issuer.logo) as saved:
        assert saved.size == (1200, 400)


def test_issuer_page_links_to_the_layout(client, admin, issuer):
    page = client.get(f"/emetteurs/{issuer.id}").get_data(as_text=True)
    assert f"/emetteurs/{issuer.id}/mise-en-page" in page and "Classique" in page
