import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SECRET = "dev-secret-change-me"
# Valeurs connues de tous (défaut du code, modèle .env.example) : refusées en production.
UNSAFE_SECRETS = (DEFAULT_SECRET, "change-moi")


def _csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY") or DEFAULT_SECRET
    DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    # PostgreSQL. Par défaut, la base « facture » de la machine, par le socket local (développement).
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or "postgresql+psycopg:///facture"
    # Connexions vérifiées avant usage : un redémarrage de PostgreSQL ne fait pas échouer la requête suivante.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_DIR = DATA_DIR / "uploads"
    # PDF des factures émises : écrits une fois, jamais modifiés (voir services/archive.py).
    ARCHIVE_DIR = DATA_DIR / "factures"
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024

    BASE_URL = os.environ.get("BASE_URL", "http://localhost:8002").rstrip("/")
    # Derrière un reverse proxy (Caddy, Traefik, nginx) : faire confiance à X-Forwarded-*.
    TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
    # Noms propres à l'appli : sur localhost, les cookies ne sont pas isolés par port, et ceux d'Adhérents ou de
    # l'Inventaire écraseraient ceux-ci (la connexion OIDC entre les applis échouerait).
    SESSION_COOKIE_NAME = "facture_session"
    REMEMBER_COOKIE_NAME = "facture_remember"
    SESSION_COOKIE_SAMESITE = "Lax"
    WTF_CSRF_TIME_LIMIT = None
    SESSION_COOKIE_SECURE = BASE_URL.startswith("https://")
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True
    ASSO_NAME = os.environ.get("ASSO_NAME", "Mon association")

    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "")
    SMTP_TLS = os.environ.get("SMTP_TLS", "true").lower() == "true"
    # Envoi hors de la requête, là où sa durée révélerait si un compte existe (mot de passe oublié).
    MAIL_IN_BACKGROUND = True

    # Connexion OIDC (facultative) : active dès que l'URL de découverte et le client sont renseignés. Prévue pour
    # Adhérents, mais tout fournisseur OpenID Connect convient.
    OIDC_DISCOVERY_URL = os.environ.get("OIDC_DISCOVERY_URL", "").strip()
    OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "").strip()
    OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
    OIDC_PROVIDER_NAME = os.environ.get("OIDC_PROVIDER_NAME", "Adhérents")
    OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid email profile groups")
    OIDC_GROUPS_CLAIM = os.environ.get("OIDC_GROUPS_CLAIM", "groups")
    # Groupes du fournisseur qui donnent le rôle administrateur (les autres rôles se règlent dans l'écran Rôles).
    OIDC_ADMIN_GROUPS = _csv(os.environ.get("OIDC_ADMIN_GROUPS", ""))
    # Rôle d'un compte créé à sa première connexion OIDC quand aucun groupe ne correspond. Vide : connexion refusée.
    OIDC_DEFAULT_ROLE = os.environ.get("OIDC_DEFAULT_ROLE", "").strip()
    OIDC_REQUIRE_VERIFIED_EMAIL = os.environ.get("OIDC_REQUIRE_VERIFIED_EMAIL", "1") == "1"
    # Rattache une identité OIDC au compte local de même email (vérifiée par le fournisseur).
    OIDC_LINK_BY_EMAIL = os.environ.get("OIDC_LINK_BY_EMAIL", "1") == "1"
    # Masque le formulaire email / mot de passe. Un administrateur garde l'accès de secours par /connexion?local=1.
    OIDC_ONLY = os.environ.get("OIDC_ONLY", "0") == "1"
    # Déconnexion transmise au fournisseur (RP-Initiated Logout), pour une session ouverte par lui.
    OIDC_PROVIDER_LOGOUT = os.environ.get("OIDC_PROVIDER_LOGOUT", "1") == "1"

    # Garde-fous : inactivité (heures, 0 : jamais) d'une session qui voit les clients et les factures.
    IDLE_TIMEOUT_HOURS = float(os.environ.get("IDLE_TIMEOUT_HOURS", "8"))

    # Content-Security-Policy : appliquée par défaut. « 1 » = Report-Only, pour diagnostiquer sans rien bloquer.
    CSP_REPORT_ONLY = os.environ.get("CSP_REPORT_ONLY", "0") == "1"


def startup_warnings(config):
    """Réglages incohérents qui n'empêchent pas de démarrer mais méritent d'être signalés dans les journaux."""
    warnings = []
    if (config.get("BASE_URL") or "").startswith("https://") and not config.get("TRUST_PROXY"):
        warnings.append(
            "BASE_URL est en https:// mais TRUST_PROXY n'est pas à 1 : derrière un reverse proxy, l'adresse des "
            "visiteurs et le protocole (cookies Secure, redirections) dépendent des en-têtes X-Forwarded-*, "
            "ignorés sans TRUST_PROXY=1."
        )
    discovery = urlsplit(config.get("OIDC_DISCOVERY_URL") or "https://non-renseigne")
    if discovery.scheme != "https" and discovery.hostname not in ("localhost", "127.0.0.1", "::1"):
        warnings.append("OIDC_DISCOVERY_URL n'est pas en https:// : le secret du client circulerait en clair.")
    return warnings


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL") or "postgresql+psycopg:///facture_test"
    WTF_CSRF_ENABLED = False
    MAIL_IN_BACKGROUND = False
