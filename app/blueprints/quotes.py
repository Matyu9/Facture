"""Devis : liste, création, décision du client et passage en facture.

Un devis est une pièce comme les autres (table Invoice, type « devis ») : brouillon, modification, PDF, émission
numérotée et envoi passent par les écrans des factures. Ici, ce qui lui est propre.
"""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import Invoice
from ..permissions import can, require
from ..services import billing
from ..services.activity import log
from .invoices import PER_PAGE, _filters, create_piece

bp = Blueprint("quotes", __name__, url_prefix="/devis")

DECISIONS = {"accepter": Invoice.ACCEPTED, "refuser": Invoice.REFUSED, "attente": None}
DECISION_LOG = {Invoice.ACCEPTED: "accepté", Invoice.REFUSED: "refusé", None: "remis en attente"}


def _quote(quote_id):
    quote = db.session.get(Invoice, quote_id)
    if quote is None or not quote.is_quote:
        abort(404)
    return quote


@bp.route("")
@require("invoice.view")
def index():
    query, params, wanted = _filters(kinds=(Invoice.QUOTE,))
    items = db.session.scalars(query).all()
    if wanted:
        items = [i for i in items if billing.state(i) == wanted]
    total = len(items)
    page = max(request.args.get("page", 1, type=int), 1)
    states = {i.id: billing.state(i) for i in items}
    pending = [i for i in items if states[i.id] == billing.PENDING]
    accepted = [i for i in items if states[i.id] == billing.ACCEPTED]
    return render_template(
        "quotes/index.html",
        items=items[(page - 1) * PER_PAGE : page * PER_PAGE],
        total=total,
        page=page,
        pages=max((total + PER_PAGE - 1) // PER_PAGE, 1),
        params=params,
        summary={
            "pending": sum(i.total_ttc_cents for i in pending),
            "pending_count": len(pending),
            "accepted": sum(i.total_ttc_cents for i in accepted),
            "accepted_count": len(accepted),
        },
        issuers=billing.issuers(include_archived=True),
        states=billing.QUOTE_STATES,
        state=billing.state,
        tones=billing.TONES,
        edit=can(current_user, "invoice.edit"),
    )


@bp.route("/nouveau", methods=["GET", "POST"])
@require("invoice.edit")
def create():
    return create_piece(Invoice.QUOTE)


@bp.route("/<int:quote_id>/<any(accepter, refuser, attente):action>", methods=["POST"])
@require("invoice.edit")
def decide(quote_id, action):
    quote = _quote(quote_id)
    decision = DECISIONS[action]
    try:
        billing.decide(quote, decision, by=current_user)
    except billing.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("invoices.detail", invoice_id=quote.id))
    log("quote.decision", f"Devis {quote.number} {DECISION_LOG[decision]}", invoice=quote, customer=quote.customer)
    db.session.commit()
    flash(f"Devis {quote.number} {DECISION_LOG[decision]}.", "success")
    return redirect(url_for("invoices.detail", invoice_id=quote.id))


@bp.route("/<int:quote_id>/facturer", methods=["POST"])
@require("invoice.edit")
def to_invoice(quote_id):
    quote = _quote(quote_id)
    try:
        invoice = billing.invoice_from_quote(quote, by=current_user)
    except billing.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("invoices.detail", invoice_id=quote.id))
    db.session.flush()
    log(
        "invoice.create",
        f"Brouillon de facture tiré du devis {quote.number}",
        invoice=invoice,
        customer=invoice.customer,
    )
    db.session.commit()
    flash("Facture préparée d'après le devis : vérifiez la date de la prestation, puis émettez-la.", "success")
    return redirect(url_for("invoices.edit", invoice_id=invoice.id))
