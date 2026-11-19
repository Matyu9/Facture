"""Réglages simples de l'appli, rangés dans AppSetting. Aucun n'a de valeur par défaut cachée : chaque fonction
dit ce que vaut un réglage absent."""

from ..extensions import db
from ..models import AppSetting


def get(key):
    setting = db.session.get(AppSetting, key)
    return setting.value if setting is not None else None


def put(key, value):
    """Enregistre la valeur (texte) ; None efface le réglage."""
    setting = db.session.get(AppSetting, key)
    if value is None:
        if setting is not None:
            db.session.delete(setting)
        return
    if setting is None:
        setting = AppSetting(key=key, value="")
        db.session.add(setting)
    setting.value = str(value)
