"""Jetons d'API : une appli (Adhérents) crée et lit des factures. Seule l'empreinte du jeton est gardée."""

import hashlib
import secrets
from datetime import datetime, timedelta

from ..extensions import db
from ..models import ApiToken

PREFIX = "fct_"
# « Dernière utilisation » n'est réécrite qu'au plus toutes les 5 minutes : pas d'écriture à chaque appel.
TOUCH_EVERY = timedelta(minutes=5)


def _digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def create(name, can_issue=False, by=None):
    """Crée un jeton et renvoie (jeton, texte en clair). Le texte n'est montré qu'une fois. Sans commit."""
    raw = PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(
        name=name.strip()[:80], token_hash=_digest(raw), prefix=raw[:12], can_issue=bool(can_issue), created_by=by
    )
    db.session.add(token)
    return token, raw


def find(raw):
    """Jeton actif correspondant au texte reçu, sinon None."""
    if not raw or not raw.startswith(PREFIX) or len(raw) > 100:
        return None
    token = db.session.scalar(db.select(ApiToken).filter_by(token_hash=_digest(raw)))
    if token is None or not token.active:
        return None
    now = datetime.now()
    if token.last_used_at is None or now - token.last_used_at >= TOUCH_EVERY:
        token.last_used_at = now
        db.session.commit()
    return token


def revoke(token):
    token.revoked_at = datetime.now()


def all_tokens():
    return db.session.scalars(db.select(ApiToken).order_by(ApiToken.revoked_at.is_not(None), ApiToken.name)).all()
