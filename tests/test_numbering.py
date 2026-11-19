"""Numérotation réglable : formats, compteur en chiffres ou en lettres, remise à zéro, numéro de départ, écrans."""

from datetime import date, datetime

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import Activity, Invoice, Issuer, NumberSequence, NumberSeries
from app.services import api_tokens, billing, numbering

from .conftest import make_draft
from .test_invoices_web import lines_form, post_form

TODAY = date.today()
LAST_YEAR = date(TODAY.year - 1, 12, 31)


def issue(issuer, customer, day=None):
    invoice = make_draft(issuer, customer)
    billing.issue(invoice, issue_date=day)
    db.session.commit()
    return invoice


def quote_form(**overrides):
    return {"quote_format": "DE-{AAAA}-{NNNN}", "quote_next": "1", **overrides}


def series_form(**overrides):
    data = {
        "name": "Comités d'entreprise",
        "code": "ce",
        "invoice_format": "CE-{AAAA}-{NNNN}",
        "invoice_next": "1",
        "credit_format": "CE-AV-{AAAA}-{NNNN}",
        "credit_next": "1",
    }
    data.update(overrides)
    return data


def general(issuer):
    return billing.default_series(issuer)


def add_series(
    issuer, code="ce", name="Comités d'entreprise", invoice="CE-{AAAA}-{NNNN}", credit="CE-AV-{AAAA}-{NNNN}"
):
    general(issuer)
    series = NumberSeries(issuer=issuer, code=code, name=name, invoice_format=invoice, credit_format=credit)
    db.session.add(series)
    db.session.commit()
    return series


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
    assert general(issuer).code == "generale" and general(issuer).is_default


def test_custom_format_with_letters(app, issuer, customer):
    general(issuer).invoice_format = "F{AA}-{L}"
    db.session.commit()
    numbers = [issue(issuer, customer).number for _ in range(3)]
    yy = f"{TODAY.year % 100:02d}"
    assert numbers == [f"F{yy}-A", f"F{yy}-B", f"F{yy}-C"]


def test_yearly_series_restarts_and_continuous_series_does_not(app, issuer, customer):
    general(issuer).invoice_format = "FA-{AAAA}-{NNN}"
    general(issuer).credit_format = "AV-{NNNNN}"
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
    general(issuer).invoice_format = "FA{AAAA}{MM}-{NN}"
    db.session.commit()
    previous = issue(issuer, customer, LAST_YEAR)
    current = issue(issuer, customer)
    assert previous.number == f"FA{LAST_YEAR.year}12-01"
    assert current.number == f"FA{TODAY.year}{TODAY.month:02d}-01"


def test_cosmetic_change_keeps_the_series_going(app, issuer, customer):
    issue(issuer, customer)
    general(issuer).invoice_format = "FA-{AAAA}-{NNNNNN}"
    db.session.commit()
    assert issue(issuer, customer).number == f"FA-{TODAY.year}-000002"


def test_a_number_is_never_given_twice(app, issuer, customer):
    """Changer de format peut faire retomber sur un numéro déjà donné : l'émission est refusée, rien n'est pris."""
    general(issuer).invoice_format = "FA-{N}"
    db.session.commit()
    issue(issuer, customer)  # FA-1
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
    general(issuer).invoice_format = "FA-{N}"  # série continue : les pièces de l'an dernier comptent
    assert billing.lowest_next_number(issuer, Invoice.INVOICE) == 2


# ---------------------------------------------------------------------------
# Plusieurs séries
# ---------------------------------------------------------------------------


def issue_in(series, customer, day=None):
    invoice = make_draft(series.issuer, customer)
    billing.set_series(invoice, series)
    billing.issue(invoice, issue_date=day)
    db.session.commit()
    return invoice


def test_each_series_has_its_own_numbers(app, issuer, customer):
    ce = add_series(issuer)
    numbers = [
        issue(issuer, customer).number,
        issue_in(ce, customer).number,
        issue(issuer, customer).number,
        issue_in(ce, customer).number,
    ]
    y = TODAY.year
    assert numbers == [f"FA-{y}-0001", f"CE-{y}-0001", f"FA-{y}-0002", f"CE-{y}-0002"]


def test_a_credit_note_follows_its_invoice_series(app, issuer, customer):
    ce = add_series(issuer)
    invoice = issue_in(ce, customer)
    credit = billing.new_credit_note(invoice)
    assert credit.series is ce
    with pytest.raises(billing.BillingError, match="un avoir suit sa facture"):
        billing.set_series(credit, general(issuer))
    billing.issue(credit)
    db.session.commit()
    assert credit.number == f"CE-AV-{TODAY.year}-0001"


def test_chronology_is_checked_per_series(app, issuer, customer):
    """Une série peut émettre à une date antérieure à la dernière pièce d'une autre série, pas de la sienne."""
    ce = add_series(issuer)
    issue(issuer, customer)
    assert issue_in(ce, customer, LAST_YEAR).number == f"CE-{LAST_YEAR.year}-0001"
    late = make_draft(issuer, customer)
    with pytest.raises(billing.BillingError, match="ordre des dates"):
        billing.issue(late, issue_date=LAST_YEAR)


def test_start_number_per_series(app, issuer, customer):
    ce = add_series(issuer)
    billing.set_next_number(issuer, Invoice.INVOICE, 40, series=ce)
    db.session.commit()
    assert issue_in(ce, customer).number == f"CE-{TODAY.year}-0040"
    assert issue(issuer, customer).number == f"FA-{TODAY.year}-0001"


def test_series_follow_duplicates_issuer_changes_and_quotes(app, issuer, vat_issuer, customer):
    ce = add_series(issuer)
    invoice = issue_in(ce, customer)
    copy = billing.duplicate(invoice)
    assert copy.series is ce
    billing.change_issuer(copy, vat_issuer)
    assert copy.series is general(vat_issuer)
    with pytest.raises(billing.BillingError, match="autre émetteur"):
        billing.set_series(copy, ce)
    quote = billing.new_draft(issuer, customer, kind=Invoice.QUOTE)
    assert quote.series is None
    billing.set_lines(quote, [billing.LineInput(label="Concert", quantity=1, unit_price_cents=1000)])
    billing.issue(quote)
    assert billing.invoice_from_quote(quote, series=ce).series is ce


def test_archived_series(app, issuer, customer):
    ce = add_series(issuer)
    draft = make_draft(issuer, customer)
    billing.set_series(draft, ce)
    ce.archived_at = datetime.now()
    db.session.commit()
    with pytest.raises(billing.BillingError, match="archivée"):
        billing.issue(draft)
    db.session.rollback()
    with pytest.raises(billing.BillingError, match="archivée"):
        billing.set_series(make_draft(issuer, customer), ce)


def test_only_one_default_series_per_issuer(app, issuer):
    general(issuer)
    db.session.add(
        NumberSeries(issuer=issuer, code="x", name="X", invoice_format="X{N}", credit_format="XA{N}", is_default=True)
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


# ---------------------------------------------------------------------------
# Écrans
# ---------------------------------------------------------------------------


def test_numbering_page(client, admin, issuer, customer):
    issue(issuer, customer)
    page = client.get(f"/emetteurs/{issuer.id}/numerotation").get_data(as_text=True)
    assert "FA-{AAAA}-{NNNN}" in page and f"FA-{TODAY.year}-0002" in page and "Des pièces sont déjà émises" in page
    assert "Générale" in page and "par défaut" in page and "DE-{AAAA}-{NNNN}" in page
    detail = client.get(f"/emetteurs/{issuer.id}").get_data(as_text=True)
    assert f"FA-{TODAY.year}-0002" in detail and "Modifier la numérotation" in detail


def test_save_quote_numbering(client, admin, issuer):
    response = client.post(
        f"/emetteurs/{issuer.id}/numerotation", data=quote_form(quote_format="DEVIS-{L}", quote_next="3")
    )
    assert response.status_code == 302
    db.session.expire_all()
    assert issuer.quote_format == "DEVIS-{L}"
    assert billing.next_number(issuer, Invoice.QUOTE) == 3
    entry = db.session.scalar(db.select(Activity).where(Activity.action == "issuer.numbering"))
    assert entry is not None and "quote_next" in entry.changes


def test_quote_format_must_not_clash_with_invoices(client, admin, issuer):
    general(issuer)
    db.session.commit()
    response = client.post(f"/emetteurs/{issuer.id}/numerotation", data=quote_form(quote_format="FA-{AAAA}-{NN}"))
    assert response.status_code == 200 and "Même forme que les factures de" in response.get_data(as_text=True)


def test_add_a_series(client, admin, issuer, customer):
    assert client.get(f"/emetteurs/{issuer.id}/series/nouvelle").status_code == 200
    response = client.post(f"/emetteurs/{issuer.id}/series/nouvelle", data=series_form(invoice_next="12"))
    assert response.status_code == 302
    ce = db.session.scalar(db.select(NumberSeries).filter_by(code="ce"))
    assert ce.name == "Comités d'entreprise" and not ce.is_default
    assert issue_in(ce, customer).number == f"CE-{TODAY.year}-0012"
    assert db.session.scalar(db.select(Activity).where(Activity.action == "issuer.series")) is not None


def test_edit_a_series_and_make_it_default(client, admin, issuer, customer):
    ce = add_series(issuer)
    issue_in(ce, customer)
    page = client.get(f"/emetteurs/{issuer.id}/series/{ce.id}").get_data(as_text=True)
    assert "CE-{AAAA}-{NNNN}" in page and 'value="2"' in page
    response = client.post(
        f"/emetteurs/{issuer.id}/series/{ce.id}",
        data=series_form(invoice_format="CE{AA}-{NNN}", invoice_next="2", is_default="y"),
    )
    assert response.status_code == 302
    db.session.expire_all()
    assert ce.is_default and not db.session.scalar(db.select(NumberSeries).filter_by(code="generale")).is_default
    assert make_draft(issuer, customer).series is ce
    assert issue(issuer, customer).number == f"CE{TODAY.year % 100:02d}-002"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"invoice_format": "CE-{AAAA}"}, "un compteur"),
        ({"invoice_format": "FA-{AAAA}-{NN}"}, "Même forme que les factures de"),
        ({"credit_format": "CE-{AAAA}-{N}"}, "Même forme que les factures de cette série"),
        ({"code": "generale"}, "a ce code"),
        ({"name": "generale"}, "porte ce nom"),
        ({"code": "C E"}, "minuscules"),
        ({"invoice_next": "0"}, "entre 1 et"),
    ],
)
def test_refused_series(client, admin, issuer, overrides, message):
    general(issuer)
    db.session.commit()
    response = client.post(f"/emetteurs/{issuer.id}/series/nouvelle", data=series_form(**overrides))
    assert response.status_code == 200 and message in response.get_data(as_text=True)
    assert db.session.scalar(db.select(NumberSeries).filter_by(code="ce")) is None


def test_next_number_below_issued_is_refused(client, admin, issuer, customer):
    for _ in range(3):
        issue(issuer, customer)
    series = general(issuer)
    data = series_form(
        name="Générale", code="generale", invoice_format="FA-{AAAA}-{NNNN}", credit_format="AV-{AAAA}-{NNNN}"
    )
    response = client.post(
        f"/emetteurs/{issuer.id}/series/{series.id}", data={**data, "invoice_next": "2", "name": "Autre"}
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200 and "Au moins 4" in text
    db.session.expire_all()
    assert series.name == "Générale"  # rien n'est enregistré
    assert billing.next_number(issuer, Invoice.INVOICE) == 4


def test_next_number_applies_to_the_new_format(client, admin, issuer, customer):
    """Passer d'une série annuelle à mensuelle : le numéro affiché est celui qui sort."""
    for _ in range(3):
        issue(issuer, customer)
    series = general(issuer)
    data = series_form(
        name="Générale", code="generale", invoice_format="F{AA}{MM}-{L}", credit_format="AV-{AAAA}-{NNNN}"
    )
    response = client.post(
        f"/emetteurs/{issuer.id}/series/{series.id}", data={**data, "invoice_next": "4", "is_default": "y"}
    )
    assert response.status_code == 302
    assert issue(issuer, customer).number == f"F{TODAY.year % 100:02d}{TODAY.month:02d}-D"


def test_archive_a_series(client, admin, issuer):
    ce = add_series(issuer)
    client.post(f"/emetteurs/{issuer.id}/series/{ce.id}/archiver")
    db.session.expire_all()
    assert ce.archived
    default = general(issuer)
    client.post(f"/emetteurs/{issuer.id}/series/{default.id}/archiver")
    db.session.expire_all()
    assert not default.archived


def test_series_of_another_issuer_is_not_found(client, admin, issuer, vat_issuer):
    other = general(vat_issuer)
    db.session.commit()
    assert client.get(f"/emetteurs/{issuer.id}/series/{other.id}").status_code == 404


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
    assert client.post(f"/emetteurs/{issuer.id}/numerotation", data=quote_form()).status_code == 403
    assert client.post(f"/emetteurs/{issuer.id}/series/nouvelle", data=series_form()).status_code == 403


def test_new_issuer_gets_formats_that_do_not_clash(client, admin, issuer):
    from .test_invoices_web import ISSUER_FORM

    general(issuer)
    db.session.commit()
    response = client.post("/emetteurs/nouveau", data={**ISSUER_FORM, "code": "regie-2", "name": "Régie 2"})
    assert response.status_code == 302
    other = db.session.scalar(db.select(Issuer).filter_by(code="regie-2"))
    assert general(other).invoice_format == "REGIE-2-FA-{AAAA}-{NNNN}"
    assert other.quote_format == "REGIE-2-DE-{AAAA}-{NNNN}"


def test_choose_the_series_on_the_draft(client, manager, issuer, customer):
    ce = add_series(issuer)
    draft = make_draft(issuer, customer)
    page = client.get(f"/factures/{draft.id}/modifier").get_data(as_text=True)
    assert "Série de numérotation" in page and "Comités d&#39;entreprise" in page
    form = lines_form(draft, {"label": "Cours", "quantity": "1", "unit_price": "100"}, series_id=str(ce.id))
    response = post_form(client, f"/factures/{draft.id}/modifier", form)
    assert response.status_code == 302
    db.session.expire_all()
    assert draft.series is ce
    detail = client.get(f"/factures/{draft.id}").get_data(as_text=True)
    assert "(série Comités d&#39;entreprise)" in detail
    listing = client.get("/factures", query_string={"serie": ce.id}).get_data(as_text=True)
    assert "Toutes les séries" in listing


def test_single_series_is_not_shown_on_the_draft(client, manager, issuer, customer):
    draft = make_draft(issuer, customer)
    assert "Série de numérotation" not in client.get(f"/factures/{draft.id}/modifier").get_data(as_text=True)
    assert "Toutes les séries" not in client.get("/factures").get_data(as_text=True)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_series(client, issuer, customer):
    from .test_quotes import auth, body

    _, raw = api_tokens.create("adherents", can_issue=True)
    ce = add_series(issuer)
    listing = client.get("/api/v1/emetteurs", headers=auth(raw)).json["issuers"][0]["series"]
    assert [(s["code"], s["default"]) for s in listing] == [("generale", True), ("ce", False)]

    response = client.post("/api/v1/factures", json=body(issue=True, series="ce"), headers=auth(raw))
    assert response.status_code == 201 and response.json["series"] == "ce"
    assert response.json["number"] == f"CE-{TODAY.year}-0001"
    plain = client.post("/api/v1/factures", json=body(idempotency_key="k-2", issue=True), headers=auth(raw))
    assert plain.json["series"] == "generale" and plain.json["number"] == f"FA-{TODAY.year}-0001"
    only_ce = client.get("/api/v1/factures", query_string={"serie": "ce"}, headers=auth(raw)).json
    assert [i["number"] for i in only_ce["invoices"]] == [f"CE-{TODAY.year}-0001"]

    unknown = client.post("/api/v1/factures", json=body(idempotency_key="k-3", series="x"), headers=auth(raw))
    assert unknown.status_code == 422 and "Série inconnue" in unknown.json["error"]["message"]
    on_quote = client.post("/api/v1/devis", json=body(idempotency_key="k-4", series="ce"), headers=auth(raw))
    assert on_quote.status_code == 422
    ce.archived_at = datetime.now()
    db.session.commit()
    archived = client.post("/api/v1/factures", json=body(idempotency_key="k-5", series="ce"), headers=auth(raw))
    assert archived.status_code == 422


def test_api_quote_to_invoice_in_a_series(client, issuer, customer):
    from .test_quotes import auth, body

    _, raw = api_tokens.create("adherents", can_issue=True)
    add_series(issuer)
    quote = client.post("/api/v1/devis", json=body(issue=True), headers=auth(raw)).json
    assert quote["series"] is None
    invoice = client.post(
        f"/api/v1/devis/{quote['id']}/facturer", json={"series": "ce", "issue": True}, headers=auth(raw)
    )
    assert invoice.status_code == 201 and invoice.json["number"] == f"CE-{TODAY.year}-0001"
