"""Factures et avoirs : calcul, émission, numérotation, règlements.

Règles :
- un devis suit le même chemin (brouillon, puis émission numérotée dans sa propre série) ; il n'est pas une pièce
  comptable : ni règlement, ni avoir, ni journal des ventes. Accepté, il se transforme en facture ;
- un brouillon se modifie et se supprime librement ; il n'a pas de numéro ;
- l'émission attribue le numéro suivant de l'émetteur (par type de pièce et par année, sans trou), fige une copie de
  l'émetteur et du client, et archive le PDF : la pièce ne change plus ;
- une erreur sur une pièce émise se corrige par un avoir, total ou partiel ;
- le reste à payer d'une facture est son total, moins ses règlements et ses avoirs émis.

Les services ne font pas de commit : c'est la route qui valide.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..extensions import db
from ..models import RECIPIENT_FIELDS, Customer, Invoice, InvoiceLine, Issuer, NumberSequence, Payment
from . import archive, money

# Mention des pénalités de retard, obligatoire entre professionnels (art. L441-10 du code de commerce), imprimée quand
# l'émetteur n'en a pas saisi une à lui.
DEFAULT_LATE_PENALTY = (
    "En cas de retard de paiement, pénalités au taux de trois fois le taux d'intérêt légal, et indemnité forfaitaire "
    "pour frais de recouvrement de 40 € (art. L441-10 du code de commerce). Pas d'escompte pour paiement anticipé."
)
MAX_LINES = 200


class BillingError(ValueError):
    """Action refusée : le message est affiché tel quel."""


@dataclass
class LineInput:
    label: str
    quantity: Decimal
    unit_price_cents: int
    vat_rate: Decimal = Decimal("0")
    description: str | None = None
    unit: str | None = None
    product_id: int | None = None


# ---------------------------------------------------------------------------
# Calcul
# ---------------------------------------------------------------------------


def line_total(quantity, unit_price_cents):
    return money.round_cents(Decimal(quantity) * Decimal(unit_price_cents))


def compute(invoice, subject_to_vat=None):
    """Recalcule les lignes et les totaux. La TVA se calcule par taux, sur le total hors taxe de ce taux."""
    subject = subject_to_vat if subject_to_vat is not None else bool(invoice.issuer and invoice.issuer.subject_to_vat)
    bases = {}
    for line in invoice.lines:
        if not subject:
            line.vat_rate = Decimal("0")
        line.total_ht_cents = line_total(line.quantity, line.unit_price_cents)
        rate = Decimal(line.vat_rate).quantize(Decimal("0.01"))
        bases[rate] = bases.get(rate, 0) + line.total_ht_cents
    breakdown = []
    total_vat = 0
    for rate in sorted(bases):
        vat = money.round_cents(Decimal(bases[rate]) * rate / 100)
        breakdown.append([str(rate), bases[rate], vat])
        total_vat += vat
    invoice.total_ht_cents = sum(bases.values())
    invoice.total_vat_cents = total_vat
    invoice.total_ttc_cents = invoice.total_ht_cents + total_vat
    invoice.vat_breakdown = breakdown if subject else None


def set_lines(invoice, inputs):
    """Remplace les lignes d'un brouillon et recalcule. Sans commit."""
    _require_draft(invoice)
    inputs = list(inputs)
    if len(inputs) > MAX_LINES:
        raise BillingError(f"{MAX_LINES} lignes au plus.")
    invoice.lines.clear()
    db.session.flush()
    for position, row in enumerate(inputs):
        invoice.lines.append(
            InvoiceLine(
                position=position,
                label=row.label.strip()[:200],
                description=(row.description or "").strip() or None,
                quantity=row.quantity,
                unit=(row.unit or "").strip()[:30] or None,
                unit_price_cents=row.unit_price_cents,
                vat_rate=row.vat_rate,
                product_id=row.product_id,
            )
        )
    compute(invoice)


def default_vat_rate(issuer, product=None):
    if not issuer.subject_to_vat:
        return Decimal("0")
    if product is not None and product.vat_rate is not None:
        return Decimal(product.vat_rate)
    return Decimal(issuer.default_vat_rate)


# ---------------------------------------------------------------------------
# Brouillons
# ---------------------------------------------------------------------------


def _require_draft(invoice):
    if not invoice.is_draft:
        raise BillingError("Cette pièce est émise : elle ne se modifie plus. Faites un avoir pour la corriger.")


def new_draft(issuer, customer, by=None, kind=Invoice.INVOICE, recipient=None):
    """Brouillon pour un client enregistré, ou, sans client, pour un destinataire ponctuel (`recipient` : ses
    coordonnées, voir set_recipient)."""
    if issuer.archived:
        raise BillingError("Cet émetteur est archivé.")
    if customer is not None and customer.archived:
        raise BillingError("Ce client est archivé.")
    invoice = Invoice(kind=kind, status=Invoice.DRAFT, issuer=issuer, customer=customer, created_by=by)
    if customer is None:
        set_recipient(invoice, recipient or {})
    db.session.add(invoice)
    return invoice


def clean_recipient(values):
    """Coordonnées d'un destinataire ponctuel, bornées comme une fiche client. BillingError si le nom manque."""
    from .customers import clean

    values = {field: values.get(field) for field in RECIPIENT_FIELDS}
    values["kind"] = values["kind"] or Customer.PERSON
    try:
        return clean(values)
    except ValueError as exc:
        raise BillingError(str(exc)) from exc


def set_recipient(invoice, values):
    """Le brouillon s'adresse à un destinataire ponctuel, sans fiche client."""
    _require_draft(invoice)
    if invoice.is_credit and invoice.credited_invoice_id:
        raise BillingError("Un avoir garde le client de la facture qu'il corrige.")
    invoice.customer = None
    invoice.customer_snapshot = clean_recipient(values)


def set_customer(invoice, customer):
    """Le brouillon s'adresse à un client enregistré (les coordonnées ponctuelles éventuelles sont oubliées)."""
    _require_draft(invoice)
    if invoice.customer is customer:
        return
    if invoice.is_credit and invoice.credited_invoice_id:
        raise BillingError("Un avoir garde le client de la facture qu'il corrige.")
    if customer.archived:
        raise BillingError("Ce client est archivé.")
    invoice.customer = customer
    invoice.customer_snapshot = None


def save_recipient_as_customer(invoice):
    """Crée la fiche client d'un destinataire ponctuel et y rattache la pièce (et ses avoirs). Une pièce émise garde
    ses coordonnées imprimées ; seul le rattachement change. Renvoie le client. Sans commit."""
    if not invoice.is_one_off:
        raise BillingError("Cette pièce a déjà une fiche client.")
    customer = Customer(**invoice.recipient)
    db.session.add(customer)
    related = [invoice, *invoice.credit_notes]
    if invoice.credited_invoice is not None:
        related.append(invoice.credited_invoice)
    for piece in related:
        if piece.is_one_off:
            piece.customer = customer
            if piece.is_draft:
                piece.customer_snapshot = None
    db.session.flush()
    return customer


def delete_draft(invoice):
    _require_draft(invoice)
    db.session.delete(invoice)


def change_issuer(invoice, issuer):
    """Un brouillon change d'émetteur : les taux de TVA suivent son régime."""
    _require_draft(invoice)
    if invoice.is_credit and invoice.credited_invoice_id:
        raise BillingError("Un avoir garde l'émetteur de la facture qu'il corrige.")
    if issuer.archived:
        raise BillingError("Cet émetteur est archivé.")
    previous = invoice.issuer
    invoice.issuer = issuer
    if issuer.subject_to_vat and not (previous and previous.subject_to_vat):
        for line in invoice.lines:
            line.vat_rate = default_vat_rate(issuer, line.product)
    compute(invoice)


# ---------------------------------------------------------------------------
# Émission
# ---------------------------------------------------------------------------


def issuer_snapshot(issuer):
    return {
        "code": issuer.code,
        "name": issuer.name,
        "legal_name": issuer.legal_name,
        "legal_form": issuer.legal_form,
        "address": issuer.address,
        "postcode": issuer.postcode,
        "city": issuer.city,
        "country": issuer.country,
        "email": issuer.email,
        "phone": issuer.phone,
        "website": issuer.website,
        "siret": issuer.siret,
        "rna": issuer.rna,
        "vat_number": issuer.vat_number,
        "vat_regime": issuer.vat_regime,
        "vat_mention": issuer.mention,
        "iban": issuer.iban,
        "bic": issuer.bic,
        "payment_instructions": issuer.payment_instructions,
        "late_penalty": issuer.late_penalty,
        "footer": issuer.footer,
        "logo": issuer.logo,
        "pdf_layout": issuer.pdf_layout,
        "pdf_color": issuer.pdf_color,
        "logo_size": issuer.logo_size,
        "logo_position": issuer.logo_position,
        "intro_text": issuer.intro_text,
        "closing_text": issuer.closing_text,
        "recipient_label": issuer.recipient_label,
        "designation_label": issuer.designation_label,
    }


def customer_snapshot(customer):
    return customer.snapshot()


def last_issue_date(issuer, kind):
    return db.session.scalar(
        db.select(db.func.max(Invoice.issue_date)).where(
            Invoice.issuer_id == issuer.id, Invoice.kind == kind, Invoice.status == Invoice.ISSUED
        )
    )


def problems(invoice, issue_date=None):
    """Ce qui empêche d'émettre la pièce, en phrases. Liste vide : elle peut l'être."""
    found = []
    issuer, customer = invoice.issuer, invoice.recipient
    issue_date = issue_date or date.today()
    if not invoice.is_draft:
        return ["Cette pièce est déjà émise."]
    if issuer.archived:
        found.append("L'émetteur est archivé.")
    if not (issuer.legal_name and issuer.address and issuer.postcode and issuer.city):
        found.append("L'émetteur n'a pas de dénomination ou d'adresse complète (Émetteurs).")
    if issuer.subject_to_vat and not issuer.vat_number:
        found.append("L'émetteur est assujetti à la TVA mais n'a pas de numéro de TVA intracommunautaire.")
    if not customer.get("name"):
        found.append("Le client n'a pas de nom.")
    if not (customer.get("address") and customer.get("postcode") and customer.get("city")):
        found.append("L'adresse du client est incomplète : elle doit figurer sur la facture.")
    if not invoice.lines:
        found.append("La pièce n'a aucune ligne.")
    if any(not line.label.strip() for line in invoice.lines):
        found.append("Une ligne n'a pas de désignation.")
    if invoice.total_ttc_cents <= 0:
        found.append("Le total doit être positif.")
    if invoice.service_date is None and not invoice.is_quote:
        found.append("Indiquez la date de la prestation (ou de la vente) : c'est une mention obligatoire.")
    if invoice.is_quote and invoice.valid_until and invoice.valid_until < issue_date:
        found.append("La date de validité du devis est avant sa date d'émission.")
    if invoice.service_end and invoice.service_date and invoice.service_end < invoice.service_date:
        found.append("La fin de la période est avant son début.")
    if invoice.due_date and invoice.due_date < issue_date:
        found.append("L'échéance est avant la date d'émission.")
    last = last_issue_date(issuer, invoice.kind)
    if last and issue_date < last:
        found.append(
            f"La date d'émission ne peut pas être avant celle de la dernière pièce émise ({last:%d/%m/%Y}) : "
            "les numéros doivent suivre l'ordre des dates."
        )
    if invoice.is_credit:
        original = invoice.credited_invoice
        if original is None or original.status != Invoice.ISSUED:
            found.append("Un avoir doit corriger une facture émise.")
        elif invoice.total_ttc_cents > creditable_cents(original):
            found.append(
                f"L'avoir dépasse ce qui reste à créditer sur la facture {original.number} "
                f"({money.amount(creditable_cents(original))})."
            )
    return found


def _next_number(issuer, kind, year):
    """Prend le numéro suivant de la série, sous verrou de ligne : deux émissions simultanées ne l'auront pas toutes
    les deux. La transaction qui échoue ensuite rend le numéro (rien n'est validé) : pas de trou."""
    sequence = db.session.scalar(
        db.select(NumberSequence)
        .where(NumberSequence.issuer_id == issuer.id, NumberSequence.kind == kind, NumberSequence.year == year)
        .with_for_update()
    )
    if sequence is None:
        sequence = NumberSequence(issuer_id=issuer.id, kind=kind, year=year, last_number=0)
        db.session.add(sequence)
    sequence.last_number += 1
    prefix = {Invoice.CREDIT: issuer.credit_prefix, Invoice.QUOTE: issuer.quote_prefix}.get(kind, issuer.invoice_prefix)
    return sequence.last_number, f"{prefix}{year}-{sequence.last_number:0{issuer.number_digits}d}"


def issue(invoice, by=None, issue_date=None):
    """Émet la pièce : numéro, copies figées, échéance, PDF archivé. Sans commit (le PDF, lui, est déjà écrit)."""
    issue_date = issue_date or date.today()
    if issue_date > date.today():
        raise BillingError("La date d'émission ne peut pas être dans le futur.")
    compute(invoice)
    found = problems(invoice, issue_date)
    if found:
        raise BillingError(" ".join(found))
    issuer = invoice.issuer
    invoice.sequence_year = issue_date.year
    invoice.sequence_number, invoice.number = _next_number(issuer, invoice.kind, issue_date.year)
    invoice.issue_date = issue_date
    if invoice.is_quote:
        invoice.due_date = None
        if invoice.valid_until is None:
            invoice.valid_until = issue_date + timedelta(days=issuer.quote_validity_days)
    elif invoice.due_date is None and not invoice.is_credit:
        invoice.due_date = issue_date + timedelta(days=issuer.payment_terms_days)
    invoice.issuer_snapshot = issuer_snapshot(issuer)
    invoice.customer_snapshot = invoice.recipient
    if (
        invoice.kind == Invoice.INVOICE
        and invoice.customer_snapshot.get("kind") == Customer.ORGANISATION
        and not issuer.late_penalty
    ):
        invoice.issuer_snapshot["late_penalty"] = DEFAULT_LATE_PENALTY
    invoice.status = Invoice.ISSUED
    invoice.issued_at = datetime.now()
    invoice.issued_by = by
    db.session.flush()
    from . import pdf

    invoice.pdf_path, invoice.pdf_sha256 = archive.store(invoice, pdf.render(invoice))
    return invoice


# ---------------------------------------------------------------------------
# Avoirs
# ---------------------------------------------------------------------------


def _one_off(invoice):
    """Coordonnées d'un destinataire ponctuel à reprendre dans une nouvelle pièce (None s'il a une fiche client)."""
    return invoice.recipient if invoice.is_one_off else None


def creditable_cents(invoice):
    """Ce qu'un avoir peut encore annuler sur cette facture : son total, moins les avoirs déjà émis."""
    return invoice.total_ttc_cents - invoice.credited_cents


def new_credit_note(invoice, by=None):
    """Brouillon d'avoir qui reprend toutes les lignes de la facture (à réduire pour un avoir partiel)."""
    if invoice.kind != Invoice.INVOICE or invoice.status != Invoice.ISSUED:
        raise BillingError("Seule une facture émise peut être corrigée par un avoir.")
    if creditable_cents(invoice) <= 0:
        raise BillingError("Cette facture est déjà entièrement annulée par des avoirs.")
    pending = [c for c in invoice.credit_notes if c.is_draft]
    if pending:
        raise BillingError("Un avoir est déjà en préparation pour cette facture : terminez-le ou supprimez-le.")
    credit = new_draft(invoice.issuer, invoice.customer, by, kind=Invoice.CREDIT, recipient=_one_off(invoice))
    credit.credited_invoice = invoice
    credit.subject = f"Avoir sur la facture {invoice.number}" + (f" : {invoice.subject}" if invoice.subject else "")
    credit.service_date = invoice.service_date
    credit.service_end = invoice.service_end
    credit.customer_reference = invoice.customer_reference
    set_lines(
        credit,
        [
            LineInput(
                label=line.label,
                description=line.description,
                quantity=line.quantity,
                unit=line.unit,
                unit_price_cents=line.unit_price_cents,
                vat_rate=line.vat_rate,
                product_id=line.product_id,
            )
            for line in invoice.lines
        ],
    )
    return credit


def _copy_lines(source, target):
    set_lines(
        target,
        [
            LineInput(
                label=line.label,
                description=line.description,
                quantity=line.quantity,
                unit=line.unit,
                unit_price_cents=line.unit_price_cents,
                vat_rate=line.vat_rate if source.issuer.subject_to_vat else default_vat_rate(target.issuer),
                product_id=line.product_id,
            )
            for line in source.lines
        ],
    )


def duplicate(invoice, by=None):
    """Nouveau brouillon du même type (facture ou devis), avec le même émetteur, le même client et les mêmes
    lignes."""
    if invoice.is_credit:
        raise BillingError("Un avoir ne se duplique pas.")
    copy = new_draft(invoice.issuer, invoice.customer, by, kind=invoice.kind, recipient=_one_off(invoice))
    copy.subject = invoice.subject
    copy.notes = invoice.notes
    copy.customer_reference = invoice.customer_reference
    _copy_lines(invoice, copy)
    return copy


# ---------------------------------------------------------------------------
# Devis
# ---------------------------------------------------------------------------


def _require_issued_quote(quote):
    if not quote.is_quote or quote.status != Invoice.ISSUED:
        raise BillingError("Cette action concerne un devis émis.")


def decide(quote, decision, by=None):
    """Le client accepte ou refuse le devis ; None le remet en attente. Sans commit."""
    _require_issued_quote(quote)
    if decision not in (None, *Invoice.DECISIONS):
        raise BillingError("Décision inconnue.")
    if decision != Invoice.ACCEPTED and any(not i.is_credit for i in quote.quote_invoices):
        raise BillingError("Ce devis a déjà été facturé : il reste accepté.")
    quote.decision = decision
    quote.decided_at = datetime.now() if decision else None


def invoice_from_quote(quote, by=None):
    """Brouillon de facture reprenant le devis (destinataire, objet, lignes). Le devis est accepté au passage."""
    _require_issued_quote(quote)
    if quote.decision == Invoice.REFUSED:
        raise BillingError("Ce devis a été refusé : remettez-le en attente avant de le facturer.")
    if quote.quote_invoices:
        raise BillingError("Ce devis a déjà sa facture : retrouvez-la depuis la fiche du devis.")
    if quote.issuer.archived:
        raise BillingError("L'émetteur de ce devis est archivé.")
    customer = quote.customer if quote.customer is not None and not quote.customer.archived else None
    recipient = None if customer is not None else quote.recipient
    invoice = new_draft(quote.issuer, customer, by, recipient=recipient)
    invoice.quote = quote
    invoice.subject = quote.subject
    invoice.notes = quote.notes
    invoice.customer_reference = quote.customer_reference
    invoice.service_date = quote.service_date
    invoice.service_end = quote.service_end
    _copy_lines(quote, invoice)
    if quote.decision is None:
        decide(quote, Invoice.ACCEPTED, by)
    return invoice


# ---------------------------------------------------------------------------
# Règlements
# ---------------------------------------------------------------------------


def add_payment(invoice, amount_cents, paid_on, method, reference=None, note=None, by=None):
    if invoice.kind != Invoice.INVOICE or invoice.status != Invoice.ISSUED:
        raise BillingError("Un règlement s'enregistre sur une facture émise.")
    if method not in Payment.METHODS:
        raise BillingError("Mode de règlement inconnu.")
    if amount_cents <= 0:
        raise BillingError("Le montant doit être positif.")
    if amount_cents > invoice.balance_cents:
        raise BillingError(f"Le montant dépasse le reste à payer ({money.amount(invoice.balance_cents)}).")
    if paid_on > date.today():
        raise BillingError("La date du règlement ne peut pas être dans le futur.")
    if paid_on < invoice.issue_date:
        raise BillingError("La date du règlement est avant celle de la facture.")
    payment = Payment(
        invoice=invoice,
        amount_cents=amount_cents,
        paid_on=paid_on,
        method=method,
        reference=(reference or "").strip()[:120] or None,
        note=(note or "").strip()[:255] or None,
        created_by=by,
    )
    db.session.add(payment)
    return payment


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------

DRAFT, DUE, LATE, PARTIAL, PAID, CANCELLED, ISSUED_CREDIT = (
    "brouillon",
    "a_payer",
    "en_retard",
    "partiel",
    "payee",
    "annulee",
    "avoir",
)
STATES = {
    DRAFT: "Brouillon",
    DUE: "À payer",
    LATE: "En retard",
    PARTIAL: "Payée en partie",
    PAID: "Payée",
    CANCELLED: "Annulée par avoir",
    ISSUED_CREDIT: "Avoir émis",
}
TONES = {DRAFT: "", DUE: "badge-info", LATE: "badge-warn", PARTIAL: "badge-info", PAID: "badge-ok", CANCELLED: ""}

# États d'un devis émis.
PENDING, ACCEPTED, REFUSED, EXPIRED, INVOICED = "en_attente", "accepte", "refuse", "expire", "facture"
QUOTE_STATES = {
    DRAFT: "Brouillon",
    PENDING: "En attente",
    ACCEPTED: "Accepté",
    REFUSED: "Refusé",
    EXPIRED: "Expiré",
    INVOICED: "Facturé",
}
INVOICE_STATES = dict(STATES)  # filtres de la liste des factures
STATES.update({k: v for k, v in QUOTE_STATES.items() if k != DRAFT})
TONES.update({PENDING: "badge-info", ACCEPTED: "badge-ok", REFUSED: "", EXPIRED: "badge-warn", INVOICED: "badge-ok"})


def quote_state(quote):
    if quote.is_draft:
        return DRAFT
    if any(not i.is_credit for i in quote.quote_invoices):
        return INVOICED
    if quote.decision == Invoice.ACCEPTED:
        return ACCEPTED
    if quote.decision == Invoice.REFUSED:
        return REFUSED
    if quote.valid_until and quote.valid_until < date.today():
        return EXPIRED
    return PENDING


def state(invoice):
    if invoice.is_quote:
        return quote_state(invoice)
    if invoice.is_draft:
        return DRAFT
    if invoice.is_credit:
        return ISSUED_CREDIT
    if invoice.balance_cents <= 0:
        if invoice.paid_cents == 0 and invoice.credited_cents >= invoice.total_ttc_cents:
            return CANCELLED
        return PAID
    if invoice.overdue:
        return LATE
    return PARTIAL if invoice.paid_cents or invoice.credited_cents else DUE


def issuers(include_archived=False):
    query = db.select(Issuer).order_by(Issuer.name)
    if not include_archived:
        query = query.where(Issuer.archived_at.is_(None))
    return db.session.scalars(query).all()
