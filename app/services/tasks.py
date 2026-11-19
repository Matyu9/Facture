"""Tâches périodiques, lancées par `flask run-tasks` (service scheduler)."""

from ..extensions import db
from . import accounts, activity, mailer, reminders, sessions, throttle


def run(now=None):
    """Lance les tâches et renvoie un résumé lisible."""
    logins = activity.purge_expired_logins(now)
    attempts = throttle.purge(now)
    links = accounts.purge_expired(now)
    old_sessions = sessions.purge_expired(now)
    overdue = reminders.notify_overdue(now)
    db.session.commit()
    emails = mailer.deliver_pending()
    return (
        f"{logins} connexion(s), {attempts} tentative(s) et {links} lien(s) expiré(s) effacé(s), "
        f"{old_sessions} session(s) trop ancienne(s) fermée(s), {overdue} facture(s) passée(s) en retard signalée(s), "
        f"{emails} email(s) envoyé(s)."
    )
