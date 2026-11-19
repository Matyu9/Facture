"""Accueil : où en sont les factures (à émettre, à encaisser, en retard), par émetteur pour l'année en cours."""

from datetime import date

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Invoice, Notification
from ..permissions import can
from ..services import billing, reminders

bp = Blueprint("dashboard", __name__)

RECENT_NOTIFICATIONS = 5


def _year_summary(year):
    """Par émetteur : facturé, avoirs, encaissé et reste dû sur les pièces émises cette année."""
    rows = {}
    issued = db.session.scalars(
        db.select(Invoice).where(Invoice.status == Invoice.ISSUED, db.extract("year", Invoice.issue_date) == year)
    ).all()
    for invoice in issued:
        row = rows.setdefault(
            invoice.issuer_id, {"issuer": invoice.issuer, "invoiced": 0, "credited": 0, "paid": 0, "due": 0, "count": 0}
        )
        if invoice.is_credit:
            row["credited"] += invoice.total_ttc_cents
        else:
            row["count"] += 1
            row["invoiced"] += invoice.total_ttc_cents
            row["paid"] += invoice.paid_cents
            row["due"] += invoice.balance_cents
    return sorted(rows.values(), key=lambda r: r["issuer"].name)


@bp.route("/")
@login_required
def index():
    notifications = db.session.scalars(
        db.select(Notification)
        .where(
            Notification.account_id == current_user.id,
            Notification.read.is_(False),
            Notification.dismissed_at.is_(None),
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(RECENT_NOTIFICATIONS)
    ).all()
    context = {"notifications": notifications, "year": date.today().year}
    if can(current_user, "invoice.view"):
        context.update(
            summary=_year_summary(date.today().year),
            drafts=db.session.scalars(
                db.select(Invoice).where(Invoice.status == Invoice.DRAFT).order_by(Invoice.updated_at.desc()).limit(10)
            ).all(),
            overdue=reminders.overdue()[:15],
            no_issuer=not billing.issuers(),
        )
    return render_template("dashboard/index.html", **context)
