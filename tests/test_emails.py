import smtplib
import ssl
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import Notification, Role
from app.services import branding, mailer, tasks

from .conftest import make_account


class FakeSMTP:
    sent, fail = [], None
    implicit_tls = False

    def __init__(self, host, port, local_hostname=None, timeout=None, context=None):
        self.host, self.port = host, port
        FakeSMTP.connected = (host, port, local_hostname)
        if context is not None:
            FakeSMTP.context = context

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def starttls(self, context=None):
        FakeSMTP.tls, FakeSMTP.context = True, context

    def close(self):
        pass

    def login(self, user, password):
        FakeSMTP.credentials = (user, password)

    def send_message(self, message, from_addr=None):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        FakeSMTP.envelope_from = from_addr
        FakeSMTP.sent.append(message)


@pytest.fixture
def smtp(app, monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail, FakeSMTP.tls, FakeSMTP.credentials = [], None, False, None
    FakeSMTP.context, FakeSMTP.connected, FakeSMTP.implicit_tls = None, None, False

    class FakeSMTPSSL(FakeSMTP):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            FakeSMTP.implicit_tls = True

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    app.config.update(
        SMTP_HOST="smtp.test", SMTP_FROM="asso@asso.test", SMTP_USER="u", SMTP_PASSWORD="p", SMTP_TLS=True
    )
    return FakeSMTP


def pending(account, message="Votre dossier est incomplet", **fields):
    notification = Notification(account_id=account.id, message=message, **fields)
    db.session.add(notification)
    db.session.commit()
    return notification


def test_nothing_is_sent_without_smtp(accounts):
    notification = pending(accounts[Role.LECTEUR])
    assert not mailer.smtp_enabled() and mailer.deliver_pending() == 0
    assert not db.session.get(Notification, notification.id).emailed


def test_delivery_marks_as_sent_and_is_not_repeated(accounts, smtp):
    notification = pending(accounts[Role.LECTEUR], link="/notifications")
    assert mailer.deliver_pending() == 1
    assert db.session.get(Notification, notification.id).emailed
    assert mailer.deliver_pending() == 0 and len(smtp.sent) == 1
    message = smtp.sent[0]
    assert message["To"] == "lecteur@asso.test" and "Votre dossier" in message["Subject"]
    assert smtp.tls and smtp.credentials == ("u", "p")


def test_email_has_text_and_html_with_absolute_link(app, accounts, smtp):
    pending(accounts[Role.LECTEUR], link="/notifications")
    mailer.deliver_pending()
    message = smtp.sent[0]
    text = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    url = app.config["BASE_URL"] + "/notifications"
    assert url in text and url in html and "Bonjour Lecteur" in text and "Inventaire" not in html + text


def test_email_escapes_html_and_header_injection(accounts, smtp):
    pending(accounts[Role.LECTEUR], message="<b>Gras</b>\nBcc: pirate@evil.test")
    mailer.deliver_pending()
    message = smtp.sent[0]
    assert "<b>Gras" not in message.get_body(("html",)).get_content()
    assert message["Bcc"] is None and "\n" not in message["Subject"]


def test_email_uses_the_palette_and_name(accounts, smtp):
    branding.save("Harmonie", "", "vert")
    db.session.commit()
    pending(accounts[Role.LECTEUR])
    mailer.deliver_pending()
    assert "Harmonie" in smtp.sent[0]["Subject"]
    assert branding.PALETTES["vert"][1] in smtp.sent[0].get_body(("html",)).get_content()


def test_old_notifications_and_inactive_accounts_are_skipped(accounts, smtp):
    old = pending(accounts[Role.LECTEUR], created_at=datetime.now() - mailer.MAX_AGE - timedelta(hours=1))
    gone = make_account(email="parti@asso.test", active=False)
    pending(gone)
    assert mailer.deliver_pending() == 0 and not db.session.get(Notification, old.id).emailed


def test_failure_stops_and_retries_next_time(accounts, smtp):
    first = pending(accounts[Role.LECTEUR], message="un")
    second = pending(accounts[Role.LECTEUR], message="deux")
    smtp.fail = OSError("serveur indisponible")
    assert mailer.deliver_pending() == 0
    assert not db.session.get(Notification, first.id).emailed
    smtp.fail = None
    assert mailer.deliver_pending() == 2
    assert db.session.get(Notification, second.id).emailed


def test_deliver_quietly_never_raises(accounts, smtp, monkeypatch):
    monkeypatch.setattr(mailer, "deliver_pending", lambda limit: 1 / 0)
    assert mailer.deliver_quietly() == 0


def test_periodic_task_sends_emails_outside_of_a_request(app, accounts, smtp):
    pending(accounts[Role.LECTEUR])
    branding._set("logo", "logo/" + "a" * 32 + ".png")
    db.session.commit()
    assert "1 email(s)" in tasks.run()


def test_test_email_route(client, admin, smtp):
    assert "Email de test envoyé" in client.post("/parametres/email-test", follow_redirects=True).get_data(as_text=True)
    assert smtp.sent[0]["To"] == admin.email


def test_test_email_route_reports_failures(client, admin, smtp):
    smtp.fail = OSError("refusé")
    assert "Échec de l&#39;envoi" in client.post("/parametres/email-test", follow_redirects=True).get_data(as_text=True)


def test_long_subject_is_cut():
    assert len(mailer._shorten("x" * 300)) == 100
    assert mailer._shorten("court") == "court"


# --- Connexion au serveur SMTP (relais Google Workspace, etc.) ---


def test_starttls_verifies_the_server_certificate(app, smtp):
    mailer.send("a@b.test", "Objet", "Texte")
    assert smtp.tls and not smtp.implicit_tls
    assert smtp.context.verify_mode == ssl.CERT_REQUIRED and smtp.context.check_hostname


def test_port_465_uses_implicit_tls_with_a_verified_certificate(app, smtp):
    app.config.update(SMTP_PORT=465)
    mailer.send("a@b.test", "Objet", "Texte")
    assert smtp.implicit_tls and not smtp.tls and smtp.connected[1] == 465
    assert smtp.context.verify_mode == ssl.CERT_REQUIRED and smtp.context.check_hostname
    assert len(smtp.sent) == 1


def test_no_encryption_when_disabled(app, smtp):
    app.config.update(SMTP_TLS=False, SMTP_PORT=25)
    mailer.send("a@b.test", "Objet", "Texte")
    assert not smtp.tls and not smtp.implicit_tls and len(smtp.sent) == 1


def test_relay_by_ip_address_needs_no_login(app, smtp):
    """Relais Google autorisé par l'adresse IP du serveur : pas d'identifiant, seulement l'expéditeur."""
    app.config.update(SMTP_USER="", SMTP_PASSWORD="", SMTP_FROM="factures@asso.test")
    assert mailer.smtp_enabled()
    mailer.send("a@b.test", "Objet", "Texte")
    assert smtp.credentials is None and "factures@asso.test" in smtp.sent[0]["From"]


@pytest.mark.parametrize(
    ("helo", "base_url", "expected"),
    [
        ("", "https://facture.asso.test", "facture.asso.test"),
        ("", "https://facture.asso.test:8443/", "facture.asso.test"),
        ("relais.asso.test", "https://facture.asso.test", "relais.asso.test"),
        ("", "http://localhost:8002", None),
        ("", "http://127.0.0.1:8002", None),
        ("", "http://192.168.1.20:8002", None),
        ("", "http://[::1]:8002", None),
        ("", "", None),
    ],
)
def test_ehlo_name(app, smtp, helo, base_url, expected):
    """Un vrai nom de domaine, pas l'identifiant du conteneur Docker que smtplib annoncerait sinon."""
    app.config.update(SMTP_HELO=helo, BASE_URL=base_url)
    mailer.send("a@b.test", "Objet", "Texte")
    assert smtp.connected[2] == expected


@pytest.mark.parametrize(
    ("smtp_from", "sender_name", "expected_name"),
    [
        ("factures@asso.test", None, "Asso"),
        ("Facturation Asso <factures@asso.test>", None, "Facturation Asso"),
        ('"Facturation Asso" <factures@asso.test>', None, "Facturation Asso"),
        ("Facturation Asso <factures@asso.test>", "Régie son", "Régie son"),
    ],
)
def test_sender_address_with_or_without_a_name(app, smtp, monkeypatch, smtp_from, sender_name, expected_name):
    """SMTP_FROM peut porter un nom : l'enveloppe ne reçoit que l'adresse (Google refusait « Nom » comme adresse)."""
    monkeypatch.setattr(branding, "name", lambda: "Asso")
    app.config.update(SMTP_FROM=smtp_from)
    mailer.send("a@b.test", "Objet", "Texte", sender_name=sender_name)
    sender = smtp.sent[0]["From"].addresses
    assert smtp.envelope_from == "factures@asso.test"
    assert len(sender) == 1 and sender[0].addr_spec == "factures@asso.test" and sender[0].display_name == expected_name


def test_invalid_sender_address_gives_a_readable_error(app, smtp):
    app.config.update(SMTP_FROM="Facturation Asso")
    with pytest.raises(smtplib.SMTPException, match="SMTP_FROM"):
        mailer.send("a@b.test", "Objet", "Texte")
    assert not smtp.sent
