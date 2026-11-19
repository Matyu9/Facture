"""Numérotation réglable : formats, compteur en chiffres ou en lettres, remise à zéro, numéro de départ, écrans."""

from datetime import date

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import Activity, Invoice, Issuer, NumberSequence
from app.services import billing, numbering

from .conftest import make_draft

TODAY = date.today()
LAST_YEAR = date(TODAY.year - 1, 12, 31)


def issue(issuer, customer, day=None):
    invoice = make_draft(issuer, customer)
    billing.issue(invoice, issue_date=day)
    db.session.commit()
    return invoice


def page_form(**overrides):
    data = {
        "invoice_format": "FA-{AAAA}-{NNNN}",
        "invoice_next": "1",
        "credit_format": "AV-{AAAA}-{NNNN}",
        "credit_next": "1",
        "quote_format": "DE-{AAAA}-{NNNN}",
        "quote_next": "1",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "n", "expected", "reset"),
    [
        ("FA-{AAAA}-{NNNN}", 7, "FA-2026-0007", numbering.YEARLY),
        ("F{AA}{MM}-{NNN}", 12, "F2611-012", numbering.MONTHLY),
        ("FACT-{NNNNNN}", 1, "FACT-000001", numbering.NEVER),
        ("{N}", 1234, "1234", numbering.NEVER),
        ("{AAAA}-{L}", 1, "2026-A", numbering.YEARLY),
        ("{aaaa}/{mm}/{jj}-{n}", 3, "2026/11/19-3", numbering.MONTHLY),
        ("R{MM}-{NN}", 5, "R11-05", numbering.NEVER),  # le mois sans l'année ne remet pas à zéro
        ("FA.{AA}.{NNNN}", 10_000, "FA.26.10000", numbering.YEARLY),  # le compteur déborde sans se tronquer
    ],
)
def test_formats(text, n, expected, reset):
    fmt = numbering.parse(text)
    assert fmt.render(date(2026, 11, 19), n) == expected
    assert fmt.reset == reset


@pytest.mark.parametrize(
    ("n", "expected"), [(1, "A"), (2, "B"), (26, "Z"), (27, "AA"), (52, "AZ"), (53, "BA"), (702, "ZZ"), (703, "AAA")]
)
def test_letter_counter(n, expected):
    assert numbering.letters(n) == expected


def test_periods():
    day = date(2026, 3, 4)
    assert numbering.parse("FA-{AAAA}-{N}").period(day) == "2026"
    assert numbering.parse("FA-{AA}{MM}-{N}").period(day) == "2026-03"
    assert numbering.parse("FA-{N}").period(day) == ""


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "Indiquez"),
        ("FA-{AAAA}", "un compteur"),
        ("{N}-{L}", "un seul"),
        ("FA-{ANNEE}-{N}", "Balise inconnue"),
        ("FA {N}", "En dehors des balises"),
        ("Fé-{N}", "En dehors des balises"),
        ("FA-{N", "Accolade"),
        ("FA-" + "X" * 40 + "{N}", "40 caractères"),
        ("{NNNNNNNNN}", "Balise inconnue"),  # 9 chiffres : au-delà de 8
    ],
)
def test_refused_formats(text, message):
    with pytest.raises(numbering.FormatError, match=message):
        numbering.example(text)


def test_too_long_numbers_are_refused():
    with pytest.raises(numbering.FormatError, match="trop longs"):
        numbering.example("X" * 37 + "{N}")


def test_signature_ignores_widths_but_not_counter_style():
    assert numbering.parse("FA-{AAAA}-{NNN}").signature == numbering.parse("FA-{AAAA}-{NNNNNN}").signature
    assert numbering.parse("FA-{AAAA}-{N}").signature != numbering.parse("FA-{AAAA}-{L}").signature
    assert numbering.parse("FA-{AAAA}-{N}").signature != numbering.parse("FA-{AA}-{N}").signature


# ---------------------------------------------------------------------------
# Émission
# ---------------------------------------------------------------------------


def test_default_numbering_is_unchanged(app, issuer, customer):
    assert issue(issuer, customer).number == f"FA-{TODAY.year}-0001"


def test_custom_format_with_letters(app, issuer, customer):
    issuer.invoice_format = "F{AA}-{L}"
    db.session.commit()
    numbers = [issue(issuer, customer).number for _ in range(3)]
    yy = f"{TODAY.year % 100:02d}"
    assert numbers == [f"F{yy}-A", f"F{yy}-B", f"F{yy}-C"]


def test_yearly_series_restarts_and_continuous_series_does_not(app, issuer, customer):
    issuer.invoice_format = "FA-{AAAA}-{NNN}"
    issuer.credit_format = "AV-{NNNNN}"
    db.session.commit()
    first = issue(issuer, customer, LAST_YEAR)
    second = issue(issuer, customer)
    assert first.number == f"FA-{LAST_YEAR.year}-001" and second.number == f"FA-{TODAY.year}-001"
    credits = []
    for invoice in (first, second):
        credit = billing.new_credit_note(invoice)
        billing.issue(credit, issue_date=invoice.issue_date)
        db.session.commit()
        credits.append(credit.number)
    assert credits == ["AV-00001", "AV-00002"]
    periods = db.session.scalars(
        db.select(NumberSequence.period).where(NumberSequence.issuer_id == issuer.id).order_by(NumberSequence.period)
    ).all()
    assert periods == ["", str(LAST_YEAR.year), str(TODAY.year)]


def test_monthly_series(app, issuer, customer):
    issuer.invoice_format = "FA{AAAA}{MM}-{NN}"
    db.session.commit()
    previous = issue(issuer, customer, LAST_YEAR)
    current = issue(issuer, customer)
    assert previous.number == f"FA{LAST_YEAR.year}12-01"
    assert current.number == f"FA{TODAY.year}{TODAY.month:02d}-01"


def test_cosmetic_change_keeps_the_series_going(app, issuer, customer):
    issue(issuer, customer)
    issuer.invoice_format = "FA-{AAAA}-{NNNNNN}"
    db.session.commit()
    assert issue(issuer, customer).number == f"FA-{TODAY.year}-000002"


def test_a_number_is_never_given_twice(app, issuer, customer):
    """Changer de format peut faire retomber sur un numéro déjà donné : l'émission est refusée, rien n'est pris."""
    issuer.invoice_format = "FA-{N}"
    db.session.commit()
    issue(issuer, customer)  # FA-1
    issuer.invoice_format = "FA-{N}"
    db.session.execute(sa.update(NumberSequence).where(NumberSequence.issuer_id == issuer.id).values(last_number=0))
    db.session.commit()
    draft = make_draft(issuer, customer)
    with pytest.raises(billing.BillingError, match="FA-1 est déjà pris"):
        billing.issue(draft)
    db.session.rollback()
    assert db.session.get(Invoice, draft.id).is_draft


def test_start_number(app, issuer, customer):
    billing.set_next_number(issuer, Invoice.INVOICE, 143)
    db.session.commit()
    assert billing.next_number(issuer, Invoice.INVOICE) == 143
    assert issue(issuer, customer).number == f"FA-{TODAY.year}-0143"
    with pytest.raises(billing.BillingError):
        billing.set_next_number(issuer, Invoice.INVOICE, 143)
    db.session.rollback()
    assert billing.lowest_next_number(issuer, Invoice.INVOICE) == 144
    # Une erreur de frappe (trop haut) se corrige tant que rien n'a été émis au-delà.
    billing.set_next_number(issuer, Invoice.INVOICE, 9000)
    billing.set_next_number(issuer, Invoice.INVOICE, 144)
    db.session.commit()
    assert issue(issuer, customer).number == f"FA-{TODAY.year}-0144"


def test_last_year_pieces_do_not_block_this_year_start(app, issuer, customer):
    issue(issuer, customer, LAST_YEAR)
    billing.set_next_number(issuer, Invoice.INVOICE, 1)  # nouvelle année : on peut repartir de 1
    issuer.invoice_format = "FA-{N}"  # série continue : les pièces de l'an dernier comptent
    assert billing.lowest_next_number(issuer, Invoice.INVOICE) == 2


# ---------------------------------------------------------------------------
# Écrans
# ---------------------------------------------------------------------------


def test_numbering_page(client, admin, issuer, customer):
    issue(issuer, customer)
    page = client.get(f"/emetteurs/{issuer.id}/numerotation").get_data(as_text=True)
    assert "FA-{AAAA}-{NNNN}" in page and f"FA-{TODAY.year}-0002" in page and "Des pièces sont déjà émises" in page
    detail = client.get(f"/emetteurs/{issuer.id}").get_data(as_text=True)
    assert f"FA-{TODAY.year}-0002" in detail and "Modifier la numérotation" in detail


def test_save_numbering(client, admin, issuer, customer):
    response = client.post(
        f"/emetteurs/{issuer.id}/numerotation",
        data=page_form(invoice_format="F{AA}-{NNN}", invoice_next="50", quote_format="DEVIS-{L}"),
    )
    assert response.status_code == 302
    db.session.expire_all()
    assert issuer.invoice_format == "F{AA}-{NNN}" and issuer.quote_format == "DEVIS-{L}"
    assert issue(issuer, customer).number == f"F{TODAY.year % 100:02d}-050"
    entry = db.session.scalar(db.select(Activity).where(Activity.action == "issuer.numbering"))
    assert entry is not None and "invoice_next" in entry.changes


def test_untouched_next_number_keeps_the_series(client, admin, issuer, customer):
    issue(issuer, customer)
    response = client.post(f"/emetteurs/{issuer.id}/numerotation", data=page_form(invoice_next="2"))
    assert response.status_code == 302
    assert issue(issuer, customer).number.endswith("-0002")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"invoice_format": "FA-{AAAA}"}, "un compteur"),
        ({"quote_format": "FA-{AAAA}-{NN}"}, "Même forme que les factures"),
        ({"invoice_next": "0"}, "entre 1 et"),
    ],
)
def test_refused_numbering(client, admin, issuer, overrides, message):
    response = client.post(f"/emetteurs/{issuer.id}/numerotation", data=page_form(**overrides))
    assert response.status_code == 200 and message in response.get_data(as_text=True)
    db.session.expire_all()
    assert issuer.invoice_format == "FA-{AAAA}-{NNNN}"


def test_next_number_below_issued_is_refused(client, admin, issuer, customer):
    for _ in range(3):
        issue(issuer, customer)
    response = client.post(
        f"/emetteurs/{issuer.id}/numerotation", data=page_form(invoice_next="2", quote_format="Q{N}")
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200 and "Au moins 4" in text
    db.session.expire_all()
    assert issuer.quote_format == "DE-{AAAA}-{NNNN}"  # rien n'est enregistré
    assert billing.next_number(issuer, Invoice.INVOICE) == 4


def test_formats_are_unique_across_issuers(client, admin, issuer, vat_issuer):
    response = client.post(f"/emetteurs/{vat_issuer.id}/numerotation", data=page_form())
    assert "Même forme que les factures de" in response.get_data(as_text=True)


def test_live_example(client, admin, issuer):
    url = f"/emetteurs/{issuer.id}/numerotation/exemple"
    ok = client.get(
        url, query_string={"serie": "invoice_format", "invoice_format": "F{AA}{MM}-{NNN}", "invoice_next": "7"}
    )
    assert f"F{TODAY.year % 100:02d}{TODAY.month:02d}-007" in ok.get_data(as_text=True)
    assert "chaque mois" in ok.get_data(as_text=True)
    bad = client.get(url, query_string={"serie": "quote_format", "quote_format": "D-{X}"})
    assert "Balise inconnue" in bad.get_data(as_text=True)
    assert client.get(url, query_string={"serie": "autre"}).status_code == 400


def test_reader_cannot_change_numbering(client, reader, issuer):
    assert client.get(f"/emetteurs/{issuer.id}/numerotation").status_code == 403
    assert client.post(f"/emetteurs/{issuer.id}/numerotation", data=page_form()).status_code == 403


def test_new_issuer_gets_formats_that_do_not_clash(client, admin, issuer):
    from .test_invoices_web import ISSUER_FORM

    response = client.post("/emetteurs/nouveau", data={**ISSUER_FORM, "code": "regie-2", "name": "Régie 2"})
    assert response.status_code == 302
    other = db.session.scalar(db.select(Issuer).filter_by(code="regie-2"))
    assert other.invoice_format == "REGIE-2-FA-{AAAA}-{NNNN}"
    assert other.quote_format == "REGIE-2-DE-{AAAA}-{NNNN}"


def test_next_number_applies_to_the_new_series(client, admin, issuer, customer):
    """Passer d'une série annuelle à une série mensuelle : le numéro affiché est celui qui sort."""
    for _ in range(3):
        issue(issuer, customer)
    response = client.post(
        f"/emetteurs/{issuer.id}/numerotation", data=page_form(invoice_format="F{AA}{MM}-{L}", invoice_next="4")
    )
    assert response.status_code == 302
    assert issue(issuer, customer).number == f"F{TODAY.year % 100:02d}{TODAY.month:02d}-D"
