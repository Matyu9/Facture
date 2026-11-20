import contextlib
import os
from pathlib import Path

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import UNSAFE_SECRETS, Config, startup_warnings
from .errors import register_error_handlers
from .extensions import csrf, db, login_manager, migrate
from .services import branding

__version__ = "0.4.3"

# Aucun script ni style en ligne : les comportements passent par app.js (data-confirm, data-autosubmit…).
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; report-uri /csp-report; report-to csp-endpoint"
)
# Pages qui affichent ou reçoivent des données de compte : jamais gardées par le navigateur ou un proxy.
NO_STORE = (
    "auth.login",
    "auth.password",
    "auth.two_factor",
    "auth.forgot",
    "auth.token_link",
    "auth.oidc_callback",
    "account.index",
    "account.change_email",
    "account.totp",
    "account.totp_codes",
    "account.totp_disable",
    "accounts.create",
    "accounts.invite",
    "accounts.detail",
    "api_tokens.index",
)


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    if app.config["SECRET_KEY"] in ("", *UNSAFE_SECRETS) and not (
        app.debug or app.testing or os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true")
    ):
        raise RuntimeError(
            "SECRET_KEY n'est pas définie (ou vaut encore la valeur d'exemple) : "
            "renseignez-la dans le fichier .env avant de démarrer."
        )
    if app.config.get("TRUST_PROXY"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["ARCHIVE_DIR"]).mkdir(parents=True, exist_ok=True)
    for warning in startup_warnings(app.config):
        app.logger.warning(warning)

    db.init_app(app)
    migrate.init_app(app, db)
    from . import models  # noqa: F401  (enregistre les modèles pour Alembic)

    csrf.init_app(app)
    login_manager.init_app(app)

    from .blueprints.account import bp as account_bp
    from .blueprints.accounts import bp as accounts_bp
    from .blueprints.api import bp as api_bp
    from .blueprints.api_tokens import bp as api_tokens_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.csp import bp as csp_bp
    from .blueprints.customers import bp as customers_bp
    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.health import bp as health_bp
    from .blueprints.invoices import bp as invoices_bp
    from .blueprints.issuers import bp as issuers_bp
    from .blueprints.journal import bp as journal_bp
    from .blueprints.notifications import bp as notifications_bp
    from .blueprints.products import bp as products_bp
    from .blueprints.quotes import bp as quotes_bp
    from .blueprints.roles import bp as roles_bp
    from .blueprints.settings import bp as settings_bp
    from .cli import register_cli
    from .filters import register_filters
    from .permissions import can
    from .services import oidc
    from .services.notify import unread_count

    for blueprint in (
        auth_bp,
        dashboard_bp,
        invoices_bp,
        quotes_bp,
        customers_bp,
        products_bp,
        issuers_bp,
        account_bp,
        accounts_bp,
        notifications_bp,
        journal_bp,
        roles_bp,
        settings_bp,
        api_tokens_bp,
        health_bp,
        csp_bp,
        api_bp,
    ):
        app.register_blueprint(blueprint)
    oidc.init_oidc(app)
    register_cli(app)
    register_filters(app)

    @app.url_defaults
    def static_version(endpoint, values):
        """?v=<date du fichier> sur les fichiers statiques : après une mise à jour, le navigateur charge la
        nouvelle version au lieu de celle de son cache."""
        if endpoint == "static" and "filename" in values and "v" not in values:
            with contextlib.suppress(OSError):
                values["v"] = int((Path(app.static_folder) / values["filename"]).stat().st_mtime)

    # Aussi pour les macros importées sans contexte.
    from flask_login import current_user as _current_user

    app.jinja_env.globals.update(can=can, current_user=_current_user)

    @app.context_processor
    def inject_globals():
        brand = branding.current()
        return {
            "can": can,
            "branding": brand,
            "asso_name": brand.name,
            "unread_notifications": unread_count,
        }

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # Aucune autre origine ne peut garder une référence à nos fenêtres, ni charger nos réponses.
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        # Espace privé : rien à indexer, même si une adresse fuit.
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        if request.is_secure:  # en HTTPS (derrière le proxy : TRUST_PROXY=1), le navigateur n'essaiera plus HTTP
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        # Une réponse qui pose sa propre CSP la garde telle quelle.
        if "Content-Security-Policy" not in response.headers:
            response.headers.setdefault(
                "Content-Security-Policy-Report-Only" if app.config["CSP_REPORT_ONLY"] else "Content-Security-Policy",
                CSP,
            )
            response.headers.setdefault("Reporting-Endpoints", 'csp-endpoint="/csp-report"')  # pour report-to
        if request.endpoint in NO_STORE:
            response.headers["Cache-Control"] = "no-store"
        return response

    register_error_handlers(app)

    return app
