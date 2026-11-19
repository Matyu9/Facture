"""Règles de facturation : calculs, numérotation, émission, avoirs, règlements, archive."""

import hashlib
from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from app.extensions import db
from app.models import Customer, Invoice, Issuer, NumberSequence
from app.services import archive, billing, money, reminders

from .conftest import TEST_DATABASE_URL, make_draft

# ---------------------------------------------------------------------------
# Montants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, cents",
    [("12", 1200), ("12,5", 1250), ("12.50", 1250), ("1 234,56", 123456), ("1 234,56 €", 123456), ("-3", -300)],
)
def test_parse_amount(text, cents):
    assert money.parse_amount(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "1,234", "12,345", "1e3", "12,5,0", "10000000000"])
def test_parse_amount_refuses(text):
    with pytest.raises(ValueError):
        money.parse_amount(text)


def test_amount_is_written_the_french_way():
    assert money.amount(123456) == "1 234,56 €"
    assert money.amount(-250) == "−2,50 €"
    assert money.amount(5) == "0,05 €"
    assert money.amount_input(-123456) == "-1234,56"
    assert money.parse_amount(money.amount_input(-123456)) == -123456


def test_numbers_and_rates():
    assert money.number(Decimal("1.500")) == "1,5"
    assert money.number(Decimal("2")) == "2"
    assert money.number(Decimal("1500.25")) == "1 500,25"
    assert money.rate(Decimal("5.50")) == "5,5 %"


def test_rounding_is_half_up():
    assert money.round_cents(Decimal("0.5")) == 1
    assert money.round_cents(Decimal("2.5")) == 3
    assert money.round_cents(Decimal("-0.5")) == -1


# ---------------------------------------------------------------------------
# Calcul
# ---------------------------------------------------------------------------


def line(price, quantity="1", rate="0", label="Ligne"):
    return billing.LineInput(label=label, quantity=Decimal(quantity), unit_price_cents=price, vat_rate=Decimal(rate))


def test_totals_without_vat(issuer, customer):
    invoice = make_draft(issuer, customer, 30000, 12050)
    assert (invoice.total_ht_cents, invoice.total_vat_cents, invoice.total_ttc_cents) == (42050, 0, 42050)
    assert invoice.vat_breakdown is None


def test_franchise_ignores_any_vat_rate_sent(issuer, customer):
    invoice = billing.new_draft(issuer, customer)
    billing.set_lines(invoice, [line(1000, rate="20")])
    assert invoice.lines[0].vat_rate == 0 and invoice.total_ttc_cents == 1000


def test_vat_is_computed_per_rate_on_the_total_of_that_rate(vat_issuer, customer):
    invoice = billing.new_draft(vat_issuer, customer)
    # 3 × 3,33 € = 9,99 € HT à 20 % → TVA 1,998 → 2,00 € ; ligne à 5,5 % : 10 € → 0,55 €.
    billing.set_lines(invoice, [line(333, "3", "20"), line(1000, "1", "5.5"), line(1, "1", "20")])
    assert invoice.vat_breakdown == [["5.50", 1000, 55], ["20.00", 1000, 200]]
    assert (invoice.total_ht_cents, invoice.total_vat_cents, invoice.total_ttc_cents) == (2000, 255, 2255)


def test_fractional_quantities_are_rounded_once_per_line(issuer, customer):
    invoice = billing.new_draft(issuer, customer)
    billing.set_lines(invoice, [line(1999, "1.5"), line(-500, "1", label="Remise")])
    assert [line.total_ht_cents for line in invoice.lines] == [2999, -500]
    assert invoice.total_ttc_cents == 2499


def test_changing_to_a_vat_issuer_applies_its_default_rate(issuer, vat_issuer, customer):
    invoice = make_draft(issuer, customer, 10000)
    billing.change_issuer(invoice, vat_issuer)
    assert invoice.lines[0].vat_rate == Decimal("20") and invoice.total_ttc_cents == 12000


# ---------------------------------------------------------------------------
# Émission et numérotation
# ---------------------------------------------------------------------------


def test_issue_numbers_freezes_and_archives(app, draft):
    billing.issue(draft, issue_date=date(2026, 3, 2))
    db.session.commit()
    assert draft.number == "FA-2026-0001" and draft.status == Invoice.ISSUED
    assert draft.due_date == date(2026, 4, 1)
    assert draft.issuer_snapshot["legal_name"] == "École de musique"
    assert draft.issuer_snapshot["vat_mention"] == "TVA non applicable, art. 293 B du CGI."
    assert draft.customer_snapshot["city"] == "Gien"
    content = (app.config["ARCHIVE_DIR"] / draft.pdf_path).read_bytes()
    assert content.startswith(b"%PDF") and hashlib.sha256(content).hexdigest() == draft.pdf_sha256
    assert draft.pdf_path == "ecole/2026/FA-2026-0001.pdf"


def test_numbers_follow_each_other_per_issuer_kind_and_year(issuer, vat_issuer, customer):
    numbers = []
    for who, when in (
        (issuer, date(2025, 12, 30)),
        (issuer, date(2026, 1, 2)),
        (issuer, date(2026, 1, 2)),
        (vat_issuer, date(2026, 1, 3)),
    ):
        invoice = make_draft(who, customer, 1000)
        billing.issue(invoice, issue_date=when)
        db.session.commit()
        numbers.append(invoice.number)
    assert numbers == ["FA-2025-0001", "FA-2026-0001", "FA-2026-0002", "OR-2026-0001"]
    credit = billing.new_credit_note(db.session.scalar(db.select(Invoice).filter_by(number="FA-2026-0002")))
    billing.issue(credit, issue_date=date(2026, 1, 5))
    assert credit.number == "AV-2026-0001"


def test_a_failed_transaction_gives_its_number_back(issuer, customer):
    first = make_draft(issuer, customer, 1000)
    billing.issue(first)
    db.session.rollback()  # par exemple une erreur juste après l'émission
    second = make_draft(issuer, customer, 1000)
    billing.issue(second)
    db.session.commit()
    assert second.number.endswith("-0001")


def test_the_sequence_row_is_locked_until_the_issuing_transaction_ends(issuer, customer):
    """Deux émissions simultanées ne peuvent pas lire le même dernier numéro : la seconde attend la première."""
    db.session.add(NumberSequence(issuer_id=issuer.id, kind=Invoice.INVOICE, year=date.today().year, last_number=4))
    db.session.commit()
    invoice = make_draft(issuer, customer, 1000)
    billing.issue(invoice)  # pas encore validée
    other = sa.create_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        with other.connect() as connection, pytest.raises(sa.exc.OperationalError, match="lock"):
            connection.exec_driver_sql(
                f"SELECT last_number FROM number_sequence WHERE issuer_id = {issuer.id} FOR UPDATE NOWAIT"
            )
    finally:
        other.dispose()
    db.session.commit()
    assert invoice.number.endswith("-0005")


@pytest.mark.parametrize(
    "spoil, message",
    [
        (lambda inv: inv.lines.clear(), "aucune ligne"),
        (lambda inv: setattr(inv, "service_date", None), "date de la prestation"),
        (lambda inv: setattr(inv.customer, "address", None), "adresse du client"),
        (lambda inv: setattr(inv.issuer, "city", None), "adresse complète"),
        (lambda inv: setattr(inv.lines[0], "unit_price_cents", -99999), "positif"),
        (lambda inv: setattr(inv, "due_date", date.today() - timedelta(days=1)), "échéance"),
    ],
)
def test_issue_refuses_an_incomplete_invoice(draft, spoil, message):
    spoil(draft)
    with pytest.raises(billing.BillingError, match=message):
        billing.issue(draft)
    assert draft.number is None


def test_issue_refuses_a_vat_issuer_without_vat_number(vat_issuer, customer):
    vat_issuer.vat_number = None
    with pytest.raises(billing.BillingError, match="TVA intracommunautaire"):
        billing.issue(make_draft(vat_issuer, customer, 1000))


def test_dates_must_not_go_backwards_nor_into_the_future(issuer, customer):
    first = make_draft(issuer, customer, 1000)
    billing.issue(first, issue_date=date.today() - timedelta(days=2))
    db.session.commit()
    late = make_draft(issuer, customer, 1000)
    with pytest.raises(billing.BillingError, match="avant celle de la dernière"):
        billing.issue(late, issue_date=date.today() - timedelta(days=3))
    with pytest.raises(billing.BillingError, match="futur"):
        billing.issue(late, issue_date=date.today() + timedelta(days=1))


def test_an_issued_invoice_cannot_change_or_disappear(issued_invoice, customer):
    with pytest.raises(billing.BillingError):
        billing.set_lines(issued_invoice, [line(1)])
    with pytest.raises(billing.BillingError):
        billing.delete_draft(issued_invoice)
    with pytest.raises(billing.BillingError):
        billing.issue(issued_invoice)


def test_snapshot_keeps_the_invoice_as_issued(issued_invoice, customer, issuer):
    customer.address, issuer.legal_name = "Ailleurs", "Nouveau nom"
    db.session.commit()
    assert issued_invoice.customer_snapshot["address"] == "Place de la Mairie"
    assert issued_invoice.issuer_snapshot["legal_name"] == "École de musique"


def test_late_penalty_mention_for_organisations_only(issuer, customer):
    organisation = make_draft(issuer, customer, 1000)
    billing.issue(organisation)
    family = Customer(kind=Customer.PERSON, name="Famille Martin", address="1 rue", postcode="45000", city="Orléans")
    db.session.add(family)
    private = make_draft(issuer, family, 1000)
    billing.issue(private)
    assert organisation.issuer_snapshot["late_penalty"] == billing.DEFAULT_LATE_PENALTY
    assert private.issuer_snapshot["late_penalty"] is None


def test_archive_detects_a_modified_or_missing_pdf(app, issued_invoice):
    assert archive.read(issued_invoice).startswith(b"%PDF")
    path = app.config["ARCHIVE_DIR"] / issued_invoice.pdf_path
    path.chmod(0o640)
    path.write_bytes(path.read_bytes().replace(b"%PDF", b"%PDX", 1))
    with pytest.raises(archive.ArchiveError, match="modifié"):
        archive.read(issued_invoice)
    path.unlink()
    with pytest.raises(archive.ArchiveError, match="introuvable"):
        archive.read(issued_invoice)


def test_archived_pdf_is_read_only(app, issued_invoice):
    mode = (app.config["ARCHIVE_DIR"] / issued_invoice.pdf_path).stat().st_mode & 0o777
    assert mode == 0o440


# ---------------------------------------------------------------------------
# Avoirs
# ---------------------------------------------------------------------------


def test_full_credit_note_cancels_the_invoice(issued_invoice):
    credit = billing.new_credit_note(issued_invoice)
    assert credit.total_ttc_cents == issued_invoice.total_ttc_cents and credit.credited_invoice is issued_invoice
    assert billing.state(issued_invoice) == billing.DUE  # tant que l'avoir n'est pas émis
    billing.issue(credit)
    db.session.commit()
    assert issued_invoice.balance_cents == 0 and billing.state(issued_invoice) == billing.CANCELLED
    assert billing.state(credit) == billing.ISSUED_CREDIT
    with pytest.raises(billing.BillingError, match="déjà entièrement annulée"):
        billing.new_credit_note(issued_invoice)


def test_partial_credit_note_reduces_the_balance(issued_invoice):
    credit = billing.new_credit_note(issued_invoice)
    billing.set_lines(credit, [line(5000)])
    billing.issue(credit)
    db.session.commit()
    assert issued_invoice.balance_cents == issued_invoice.total_ttc_cents - 5000
    assert billing.state(issued_invoice) == billing.PARTIAL


def test_credit_note_cannot_exceed_what_remains(issued_invoice):
    first = billing.new_credit_note(issued_invoice)
    billing.set_lines(first, [line(40000)])
    billing.issue(first)
    db.session.commit()
    second = billing.new_credit_note(issued_invoice)
    with pytest.raises(billing.BillingError, match="dépasse"):
        billing.issue(second)  # reprend toutes les lignes : 420,50 € > 20,50 € restants


def test_only_one_credit_note_in_preparation(issued_invoice):
    billing.new_credit_note(issued_invoice)
    db.session.flush()
    with pytest.raises(billing.BillingError, match="en préparation"):
        billing.new_credit_note(issued_invoice)


def test_no_credit_note_on_a_draft_or_a_credit_note(draft, issued_invoice):
    other = make_draft(draft.issuer, draft.customer, 1000)
    with pytest.raises(billing.BillingError):
        billing.new_credit_note(other)
    credit = billing.new_credit_note(issued_invoice)
    billing.issue(credit)
    with pytest.raises(billing.BillingError):
        billing.new_credit_note(credit)


# ---------------------------------------------------------------------------
# Règlements et retards
# ---------------------------------------------------------------------------


def test_payments_bring_the_balance_down_to_paid(issued_invoice):
    billing.add_payment(issued_invoice, 20000, date.today(), "cheque", reference="1234567")
    db.session.flush()
    assert billing.state(issued_invoice) == billing.PARTIAL and issued_invoice.balance_cents == 22050
    billing.add_payment(issued_invoice, 22050, date.today(), "virement")
    db.session.flush()
    assert billing.state(issued_invoice) == billing.PAID and issued_invoice.balance_cents == 0


@pytest.mark.parametrize(
    "amount, when, method, message",
    [
        (0, date.today(), "cheque", "positif"),
        (42051, date.today(), "cheque", "dépasse"),
        (100, date.today() + timedelta(days=1), "cheque", "futur"),
        (100, date.today() - timedelta(days=400), "cheque", "avant celle de la facture"),
        (100, date.today(), "bitcoin", "inconnu"),
    ],
)
def test_payment_refusals(issued_invoice, amount, when, method, message):
    with pytest.raises(billing.BillingError, match=message):
        billing.add_payment(issued_invoice, amount, when, method)


def test_no_payment_on_a_draft(draft):
    with pytest.raises(billing.BillingError):
        billing.add_payment(draft, 100, date.today(), "cheque")


def test_overdue_invoices_are_signalled_once(issuer, customer, accounts):
    old = make_draft(issuer, customer, 1000)
    billing.issue(old, issue_date=date.today() - timedelta(days=45))
    paid = make_draft(issuer, customer, 1000)
    billing.issue(paid, issue_date=date.today() - timedelta(days=45))
    billing.add_payment(paid, 1000, date.today(), "especes")
    recent = make_draft(issuer, customer, 1000)
    billing.issue(recent)
    db.session.commit()
    assert reminders.overdue() == [old] and billing.state(old) == billing.LATE
    assert reminders.notify_overdue() == 1
    db.session.commit()
    assert reminders.notify_overdue() == 0


def test_duplicate_copies_lines_into_a_new_draft(issued_invoice):
    copy = billing.duplicate(issued_invoice)
    assert copy.is_draft and copy.number is None
    assert [(line.label, line.unit_price_cents) for line in copy.lines] == [
        (line.label, line.unit_price_cents) for line in issued_invoice.lines
    ]


def test_an_archived_issuer_or_customer_takes_no_new_invoice(issuer, customer):
    issuer.archived_at = date.today()
    with pytest.raises(billing.BillingError, match="archivé"):
        billing.new_draft(issuer, customer)
    issuer.archived_at = None
    customer.archived_at = date.today()
    with pytest.raises(billing.BillingError, match="archivé"):
        billing.new_draft(issuer, customer)


def test_issuer_mention_can_be_replaced(issuer):
    assert issuer.mention.startswith("TVA non applicable")
    issuer.vat_regime = Issuer.EXEMPT
    assert "261-7-1°" in issuer.mention
    issuer.vat_mention = "Mention propre"
    assert issuer.mention == "Mention propre"
