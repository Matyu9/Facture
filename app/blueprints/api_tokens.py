"""Paramètres → API : jetons des applis qui créent ou lisent des factures (Adhérents, à terme)."""

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import current_user
from flask_wtf import FlaskForm
from wtforms import BooleanField, StringField
from wtforms.validators import DataRequired, Length

from ..extensions import db
from ..models import ApiToken
from ..permissions import require
from ..services import api_tokens
from ..services.activity import log

bp = Blueprint("api_tokens", __name__, url_prefix="/parametres/api")


class TokenForm(FlaskForm):
    name = StringField(
        "Nom de l'appli",
        validators=[DataRequired(), Length(max=80)],
        description="Par exemple « adherents ». Il sert aussi de source des clients que l'appli crée.",
    )
    can_issue = BooleanField("Peut émettre les factures (sinon : brouillons seulement, émis par une personne)")


@bp.route("", methods=["GET", "POST"])
@require("api.manage")
def index():
    form = TokenForm()
    if form.validate_on_submit():
        token, raw = api_tokens.create(form.name.data, form.can_issue.data, by=current_user)
        db.session.flush()
        log(
            "api.token_create",
            f"Jeton d'API créé : {token.name}" + (" (peut émettre)" if token.can_issue else " (brouillons)"),
        )
        db.session.commit()
        return render_template("settings/api_token_created.html", token=token, raw=raw)
    return render_template("settings/api.html", form=form, tokens=api_tokens.all_tokens())


@bp.route("/<int:token_id>/revoquer", methods=["POST"])
@require("api.manage")
def revoke(token_id):
    token = db.session.get(ApiToken, token_id) or abort(404)
    if token.active:
        api_tokens.revoke(token)
        log("api.token_revoke", f"Jeton d'API révoqué : {token.name}")
        db.session.commit()
        flash(f"Jeton « {token.name} » révoqué : l'appli ne peut plus s'en servir.", "success")
    return redirect(url_for(".index"))
