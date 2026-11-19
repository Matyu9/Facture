"""Factures arrivées à échéance sans être réglées : signalées une fois aux personnes qui suivent les règlements."""

from datetime import date

from ..extensions import db
from ..models import Invoice
from . import money, notify


def overdue(today=None):
    """Factures émises, échues, avec un reste à payer, la plus ancienne échéance d'abord."""
    today = today or date.today()
    candidates = db.session.scalars(
        db.select(Invoice)
        .where(Invoice.kind == Invoice.INVOICE, Invoice.status == Invoice.ISSUED, Invoice.due_date < today)
        .order_by(Invoice.due_date, Invoice.id)
    ).all()
    return [invoice for invoice in candidates if invoice.balance_cents > 0]


def notify_overdue(now=None):
    """Une notification par facture passée en retard (une seule fois, grâce à la clé de déduplication). Sans commit."""
    today = (now.date() if now else None) or date.today()
    recipients = notify.holders("payment.edit")
    count = 0
    for invoice in overdue(today):
        created = notify.notify(
            recipients,
            f"Facture {invoice.number} ({invoice.recipient_name}) échue le {invoice.due_date:%d/%m/%Y} : "
            f"reste {money.amount(invoice.balance_cents)} à payer.",
            link=f"/factures/{invoice.id}",
            dedup_key=f"overdue:{invoice.id}",
        )
        count += bool(created)
    return count
