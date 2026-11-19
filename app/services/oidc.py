"""Connexion OIDC (facultative) : par Adhérents, ou tout fournisseur OpenID Connect.

Rattachement de l'identité à un compte local, rôle déduit des groupes, avis de déconnexion du fournisseur
(OpenID Connect Back-Channel Logout 1.0) et déconnexion transmise au fournisseur (RP-Initiated Logout 1.0).
Repris de l'Inventaire, adapté aux comptes de Facture.
"""

import time

from authlib.common.urls import add_params_to_uri
from authlib.integrations.flask_client import OAuth
from flask import current_app, url_for
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet
from joserfc.jws import JWSRegistry

from ..extensions import db
from ..models import Account, LoginSession, Role
from ..permissions import permissions_of
from .activity import log

LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"
# Un avis plus ancien (ou daté du futur) est refusé : il a pu être intercepté puis rejoué.
LOGOUT_MAX_AGE = 5 * 60
# Signatures acceptées : jamais « none » ni une signature symétrique, qu'un client pourrait fabriquer.
ASYMMETRIC = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
# Une clé inconnue fait recharger la liste des clés, mais pas plus d'une fois par minute.
JWKS_REFRESH_EVERY = 60
_last_jwks_refresh = [0.0]


class OidcRefused(Exception):
    """Connexion refusée : le message est affiché tel quel à l'utilisateur."""


class InvalidLogoutToken(Exception):
    """Avis de déconnexion refusé : le motif part dans les journaux, pas au fournisseur."""


def init_oidc(app):
    if not enabled(app):
        return
    oauth = OAuth(app)
    app.extensions["oidc_client"] = oauth.register(
        "oidc",
        server_metadata_url=app.config["OIDC_DISCOVERY_URL"],
        client_id=app.config["OIDC_CLIENT_ID"],
        client_secret=app.config["OIDC_CLIENT_SECRET"] or None,
        client_kwargs={"scope": app.config["OIDC_SCOPES"], "code_challenge_method": "S256"},
    )


def enabled(app=None):
    config = (app or current_app).config
    return bool(config.get("OIDC_DISCOVERY_URL") and config.get("OIDC_CLIENT_ID"))


def client():
    return current_app.extensions["oidc_client"]


def password_login_enabled():
    return not (enabled() and current_app.config["OIDC_ONLY"])


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------


def _roles_with_groups():
    return db.session.scalars(db.select(Role).where(Role.oidc_groups.is_not(None), Role.oidc_groups != "")).all()


def role_from_groups(groups):
    """Rôle donné par les groupes OIDC, None si aucun ne correspond.

    Groupes du .env (OIDC_ADMIN_GROUPS) et groupes saisis sur chaque rôle : l'administrateur l'emporte, puis le rôle
    qui a le plus de droits.
    """
    if isinstance(groups, str):
        groups = [groups]
    groups = {g for g in (groups or []) if isinstance(g, str)}
    candidates = set()
    if groups & set(current_app.config["OIDC_ADMIN_GROUPS"]):
        candidates.add(Role.ADMIN)
    candidates |= {r.key for r in _roles_with_groups() if groups & set(r.oidc_group_list)}
    if not candidates:
        return None
    if Role.ADMIN in candidates:
        return Role.ADMIN
    return max(sorted(candidates), key=lambda key: len(permissions_of(key)))


def _uses_groups():
    return bool(current_app.config["OIDC_ADMIN_GROUPS"] or _roles_with_groups())


def resolve_account(claims):
    """Retrouve (ou crée) le compte correspondant aux claims OIDC et synchronise son rôle (sans commit)."""
    sub = claims.get("sub")
    if not sub or not isinstance(sub, str):
        raise OidcRefused("Le fournisseur d'identité n'a pas transmis d'identifiant.")
    email = (claims.get("email") or "").strip().lower() or None
    email_trusted = bool(email) and (
        claims.get("email_verified") is True or not current_app.config["OIDC_REQUIRE_VERIFIED_EMAIL"]
    )
    groups = claims.get(current_app.config["OIDC_GROUPS_CLAIM"])

    account = db.session.scalar(db.select(Account).filter_by(oidc_sub=sub))
    if account is None and email:
        by_email = db.session.scalar(db.select(Account).where(Account.email == email))
        if by_email is not None:
            if not current_app.config["OIDC_LINK_BY_EMAIL"]:
                raise OidcRefused(
                    "Un compte existe déjà avec cet email : demandez à un administrateur de le rattacher."
                )
            if not email_trusted:
                raise OidcRefused(
                    "Un compte existe déjà avec cet email, mais le fournisseur d'identité ne l'a pas vérifié : "
                    "demandez à un administrateur de le rattacher."
                )
            if by_email.oidc_sub:
                raise OidcRefused("Ce compte est déjà rattaché à une autre identité.")
            account = by_email
            account.oidc_sub = sub
            account.email_verified = True
            log("account.oidc_link", f"Compte rattaché à l'identité OIDC : {account.full_name}", user=account)

    if account is None:
        if not email_trusted:
            raise OidcRefused("Le fournisseur d'identité n'a pas transmis d'adresse email vérifiée.")
        role = role_from_groups(groups) or current_app.config["OIDC_DEFAULT_ROLE"] or None
        if role is None or (role not in Role.BUILTIN and db.session.get(Role, role) is None):
            raise OidcRefused("Vous n'avez pas accès à la facturation : demandez-le à un responsable de l'association.")
        account = _create(claims, sub, email, role)

    if not account.active:
        raise OidcRefused("Ce compte est coupé : contactez un responsable de l'association.")
    if account.must_change_password:
        # Le mot de passe provisoire (connu de qui a créé le compte) ne sert plus : la connexion passe par l'OIDC.
        account.password_hash = None
        account.must_change_password = False
    _sync_role(account, groups)
    return account


def _create(claims, sub, email, role):
    first = (claims.get("given_name") or "").strip()
    last = (claims.get("family_name") or "").strip()
    if not (first and last):
        parts = (claims.get("name") or claims.get("preferred_username") or "").strip().split(None, 1)
        first = first or (parts[0] if parts else "") or "Utilisateur"
        last = last or (parts[1] if len(parts) > 1 else "") or "OIDC"
    account = Account(
        first_name=first[:80],
        last_name=last[:80],
        email=email,
        email_verified=True,
        role=role,
        role_from_oidc=_uses_groups(),
        active=True,
        oidc_sub=sub,
    )
    db.session.add(account)
    db.session.flush()
    log("account.create", f"Compte créé à la connexion OIDC : {account.full_name}", user=account, target=account)
    return account


def _sync_role(account, groups):
    if not _uses_groups():
        return
    target = role_from_groups(groups)
    if target is None:
        if not account.role_from_oidc:
            return  # rôle choisi à la main : on n'y touche pas
        # Plus aucun groupe : le rôle donné par les groupes est retiré.
        target = current_app.config["OIDC_DEFAULT_ROLE"] or None
        if target is None:
            account.active = False
            log("account.cut", f"Compte coupé : {account.full_name} n'a plus de groupe donnant accès", user=account)
            raise OidcRefused(
                "Vous n'avez plus accès à la facturation : demandez-le à un responsable de l'association."
            )
    account.role_from_oidc = True
    if account.role != target:
        old = account.role_label
        account.role = target
        log(
            "account.role",
            f"Rôle de {account.full_name} : {old} → {account.role_label} (groupes OIDC)",
            user=account,
            target=account,
        )


# ---------------------------------------------------------------------------
# Déconnexion
# ---------------------------------------------------------------------------


def _key_set(force=False):
    return KeySet.import_key_set(client().fetch_jwk_set(force=force))


def _decode(raw, keys):
    return jwt.decode(raw, keys, registry=JWSRegistry(algorithms=ASYMMETRIC, strict_check_header=False))


def verify_logout_token(raw):
    """Contenu d'un avis de déconnexion vérifié comme le demande la norme (section 2.6), sinon InvalidLogoutToken."""
    if not raw or not isinstance(raw, str) or len(raw) > 16384:
        raise InvalidLogoutToken("logout_token absent")
    try:
        try:
            token = _decode(raw, _key_set())
        except InvalidKeyIdError:  # clé renouvelée chez le fournisseur : recharger, sans le laisser imposer le rythme
            if time.monotonic() - _last_jwks_refresh[0] < JWKS_REFRESH_EVERY:
                raise
            _last_jwks_refresh[0] = time.monotonic()
            token = _decode(raw, _key_set(force=True))
    except (JoseError, ValueError, TypeError) as error:
        raise InvalidLogoutToken(f"signature invalide ({error.__class__.__name__})") from error
    header, claims = token.header, token.claims
    typ = str(header.get("typ") or "").lower()
    if typ and typ not in ("jwt", "logout+jwt", "application/logout+jwt"):
        raise InvalidLogoutToken(f"typ inattendu : {typ}")
    metadata = client().load_server_metadata()
    if claims.get("iss") != metadata.get("issuer"):
        raise InvalidLogoutToken("iss inattendu")
    audience = claims.get("aud")
    client_id = current_app.config["OIDC_CLIENT_ID"]
    if not (audience == client_id or (isinstance(audience, list) and client_id in audience)):
        raise InvalidLogoutToken("aud inattendu")
    now = time.time()
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)) or abs(now - iat) > LOGOUT_MAX_AGE:
        raise InvalidLogoutToken("iat absent ou trop éloigné")
    exp = claims.get("exp")
    if exp is not None and (not isinstance(exp, (int, float)) or exp < now):
        raise InvalidLogoutToken("avis expiré")
    events = claims.get("events")
    if not isinstance(events, dict) or not isinstance(events.get(LOGOUT_EVENT), dict):
        raise InvalidLogoutToken("événement de déconnexion absent")
    if "nonce" in claims:
        raise InvalidLogoutToken("nonce interdit")
    sub, sid = claims.get("sub"), claims.get("sid")
    if (sub is not None and not isinstance(sub, str)) or (sid is not None and not isinstance(sid, str)):
        raise InvalidLogoutToken("sub ou sid illisible")
    if not (sub or sid):
        raise InvalidLogoutToken("ni sub ni sid")
    return claims


def end_sessions(claims):
    """Ferme les sessions que désigne un avis vérifié. Renvoie leur nombre. Sans commit.

    Avec `sid`, seulement cette session-là (et seulement si elle appartient à `sub`, quand il est donné). Sans `sid`,
    toutes les sessions ouvertes via le fournisseur par ce compte : celles ouvertes par mot de passe ne le concernent pas.
    """
    sub, sid = claims.get("sub"), claims.get("sid")
    account = db.session.scalar(db.select(Account).filter_by(oidc_sub=sub)) if sub else None
    if sub and account is None:
        return 0
    query = db.select(LoginSession)
    if sid:
        query = query.where(LoginSession.oidc_sid == sid)
        if account is not None:
            query = query.where(LoginSession.account_id == account.id)
    else:
        query = query.where(LoginSession.account_id == account.id, LoginSession.oidc_id_token.is_not(None))
    closed = db.session.scalars(query).all()
    for login in closed:
        log(
            "auth.logout",
            f"Déconnexion de {login.account.full_name} par le fournisseur d'identité",
            user=login.account,
        )
        db.session.delete(login)
    return len(closed)


def logout_url(login):
    """Adresse où envoyer la personne pour qu'elle soit aussi déconnectée du fournisseur, sinon None.

    Seulement pour une session ouverte via le fournisseur, et s'il sait le faire (`end_session_endpoint`). Le retour
    se fait sur la page de connexion, qu'il faut déclarer chez lui comme adresse après déconnexion.
    """
    config = current_app.config
    if login is None or not login.oidc_id_token or not enabled() or not config["OIDC_PROVIDER_LOGOUT"]:
        return None
    try:
        endpoint = client().load_server_metadata().get("end_session_endpoint")
    except Exception:  # noqa: BLE001  fournisseur injoignable : la déconnexion locale suffit
        current_app.logger.warning("Découverte OIDC injoignable : déconnexion locale seulement", exc_info=True)
        return None
    if not endpoint:
        return None
    return add_params_to_uri(
        endpoint,
        [
            ("id_token_hint", login.oidc_id_token),
            ("client_id", config["OIDC_CLIENT_ID"]),
            ("post_logout_redirect_uri", config["BASE_URL"] + url_for("auth.login")),
        ],
    )
