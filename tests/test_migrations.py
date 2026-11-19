"""Migrations Alembic jouées sur une vraie base PostgreSQL, vide et jetable.

Les autres tests créent le schéma avec create_all : ils ne verraient pas un modèle sans migration, ni une contrainte
ou une cascade que la migration aurait oubliée.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from flask_migrate import downgrade, upgrade
from sqlalchemy.pool import NullPool

from app import create_app
from app.extensions import db

from .conftest import config_for, fresh_database, release

MIGRATIONS = str(Path(__file__).resolve().parent.parent / "migrations")
SEED = (
    "INSERT INTO account (id, oidc_sub, first_name, last_name, email, email_verified, role, active, "
    "must_change_password, role_from_oidc, created_at, updated_at) VALUES "
    "(1, 'sub-1', 'Élise', 'Lefèvre', 'elise@asso.test', false, 'admin', true, false, false, now(), now()), "
    "(2, 'sub-2', 'Paul', 'Martin', 'paul@asso.test', false, 'lecteur', true, false, false, now(), now())",
    "INSERT INTO role (key, name, permissions) VALUES ('regie', 'Régie', '[\"activity.view\"]')",
    "INSERT INTO login_session (id, sid, account_id, created_at) VALUES ('jeton', 's1', 1, '2026-09-01 10:00:00')",
    "INSERT INTO activity (account_id, target_account_id, action, message, created_at) "
    "VALUES (1, 2, 'role.update', 'Rôle modifié', now())",
    "INSERT INTO notification (account_id, message, read, emailed, created_at) VALUES (2, 'Bonjour', false, false, now())",
    "INSERT INTO app_setting (key, value) VALUES ('name', 'Harmonie')",
)


@pytest.fixture
def migrated(tmp_path):
    with fresh_database() as url:
        app = create_app(config_for(tmp_path, SQLALCHEMY_DATABASE_URI=url))
        with app.app_context():
            engine = sa.create_engine(url, poolclass=NullPool)
            yield engine
            engine.dispose()
            release()


def run_sql(engine, *statements):
    """Exécute chaque requête dans sa propre transaction, comme le ferait une autre appli. Renvoie les lignes lues."""
    rows = []
    for statement in statements:
        with engine.begin() as connection:
            result = connection.exec_driver_sql(statement)
            rows.append(result.fetchall() if result.returns_rows else None)
    return rows


def assert_fails(engine, statement):
    with pytest.raises(sa.exc.IntegrityError):
        run_sql(engine, statement)


def test_single_head():
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS)
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1


def test_models_match_migrations(migrated):
    """Un modèle modifié sans migration passerait tous les autres tests, et casserait en production."""
    upgrade(MIGRATIONS)
    context = MigrationContext.configure(db.session.connection(), opts={"compare_type": True})
    assert compare_metadata(context, db.metadata) == []


def test_upgrade_then_downgrade_then_upgrade_again(migrated):
    upgrade(MIGRATIONS)
    run_sql(migrated, *SEED)
    downgrade(MIGRATIONS, revision="base")
    (tables,) = run_sql(
        migrated,
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_name <> 'alembic_version'",
    )
    assert tables == [(0,)]
    upgrade(MIGRATIONS)
    run_sql(migrated, *SEED)
    accounts, settings = run_sql(
        migrated,
        "SELECT id, oidc_sub, email FROM account ORDER BY id",
        "SELECT value FROM app_setting WHERE key = 'name'",
    )
    assert accounts == [(1, "sub-1", "elise@asso.test"), (2, "sub-2", "paul@asso.test")]
    assert settings == [("Harmonie",)]


def test_search_without_accents_is_installed(migrated):
    upgrade(MIGRATIONS)
    (rows,) = run_sql(migrated, "SELECT fold('Cœur d''Élodie LEFÈVRE'), fold(NULL)")
    assert rows == [("coeur d'elodie lefevre", None)]


def test_constraints_of_the_real_schema(migrated):
    upgrade(MIGRATIONS)
    run_sql(migrated, *SEED)
    for statement in (
        "INSERT INTO account (oidc_sub, first_name, last_name, email, email_verified, role, active, must_change_password, "
        "role_from_oidc, created_at, updated_at) "
        "VALUES ('autre', 'A', 'B', 'elise@asso.test', false, 'lecteur', true, false, false, now(), now())",
        "INSERT INTO account (oidc_sub, first_name, last_name, email, email_verified, role, active, must_change_password, "
        "role_from_oidc, created_at, updated_at) "
        "VALUES ('sub-1', 'A', 'B', 'n@asso.test', false, 'lecteur', true, false, false, now(), now())",
        "INSERT INTO login_session (id, sid, account_id, created_at) VALUES ('x', 's2', 99, now())",
        "INSERT INTO login_session (id, sid, account_id, created_at) VALUES ('y', 's1', 1, now())",
    ):
        assert_fails(migrated, statement)


BILLING = (
    "INSERT INTO issuer (id, code, name, legal_name, vat_regime, default_vat_rate, invoice_prefix, credit_prefix, "
    "number_digits, payment_terms_days, created_at, updated_at) "
    "VALUES (1, 'ecole', 'École', 'Association', 'franchise', 20, 'FA-', 'AV-', 4, 30, now(), now())",
    "INSERT INTO customer (id, kind, name, created_at, updated_at) VALUES (1, 'structure', 'Mairie', now(), now())",
    "INSERT INTO invoice (id, kind, status, issuer_id, customer_id, number, total_ht_cents, total_vat_cents, "
    "total_ttc_cents, created_at, updated_at) "
    "VALUES (1, 'facture', 'emise', 1, 1, 'FA-2026-0001', 1000, 0, 1000, now(), now())",
    "INSERT INTO invoice_line (invoice_id, position, label, quantity, unit_price_cents, vat_rate, total_ht_cents) "
    "VALUES (1, 0, 'Concert', 1, 1000, 0, 1000)",
    "INSERT INTO payment (invoice_id, paid_on, amount_cents, method, created_at, updated_at) "
    "VALUES (1, '2026-11-19', 500, 'cheque', now(), now())",
    "INSERT INTO activity (account_id, invoice_id, customer_id, action, message, created_at) "
    "VALUES (1, 1, 1, 'invoice.issue', 'Émise', now())",
)


def test_invoice_numbers_are_unique(migrated):
    upgrade(MIGRATIONS)
    run_sql(migrated, *SEED, *BILLING)
    assert_fails(
        migrated,
        "INSERT INTO invoice (kind, status, issuer_id, customer_id, number, total_ht_cents, total_vat_cents, "
        "total_ttc_cents, created_at, updated_at) VALUES ('facture', 'emise', 1, 1, 'FA-2026-0001', 0, 0, 0, now(), now())",
    )
    run_sql(migrated, "INSERT INTO number_sequence (issuer_id, kind, year, last_number) VALUES (1, 'facture', 2026, 1)")
    assert_fails(
        migrated, "INSERT INTO number_sequence (issuer_id, kind, year, last_number) VALUES (1, 'facture', 2026, 2)"
    )


def test_an_issued_invoice_protects_its_issuer_and_customer(migrated):
    """Une pièce émise ne disparaît pas parce qu'on supprime son client ou son émetteur : la base le refuse."""
    upgrade(MIGRATIONS)
    run_sql(migrated, *SEED, *BILLING)
    assert_fails(migrated, "DELETE FROM customer WHERE id = 1")
    assert_fails(migrated, "DELETE FROM issuer WHERE id = 1")


def test_deleting_an_account_keeps_the_journal_and_the_invoices(migrated):
    upgrade(MIGRATIONS)
    run_sql(
        migrated,
        *SEED,
        *BILLING,
        "UPDATE invoice SET issued_by_id = 1, created_by_id = 1",
        "INSERT INTO account_token (id, account_id, purpose, proves_email, created_at, expires_at) "
        "VALUES ('h', 2, 'reset', false, now(), now())",
        "INSERT INTO recovery_code (id, account_id) VALUES ('c', 2)",
    )
    run_sql(migrated, "DELETE FROM account WHERE id = 2")
    tokens, codes, notifications = run_sql(
        migrated,
        "SELECT count(*) FROM account_token",
        "SELECT count(*) FROM recovery_code",
        "SELECT count(*) FROM notification",
    )
    assert (tokens, codes, notifications) == ([(0,)], [(0,)], [(0,)])
    run_sql(migrated, "DELETE FROM account WHERE id = 1")
    invoices, activity, sessions = run_sql(
        migrated,
        "SELECT number, issued_by_id, created_by_id FROM invoice",
        "SELECT account_id, invoice_id FROM activity WHERE action = 'invoice.issue'",
        "SELECT count(*) FROM login_session",
    )
    assert invoices == [("FA-2026-0001", None, None)]
    assert activity == [(None, 1)]
    assert sessions == [(0,)]


def test_deleting_an_invoice_removes_its_lines_and_payments(migrated):
    """Un brouillon supprimé emporte ses lignes et ses règlements ; le journal garde l'action, sans lien."""
    upgrade(MIGRATIONS)
    run_sql(migrated, *SEED, *BILLING)
    run_sql(migrated, "DELETE FROM invoice WHERE id = 1")
    lines, payments, activity = run_sql(
        migrated,
        "SELECT count(*) FROM invoice_line",
        "SELECT count(*) FROM payment",
        "SELECT invoice_id, customer_id FROM activity WHERE action = 'invoice.issue'",
    )
    assert (lines, payments, activity) == ([(0,)], [(0,)], [(None, 1)])
