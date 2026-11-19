"""Garde-fou des sessions : une session qui voit les clients et les factures se ferme après IDLE_TIMEOUT_HOURS sans
activité, même avec « rester connecté »."""

from datetime import datetime, timedelta

from flask import current_app

# Droits qui ouvrent des données personnelles (adresses des clients, familles comprises).
SENSITIVE_RIGHTS = ("invoice.view",)


def handles_personal_data(account):
    from ..permissions import can

    return any(can(account, right) for right in SENSITIVE_RIGHTS)


def idle_expired(login, now=None):
    """Vrai si cette session doit se fermer : compte qui voit des données personnelles, inactif trop longtemps."""
    hours = current_app.config.get("IDLE_TIMEOUT_HOURS", 8)
    last = login.last_seen_at or login.created_at
    return (
        bool(hours) and last < (now or datetime.now()) - timedelta(hours=hours) and handles_personal_data(login.account)
    )
