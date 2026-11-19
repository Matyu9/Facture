from app.extensions import db
from app.models import Account, LoginSession, Role
from app.services import health

from .conftest import login, make_account


def create_admin(app, email="Chef@Asso.Test", password="un-mot-de-passe"):
    return app.test_cli_runner().invoke(
        args=[
            "create-admin",
            "--email",
            email,
            "--first-name",
            "Élise",
            "--last-name",
            "Lefèvre",
            "--password",
            password,
        ]
    )


def test_create_admin(app):
    result = create_admin(app)
    assert result.exit_code == 0 and "Élise Lefèvre" in result.output
    account = db.session.scalar(db.select(Account))
    assert account.email == "chef@asso.test" and account.is_admin and account.active
    assert account.check_password("un-mot-de-passe")


def test_create_admin_is_the_rescue_access(app, client):
    """Compte existant : il redevient admin, actif, avec le nouveau mot de passe ; ses anciennes sessions sont closes."""
    account = make_account(Role.LECTEUR, email="chef@asso.test")
    login(client, account)
    account.active = False
    db.session.commit()
    assert create_admin(app).exit_code == 0
    account = db.session.get(Account, account.id)
    assert account.is_admin and account.active and account.check_password("un-mot-de-passe")
    assert db.session.scalar(db.select(db.func.count(LoginSession.id))) == 0
    assert db.session.scalar(db.select(db.func.count(Account.id))) == 1


def test_run_tasks_records_its_passage(app):
    assert health.last_scheduler_run() is None
    result = app.test_cli_runner().invoke(args=["run-tasks"])
    assert result.exit_code == 0 and "effacé(s)" in result.output
    assert health.last_scheduler_run() is not None


def test_api_token_is_shown_once_and_stored_as_a_digest(app):
    from app.models import ApiToken
    from app.services import api_tokens

    result = app.test_cli_runner().invoke(args=["jeton-api", "--nom", "adherents"])
    assert result.exit_code == 0
    raw = result.output.splitlines()[0]
    token = db.session.scalar(db.select(ApiToken))
    assert raw.startswith("fct_") and token.name == "adherents" and not token.can_issue
    assert raw not in (token.token_hash, token.prefix) and api_tokens.find(raw) == token


def test_archive_check_reports_a_modified_pdf(app, issued_invoice):
    runner = app.test_cli_runner()
    assert runner.invoke(args=["verifier-archive"]).exit_code == 0
    path = app.config["ARCHIVE_DIR"] / issued_invoice.pdf_path
    path.chmod(0o640)
    path.write_bytes(path.read_bytes() + b"%")
    result = runner.invoke(args=["verifier-archive"])
    assert result.exit_code == 1 and "modifié" in result.output
