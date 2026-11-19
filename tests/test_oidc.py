"""Connexion OIDC (Adhérents), avec un faux fournisseur qui signe de vrais jetons : id_token (signature, iss, aud,
nonce vérifiés) et avis de déconnexion. Repris des tests de l'Inventaire."""

import html
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from app import create_app
from app.extensions import db
from app.models import Account, LoginSession, Role

from .conftest import Client, config_for, login, make_account, release, reset_database, stamp_head

ISSUER = "https://adherents.test"
CLIENT_ID = "facture"
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})
EVENT = "http://schemas.openid.net/event/backchannel-logout"
ALICE = {
    "sub": "alice-1",
    "email": "alice@asso.test",
    "email_verified": True,
    "given_name": "Alice",
    "family_name": "Martin",
    "groups": ["tresorerie"],
}


class FakeIdp:
    def __init__(self, client):
        self.client = client
        self.claims = {}
        self.userinfo = None
        client.server_metadata.update(
            {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/oidc/authorize",
                "token_endpoint": f"{ISSUER}/oidc/token",
                "userinfo_endpoint": f"{ISSUER}/oidc/userinfo",
                "end_session_endpoint": f"{ISSUER}/oidc/logout",
                "jwks": {"keys": [KEY.as_dict(private=False)]},
                "_loaded_at": time.time(),  # pas de requête réseau vers la découverte
            }
        )

    def fetch_access_token(self, **_kwargs):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, **self.claims}
        return {
            "access_token": "at",
            "token_type": "Bearer",
            "id_token": jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY),
        }

    def fetch_userinfo(self, **_kwargs):
        return self.userinfo or {}


def make_app(tmp_path, monkeypatch, **overrides):
    settings = {
        "BASE_URL": "https://facture.test",
        "OIDC_DISCOVERY_URL": f"{ISSUER}/.well-known/openid-configuration",
        "OIDC_CLIENT_ID": CLIENT_ID,
        "OIDC_CLIENT_SECRET": "secret",
        "OIDC_ADMIN_GROUPS": ["facture-admins"],
        **overrides,
    }
    app = create_app(config_for(tmp_path, **settings))
    app.test_client_class = Client
    idp = FakeIdp(app.extensions["oidc_client"])
    monkeypatch.setattr(idp.client, "fetch_access_token", idp.fetch_access_token)
    monkeypatch.setattr(idp.client, "userinfo", idp.fetch_userinfo)
    app.idp = idp
    return app


@pytest.fixture
def oidc_app(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with app.app_context():
        reset_database()
        stamp_head()
        # Les membres du groupe « tresorerie » d'Adhérents sont gestionnaires ici.
        db.session.add(
            Role(
                key=Role.GESTIONNAIRE,
                name="Gestionnaire",
                oidc_groups="tresorerie",
                permissions=["invoice.view", "invoice.edit", "invoice.issue", "payment.edit"],
            )
        )
        db.session.commit()
        yield app
        release()


@pytest.fixture
def idp(oidc_app):
    return oidc_app.idp


@pytest.fixture
def oclient(oidc_app):
    return oidc_app.test_client()


def provider_url(response):
    match = re.search(r'http-equiv="refresh" content="0;url=([^"]+)"', response.get_data(as_text=True))
    assert response.status_code == 200 and match, response.get_data(as_text=True)
    return html.unescape(match.group(1))


def oidc_login(client, idp, claims):
    query = parse_qs(urlsplit(provider_url(client.get("/connexion/oidc"))).query)
    idp.claims = {"nonce": query["nonce"][0], **claims}
    return client.get("/connexion/oidc/retour?" + urlencode({"code": "abc", "state": query["state"][0]}))


def by_sub(sub):
    return db.session.scalar(db.select(Account).filter_by(oidc_sub=sub))


def flashes(client):
    return client.get("/connexion").get_data(as_text=True)


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------


def test_disabled_without_configuration(client):
    assert client.get("/connexion/oidc").status_code == 404
    assert "Se connecter avec" not in client.get("/connexion").get_data(as_text=True)


def test_login_page_offers_the_provider(oclient):
    assert "Se connecter avec Adhérents" in oclient.get("/connexion").get_data(as_text=True)


def test_authorize_redirect_uses_pkce(oclient):
    url = provider_url(oclient.get("/connexion/oidc"))
    query = parse_qs(urlsplit(url).query)
    assert url.startswith(f"{ISSUER}/oidc/authorize") and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://facture.test/connexion/oidc/retour"]


def test_first_login_creates_the_account_with_the_role_of_its_groups(oclient, idp):
    response = oidc_login(oclient, idp, ALICE)
    account = by_sub("alice-1")
    assert response.status_code == 302 and account.role == Role.GESTIONNAIRE and account.role_from_oidc
    assert account.password_hash is None and account.email_verified
    assert oclient.get("/factures").status_code == 200
    login_session = db.session.scalar(db.select(LoginSession))
    assert login_session.oidc_id_token.count(".") == 2


def test_no_second_factor_asked_for_an_account_without_password(oclient, idp):
    oidc_login(oclient, idp, ALICE)
    assert oclient.get("/").status_code == 200  # pas de renvoi vers la configuration du TOTP


def test_admin_group_gives_admin(oclient, idp):
    oidc_login(oclient, idp, {**ALICE, "groups": ["facture-admins", "tresorerie"]})
    assert by_sub("alice-1").role == Role.ADMIN


def test_without_matching_group_access_is_refused(oclient, idp):
    oidc_login(oclient, idp, {**ALICE, "groups": ["chorale"]})
    assert by_sub("alice-1") is None
    assert "pas accès à la facturation" in flashes(oclient)


def test_default_role_lets_anyone_in(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, OIDC_DEFAULT_ROLE=Role.LECTEUR)
    with app.app_context():
        reset_database()
        oidc_login(app.test_client(), app.idp, {**ALICE, "groups": []})
        assert by_sub("alice-1").role == Role.LECTEUR
        release()


def test_losing_the_group_cuts_the_account(oclient, idp):
    oidc_login(oclient, idp, ALICE)
    other = oidc_app_client(oclient)
    oidc_login(other, idp, {**ALICE, "groups": []})
    account = by_sub("alice-1")
    assert not account.active and "plus accès" in flashes(other)


def oidc_app_client(client):
    return client.application.test_client()


def test_manual_role_is_kept_without_matching_group(oclient, idp):
    account = make_account(Role.LECTEUR, email="alice@asso.test")
    oidc_login(oclient, idp, {**ALICE, "groups": []})
    account = db.session.get(Account, account.id)
    assert account.oidc_sub == "alice-1" and account.role == Role.LECTEUR and account.active


def test_existing_account_is_linked_by_verified_email_only(oclient, idp):
    make_account(Role.LECTEUR, email="alice@asso.test")
    oidc_login(oclient, idp, {**ALICE, "email_verified": False})
    assert by_sub("alice-1") is None and "pas vérifié" in flashes(oclient)


def test_cut_account_stays_cut(oclient, idp):
    make_account(Role.LECTEUR, email="alice@asso.test", active=False)
    oidc_login(oclient, idp, ALICE)
    assert "coupé" in flashes(oclient) and oclient.get("/factures").status_code == 302


def test_forged_signature_is_refused(oclient, idp, monkeypatch):
    original = idp.fetch_access_token

    def forged(**kwargs):
        token = original(**kwargs)
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, **idp.claims}
        token["id_token"] = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, OTHER_KEY)
        return token

    monkeypatch.setattr(idp.client, "fetch_access_token", forged)
    oidc_login(oclient, idp, ALICE)
    assert by_sub("alice-1") is None


def test_password_form_hidden_when_oidc_only(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, OIDC_ONLY=True)
    with app.app_context():
        reset_database()
        client = app.test_client()
        page = client.get("/connexion").get_data(as_text=True)
        assert 'name="password"' not in page and client.post("/connexion", data={}).status_code == 404
        admin = make_account(Role.ADMIN, email="admin@asso.test")
        assert 'name="password"' in client.get("/connexion?local=1").get_data(as_text=True)
        reader = make_account(Role.LECTEUR, email="lecteur@asso.test")
        refused = client.post("/connexion?local=1", data={"email": reader.email, "password": "motdepasse"})
        assert refused.status_code == 200
        assert login(client, admin, query_string={"local": "1"}).status_code == 302
        release()


# ---------------------------------------------------------------------------
# Déconnexion
# ---------------------------------------------------------------------------


def logout_token(claims=None, key=KEY):
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + 120,
        "jti": f"j{time.time_ns()}",
        "sub": ALICE["sub"],
        "events": {EVENT: {}},
        **(claims or {}),
    }
    return jwt.encode({"alg": "RS256", "kid": "k1", "typ": "logout+jwt"}, body, key)


def test_backchannel_logout_closes_the_sessions_it_names(oclient, idp):
    oidc_login(oclient, idp, {**ALICE, "sid": "s-1"})
    response = oclient.application.test_client().post(
        "/connexion/oidc/deconnexion", data={"logout_token": logout_token({"sid": "s-1"})}
    )
    assert response.status_code == 200 and db.session.scalar(db.select(LoginSession)) is None
    assert oclient.get("/factures").status_code == 302


@pytest.mark.parametrize(
    "claims, key",
    [
        ({"iss": "https://pirate.test"}, KEY),
        ({"aud": "autre"}, KEY),
        ({"iat": int(time.time()) - 3600}, KEY),
        ({"nonce": "x"}, KEY),
        ({"events": {}}, KEY),
        ({}, OTHER_KEY),
    ],
)
def test_backchannel_logout_refuses_bad_tokens(oclient, idp, claims, key):
    oidc_login(oclient, idp, {**ALICE, "sid": "s-1"})
    response = oclient.application.test_client().post(
        "/connexion/oidc/deconnexion", data={"logout_token": logout_token(claims, key)}
    )
    assert response.status_code == 400 and db.session.scalar(db.select(LoginSession)) is not None


def test_logout_is_passed_on_to_the_provider(oclient, idp):
    oidc_login(oclient, idp, ALICE)
    page = oclient.post("/deconnexion")
    target = provider_url(page)
    query = parse_qs(urlsplit(target).query)
    assert target.startswith(f"{ISSUER}/oidc/logout")
    assert query["post_logout_redirect_uri"] == ["https://facture.test/connexion"]
    assert db.session.scalar(db.select(LoginSession)) is None


def test_role_form_saves_oidc_groups(oclient, idp):
    admin = make_account(Role.ADMIN, email="chef@asso.test")
    login(oclient, admin)
    oclient.post("/roles/lecteur/modifier", data={"name": "Lecteur", "oidc_groups": "bureau, ca,bureau"})
    assert db.session.get(Role, Role.LECTEUR).oidc_groups == "bureau, ca"
