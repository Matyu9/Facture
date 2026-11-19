"""Balayage systématique : toutes les routes × tous les rôles, puis tous les liens de toutes les pages."""

import re
from collections import deque
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urljoin, urlsplit

import pytest

from app.extensions import db
from app.models import Customer, Invoice, Issuer, Notification, Product, Role
from app.services import api_tokens, billing
from app.services.activity import log

from .conftest import login, make_account

A, G, L = Role.ADMIN, Role.GESTIONNAIRE, Role.LECTEUR
ANONYMOUS = "anonyme"

# Qui a accès à quoi. Une route absente de ce tableau fait échouer les tests : tout nouvel écran doit se déclarer ici.
PUBLIC, LOGGED_IN, ADMIN_ONLY = "public", "connecté", "admin"
SETTINGS, JOURNAL, HIDDEN = "settings.manage", "activity.view", "caché"  # caché : 404 pour qui n'a pas settings.manage
OIDC_OFF = "oidc"  # 404 tant que la connexion OIDC n'est pas configurée
API = "api"  # jeton d'appli : 401 sans lui, pour tout le monde
ACCOUNTS, API_MANAGE = "account.manage", "api.manage"
VIEW, EDIT, ISSUE, PAY, ISSUER = "invoice.view", "invoice.edit", "invoice.issue", "payment.edit", "issuer.manage"
ACCESS = {
    "static": PUBLIC,
    "auth.login": PUBLIC,
    "csp.report": PUBLIC,
    "settings.logo_file": PUBLIC,
    "auth.two_factor": PUBLIC,
    "auth.forgot": PUBLIC,
    "auth.token_link": PUBLIC,
    "auth.oidc_login": OIDC_OFF,
    "auth.oidc_callback": OIDC_OFF,
    "auth.oidc_backchannel": OIDC_OFF,
    "auth.logout": LOGGED_IN,
    "auth.password": LOGGED_IN,
    "dashboard.index": LOGGED_IN,
    "account.index": LOGGED_IN,
    "account.display": LOGGED_IN,
    "account.revoke_session": LOGGED_IN,
    "account.revoke_others": LOGGED_IN,
    "account.change_email": LOGGED_IN,
    "account.verify_email": LOGGED_IN,
    "account.totp": LOGGED_IN,
    "account.totp_codes": LOGGED_IN,
    "account.totp_disable": LOGGED_IN,
    "notifications.index": LOGGED_IN,
    "notifications.mark_all_read": LOGGED_IN,
    "notifications.dismiss": LOGGED_IN,
    "notifications.dismiss_read": LOGGED_IN,
    "journal.index": JOURNAL,
    **{
        f"accounts.{name}": ACCOUNTS
        for name in (
            "index",
            "create",
            "detail",
            "edit",
            "invite",
            "cut",
            "restore",
            "reset_totp",
            "revoke_session",
            "revoke_sessions",
        )
    },
    "roles.index": ADMIN_ONLY,
    "roles.create": ADMIN_ONLY,
    "roles.edit": ADMIN_ONLY,
    "roles.delete": ADMIN_ONLY,
    "settings.index": SETTINGS,
    "settings.save_branding": SETTINGS,
    "settings.set_logo": SETTINGS,
    "settings.remove_logo": SETTINGS,
    "settings.test_email": SETTINGS,
    "settings.run_tasks": SETTINGS,
    "settings.revoke_sessions": SETTINGS,
    "api_tokens.index": API_MANAGE,
    "api_tokens.revoke": API_MANAGE,
    "health.as_json": HIDDEN,
    "health.page": HIDDEN,
    **{f"invoices.{name}": VIEW for name in ("index", "export", "detail", "pdf_file")},
    **{f"invoices.{name}": EDIT for name in ("create", "edit", "delete", "duplicate", "credit", "save_customer")},
    "invoices.issue": ISSUE,
    "invoices.send": ISSUE,
    "invoices.add_payment": PAY,
    "invoices.delete_payment": PAY,
    "quotes.index": VIEW,
    **{f"quotes.{name}": EDIT for name in ("create", "decide", "to_invoice")},
    **{f"customers.{name}": VIEW for name in ("index", "detail")},
    **{f"customers.{name}": EDIT for name in ("create", "edit", "archive", "delete")},
    "products.index": VIEW,
    **{f"products.{name}": EDIT for name in ("create", "edit", "archive")},
    **{f"issuers.{name}": VIEW for name in ("index", "detail", "logo_file", "preview")},
    **{f"issuers.{name}": ISSUER for name in ("create", "edit", "archive", "layout")},
    **{
        f"api.{name}": API
        for name in (
            "issuers",
            "invoices",
            "invoice",
            "invoice_pdf",
            "create_invoice",
            "issue",
            "send",
            "decide",
            "quote_to_invoice",
        )
    },
}
HOLDERS = {
    SETTINGS: {A},
    JOURNAL: {A, G},
    ADMIN_ONLY: {A},
    HIDDEN: {A},
    ACCOUNTS: {A},
    API_MANAGE: {A},
    VIEW: {A, G, L},
    EDIT: {A, G},
    ISSUE: {A, G},
    PAY: {A, G},
    ISSUER: {A},
}
ARGUMENTS = {
    "filename": "css/app.css",
    "key": "lecteur",
    "sid": "inconnue",
    "name": "absent",
    "account_id": 1,
    "token": "inconnu",
    "notification_id": 999,
    "invoice_id": 999,
    "payment_id": 999,
    "customer_id": 999,
    "product_id": 999,
    "issuer_id": 999,
    "quote_id": 999,
    "action": "accepter",
    "token_id": 999,
}
# Publiques, mais renvoient légitimement vers la connexion : rien en cours, ou lien inconnu.
SENDS_TO_LOGIN = {"auth.logout", "auth.two_factor", "auth.token_link"}


def url_of(app, rule):
    with app.test_request_context():
        defaults = rule.defaults or {}
        return rule.build({arg: defaults[arg] if arg in defaults else ARGUMENTS[arg] for arg in rule.arguments})[1]


def is_login_redirect(response):
    return response.status_code == 302 and urlsplit(response.location).path == "/connexion"


def test_every_route_is_declared(app):
    assert {rule.endpoint for rule in app.url_map.iter_rules()} == set(ACCESS)


def test_every_declared_permission_exists():
    from app.permissions import PERMISSIONS

    assert {SETTINGS, JOURNAL, ACCOUNTS, API_MANAGE, VIEW, EDIT, ISSUE, PAY, ISSUER} <= set(PERMISSIONS)


@pytest.mark.parametrize("who", [ANONYMOUS, L, G, A])
def test_access_matrix(app, accounts, who):
    problems = []
    for rule in app.url_map.iter_rules():
        policy = ACCESS[rule.endpoint]
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            client = app.test_client()  # nouvelle session à chaque fois : une route peut déconnecter
            if who != ANONYMOUS:
                login(client, accounts[who])
            response = client.open(url_of(app, rule), method=method)
            code = response.status_code
            label = f"{who} {method} {rule.rule} → {code}"
            allowed = who in HOLDERS.get(policy, ()) or policy == PUBLIC or (policy == LOGGED_IN and who != ANONYMOUS)
            if code >= 500:
                problems.append(f"{label} : erreur serveur")
            elif policy == OIDC_OFF:
                if code != 404:
                    problems.append(f"{label} : devrait répondre 404 (OIDC non configuré)")
            elif policy == API:
                if code != 401:
                    problems.append(f"{label} : devrait répondre 401 (jeton d'API exigé)")
            elif allowed and (code == 403 or is_login_redirect(response) and rule.endpoint not in SENDS_TO_LOGIN):
                problems.append(f"{label} : devrait être autorisé")
            elif not allowed and policy == HIDDEN and code != 404:
                problems.append(f"{label} : devrait répondre 404")
            elif not allowed and policy != HIDDEN and who == ANONYMOUS and not is_login_redirect(response):
                problems.append(f"{label} : devrait demander la connexion")
            elif not allowed and policy != HIDDEN and who != ANONYMOUS and code != 403:
                problems.append(f"{label} : devrait répondre 403")
    assert not problems, "\n".join(problems)


def test_forbidden_post_changes_nothing(client, accounts):
    """Refusé, c'est refusé : pas seulement un code d'erreur par-dessus une action déjà faite."""
    login(client, accounts[L])
    assert client.post("/parametres/apparence", data={"name": "Piraté", "palette": "vert"}).status_code == 403
    assert client.post("/roles/nouveau", data={"name": "Piraté"}).status_code == 403
    assert client.post("/clients/nouveau", data={"kind": "structure", "name": "Piraté"}).status_code == 403
    assert db.session.get(Role, "pirate") is None
    assert db.session.scalar(db.select(Customer).filter_by(name="Piraté")) is None
    from app.services import branding

    assert branding.current().name != "Piraté"


@pytest.fixture
def rich(app, accounts):
    """Une base représentative : du HTML dans les noms, un rôle sur mesure, des pièces dans tous les états."""
    db.session.add(
        Role(key="regie", name="Régie <b>", description="Rôle sur <i>mesure</i>", permissions=["activity.view"])
    )
    make_account("regie", email="regie@asso.test", first_name="Léa <script>")
    issuer = Issuer(
        code="ecole",
        name="École <b>",
        legal_name="Association <b>Musique</b>",
        address="1 rue <i>du Port</i>",
        postcode="45000",
        city="Orléans",
        vat_regime=Issuer.SUBJECT,
        vat_number="FR12345678901",
        default_vat_rate=Decimal("20"),
        iban="FR7630006000011234567890189",
    )
    other = Issuer(
        code="orchestre",
        name="Orchestre",
        legal_name="Orchestre",
        address="2 rue",
        postcode="45000",
        city="Orléans",
        archived_at=date.today(),
    )
    customer = Customer(
        name="Mairie <b>de Gien</b>",
        address="Place <i>",
        postcode="45500",
        city="Gien",
        email="mairie@gien.test",
        notes="<script>",
    )
    family = Customer(
        kind=Customer.PERSON,
        name="Famille <b>Martin</b>",
        address="3 rue",
        postcode="45000",
        city="Orléans",
        source="adherents",
        external_id="foyer-1",
    )
    db.session.add_all([issuer, other, customer, family])
    db.session.add(Product(label="Concert <b>", unit_price_cents=80000, unit="forfait", issuer=issuer))
    db.session.flush()
    issued = billing.new_draft(issuer, customer, by=accounts[A])
    issued.subject = "Concert <b>du 14 juillet</b>"
    issued.service_date = date.today() - timedelta(days=40)
    billing.set_lines(
        issued,
        [
            billing.LineInput(
                label="Prestation <b>",
                quantity=Decimal("1"),
                unit_price_cents=100000,
                vat_rate=Decimal("20"),
                description="<i>2 heures</i>",
            )
        ],
    )
    billing.issue(issued, by=accounts[A], issue_date=date.today() - timedelta(days=40))
    db.session.flush()
    billing.add_payment(issued, 20000, date.today(), "cheque", reference="n° <b>12</b>")
    credit = billing.new_credit_note(issued)
    billing.set_lines(
        credit,
        [billing.LineInput(label="Remise <b>", quantity=Decimal("1"), unit_price_cents=10000, vat_rate=Decimal("20"))],
    )
    billing.issue(credit)
    draft = billing.new_draft(issuer, family)
    draft.service_date = date.today()
    billing.set_lines(draft, [billing.LineInput(label="Cotisation <b>", quantity=Decimal("1"), unit_price_cents=25000)])
    # Devis : en attente, refusé, et un autre passé en facture ; destinataire ponctuel.
    for decision in (None, Invoice.REFUSED, "facture"):
        quote = billing.new_draft(issuer, customer, kind=Invoice.QUOTE)
        quote.subject = "Devis <b>concert</b>"
        billing.set_lines(
            quote,
            [
                billing.LineInput(
                    label="Concert <b>", quantity=Decimal("1"), unit_price_cents=50000, vat_rate=Decimal("20")
                )
            ],
        )
        billing.issue(quote)
        db.session.flush()
        if decision == "facture":
            billing.invoice_from_quote(quote)
        elif decision:
            billing.decide(quote, decision)
    one_off = billing.new_draft(issuer, None, recipient={"name": "Jeanne <b>Durand</b>", "address": "4 rue"})
    billing.set_lines(one_off, [billing.LineInput(label="Cours", quantity=Decimal("1"), unit_price_cents=5000)])
    db.session.flush()
    log("invoice.issue", "Facture <em>émise</em>", user=accounts[A], invoice=issued, customer=customer)
    api_tokens.create("adherents <b>")
    for account in accounts.values():
        db.session.add(Notification(account_id=account.id, message="Facture <b>en retard</b>", link="/factures"))
        db.session.add(Notification(account_id=account.id, message="Sans lien", read=True))
    db.session.commit()
    return accounts


INLINE = re.compile(
    r"""<[a-z][^>]*\son[a-z]+\s*=|<[a-z][^>]*\sstyle\s*=|<style\b|<script\b(?![^>]*\ssrc=)|href\s*=\s*["']javascript:""",
    re.I,
)
LINK = re.compile(r"""(?:href|src)="([^"#]+)""")
UNESCAPED = (
    "<script>",
    "<b>en retard",
    "<em>émise",
    "École <b>",
    "Mairie <b>",
    "Famille <b>",
    "Concert <b>",
    "Prestation <b>",
    "Remise <b>",
    "Cotisation <b>",
    "Régie <b>",
    "adherents <b>",
)


@pytest.mark.parametrize("who", [L, G, A])
def test_crawl_every_link(app, rich, who):
    """Suit tous les liens depuis l'accueil : aucun lien mort, aucune erreur, et aucune page ne casserait sous la CSP."""
    client = app.test_client()
    login(client, rich[who])
    seen, queue, problems = set(), deque(["/"]), []
    while queue:
        path = queue.popleft()
        if path in seen:
            continue
        seen.add(path)
        response = client.get(path)
        if response.status_code != 200:
            problems.append(f"{path} → {response.status_code}")
            continue
        if response.mimetype != "text/html":
            continue
        html = response.get_data(as_text=True)
        if match := INLINE.search(html):
            problems.append(f"{path} : contenu en ligne interdit par la CSP : {match.group(0)!r}")
        problems += [f"{path} : « {text} » n'a pas été échappé" for text in UNESCAPED if text in html]
        for link in LINK.findall(html):
            target = urlsplit(urljoin(path, link.replace("&amp;", "&")))
            if not target.netloc and target.scheme not in ("data", "mailto", "tel"):
                queue.append(target.path + (f"?{target.query}" if target.query else ""))
    assert not problems, "\n".join(problems)
    assert "/mon-compte" in seen and any(p.startswith("/static/css/app.css") for p in seen)
    assert "/factures" in seen and "/clients" in seen and "/articles" in seen and "/emetteurs" in seen
    assert any(p.endswith("/pdf") for p in seen)
    assert any(p.startswith("/factures/export.csv") for p in seen)
    assert ("/factures/nouvelle" in seen) == (who in (A, G))
    assert any(p.endswith("/modifier") and p.startswith("/factures/") for p in seen) == (who in (A, G))
    assert any(p.startswith("/factures/") and "/envoyer" in p for p in seen) == (who in (A, G))
    assert ("/emetteurs/nouveau" in seen) == (who == A)
    assert ("/roles" in seen) == (who == A)
    assert ("/parametres" in seen) == (who == A)
    assert ("/parametres/api" in seen) == (who == A)
    assert ("/journal" in seen) == (who in (A, G))
    assert ("/comptes" in seen) == (who == A)


def test_custom_role_gets_exactly_its_rights(app, rich):
    client = app.test_client()
    login(client, db.session.scalar(db.select(type(rich[A])).filter_by(email="regie@asso.test")))
    assert client.get("/journal").status_code == 200
    assert client.get("/factures").status_code == 403
    assert client.get("/parametres").status_code == 403
    assert client.get("/roles").status_code == 403
    assert client.get("/sante").status_code == 404


def test_right_removed_from_a_role_applies_at_once(client, accounts):
    login(client, accounts[G])
    assert client.get("/journal").status_code == 200
    db.session.add(Role(key=G, name="Gestionnaire", permissions=[]))
    db.session.commit()
    assert client.get("/journal").status_code == 403
