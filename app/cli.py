import click
from flask import g

from .extensions import db
from .models import Account, Role
from .services import accounts, api_tokens, archive, sessions, totp
from .services.activity import log


def register_cli(app):
    @app.cli.command("create-admin")
    @click.option("--email", prompt="Email")
    @click.option("--first-name", prompt="Prénom")
    @click.option("--last-name", prompt="Nom")
    @click.password_option(prompt="Mot de passe")
    def create_admin(email, first_name, last_name, password):
        """Crée (ou promeut) un compte administrateur : c'est aussi l'accès de secours."""
        g.write_lock = True
        email = email.strip().lower()
        account = db.session.scalar(db.select(Account).filter_by(email=email))
        if account is None:
            account = Account(email=email, first_name=first_name, last_name=last_name)
            db.session.add(account)
        account.role = Role.ADMIN
        account.role_from_oidc = False
        account.active = True
        account.set_password(password)
        if account.id:
            sessions.revoke(account)
            accounts.revoke(account)
            totp.disable(account)  # téléphone perdu : le secours doit rouvrir l'accès, le TOTP se reconfigure ensuite
        db.session.commit()
        click.echo(f"Administrateur prêt : {account.full_name} <{email}>")

    @app.cli.command("jeton-api")
    @click.option("--nom", required=True, help="Nom de l'appli (ex. adherents).")
    @click.option("--emettre", is_flag=True, help="Le jeton peut émettre les factures (sinon : brouillons seulement).")
    def api_token(nom, emettre):
        """Crée un jeton d'API pour une appli de l'association. Il n'est affiché qu'une fois."""
        g.write_lock = True
        token, raw = api_tokens.create(nom, emettre)
        db.session.flush()
        log("api.token_create", f"Jeton d'API créé en ligne de commande : {token.name}")
        db.session.commit()
        click.echo(raw)
        click.echo("Copiez-le maintenant dans la configuration de l'appli : il ne sera plus affiché.", err=True)

    @app.cli.command("verifier-archive")
    def check_archive():
        """Relit chaque PDF émis et vérifie son empreinte : un fichier manquant ou modifié est signalé."""
        from .models import Invoice

        bad = 0
        issued = db.session.scalars(db.select(Invoice).where(Invoice.status == Invoice.ISSUED).order_by(Invoice.id))
        count = 0
        for invoice in issued:
            count += 1
            try:
                archive.read(invoice)
            except archive.ArchiveError as exc:
                bad += 1
                click.echo(str(exc), err=True)
        click.echo(f"{count} pièce(s) vérifiée(s), {bad} problème(s).")
        if bad:
            raise SystemExit(1)

    @app.cli.command("run-tasks")
    def run_tasks():
        """Tâches périodiques (lancées par le service scheduler) : purges, retards, emails en attente."""
        from .services.health import record_scheduler_run
        from .services.tasks import run

        g.write_lock = True
        click.echo(run())
        record_scheduler_run()
