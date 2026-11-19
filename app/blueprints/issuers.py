"""Émetteurs : l'association, ou chacune de ses régies, avec ses mentions légales, sa TVA et sa numérotation."""

import re
from datetime import datetime
from decimal import Decimal

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user
from flask_wtf import FlaskForm
from wtforms import BooleanField, DecimalField, IntegerField, RadioField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp, ValidationError

from ..extensions import db, fold
from ..models import Invoice, Issuer
from ..permissions import can, require
from ..services import billing, branding, invoice_mail, pdf, photos
from ..services.activity import diff, log
from ..services.photos import InvalidImage

bp = Blueprint("issuers", __name__, url_prefix="/emetteurs")


def _strip(value):
    return value.strip() if isinstance(value, str) else value


def _compact(value):
    return re.sub(r"\s+", "", value).upper() if isinstance(value, str) else value


class IssuerForm(FlaskForm):
    code = StringField(
        "Code",
        validators=[
            DataRequired(),
            Length(max=30),
            Regexp(r"^[a-z0-9-]+$", message="Lettres minuscules sans accent, chiffres et tirets."),
        ],
        filters=[_strip],
        description="Identifiant court et stable (« ecole », « orchestre »), utilisé par l'API. Il ne change plus ensuite.",
    )
    name = StringField("Nom dans l'appli", validators=[DataRequired(), Length(max=120)], filters=[_strip])
    legal_name = StringField(
        "Dénomination sur les factures", validators=[DataRequired(), Length(max=200)], filters=[_strip]
    )
    legal_form = StringField(
        "Forme juridique", validators=[Optional(), Length(max=120)], filters=[_strip], default="Association loi 1901"
    )
    address = StringField("Adresse", validators=[DataRequired(), Length(max=255)], filters=[_strip])
    postcode = StringField("Code postal", validators=[DataRequired(), Length(max=20)], filters=[_strip])
    city = StringField("Ville", validators=[DataRequired(), Length(max=120)], filters=[_strip])
    country = StringField("Pays", validators=[Optional(), Length(max=80)], filters=[_strip], default="France")
    email = StringField(
        "Email",
        validators=[
            Optional(),
            Regexp(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", message="Adresse email invalide."),
            Length(max=255),
        ],
        filters=[_strip],
    )
    phone = StringField("Téléphone", validators=[Optional(), Length(max=40)], filters=[_strip])
    website = StringField("Site web", validators=[Optional(), Length(max=255)], filters=[_strip])
    siret = StringField(
        "SIRET",
        validators=[Optional(), Regexp(r"^\d{14}$", message="14 chiffres.")],
        filters=[_compact],
    )
    rna = StringField(
        "Numéro RNA",
        validators=[Optional(), Regexp(r"^W\d{9}$", message="W suivi de 9 chiffres.")],
        filters=[_compact],
    )
    vat_regime = SelectField("Régime de TVA", choices=list(Issuer.VAT_REGIMES.items()))
    vat_number = StringField(
        "N° de TVA intracommunautaire",
        validators=[Optional(), Regexp(r"^[A-Z]{2}[0-9A-Z]{2,13}$", message="Exemple : FR12345678901.")],
        filters=[_compact],
    )
    default_vat_rate = DecimalField(
        "Taux de TVA par défaut (%)",
        places=2,
        validators=[Optional(), NumberRange(min=0, max=100)],
        default=Decimal("20"),
    )
    vat_mention = StringField(
        "Mention de TVA",
        validators=[Optional(), Length(max=255)],
        filters=[_strip],
        description="Laissée vide : la mention du régime choisi.",
    )
    invoice_prefix = StringField(
        "Préfixe des factures",
        validators=[DataRequired(), Length(max=20), Regexp(r"^[A-Za-z0-9_-]*$", message="Lettres, chiffres, - et _.")],
        default="FA-",
    )
    credit_prefix = StringField(
        "Préfixe des avoirs",
        validators=[DataRequired(), Length(max=20), Regexp(r"^[A-Za-z0-9_-]*$", message="Lettres, chiffres, - et _.")],
        default="AV-",
    )
    number_digits = IntegerField("Chiffres du numéro", validators=[NumberRange(min=3, max=8)], default=4)
    payment_terms_days = IntegerField(
        "Délai de paiement (jours)",
        validators=[NumberRange(min=0, max=60, message="Entre 0 et 60 jours (délai légal maximal).")],
        default=30,
    )
    iban = StringField(
        "IBAN",
        validators=[Optional(), Regexp(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$", message="IBAN invalide.")],
        filters=[_compact],
    )
    bic = StringField(
        "BIC",
        validators=[Optional(), Regexp(r"^[A-Z0-9]{8}([A-Z0-9]{3})?$", message="BIC invalide.")],
        filters=[_compact],
    )
    payment_instructions = TextAreaField(
        "Modalités de règlement", validators=[Optional(), Length(max=1000)], filters=[_strip]
    )
    late_penalty = TextAreaField(
        "Pénalités de retard",
        validators=[Optional(), Length(max=1000)],
        filters=[_strip],
        description="Imprimées sur les factures des structures. Laissées vides : la mention légale par défaut.",
    )
    footer = TextAreaField("Pied de page", validators=[Optional(), Length(max=500)], filters=[_strip])
    email_subject = StringField("Objet de l'email d'envoi", validators=[Optional(), Length(max=200)], filters=[_strip])
    email_body = TextAreaField("Texte de l'email d'envoi", validators=[Optional(), Length(max=4000)], filters=[_strip])

    def __init__(self, *args, current=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._current = current

    def validate_code(self, field):
        if self._current is not None and self._current.code != field.data and self._has_issued():
            raise ValidationError("Le code ne change plus une fois des pièces émises.")
        other = db.session.scalar(db.select(Issuer).filter_by(code=field.data))
        if other is not None and other is not self._current:
            raise ValidationError("Un autre émetteur a déjà ce code.")

    def validate_name(self, field):
        for issuer in billing.issuers(include_archived=True):
            if fold(issuer.name) == fold(field.data) and issuer is not self._current:
                raise ValidationError("Un autre émetteur porte déjà ce nom.")

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators)
        # Hors des validateurs du champ : Optional() arrête la chaîne quand il est vide.
        if self.vat_regime.data == Issuer.SUBJECT and not self.vat_number.data:
            self.vat_number.errors = [*self.vat_number.errors, "Obligatoire pour un émetteur assujetti à la TVA."]
            ok = False
        return ok

    def _has_issued(self):
        return bool(
            db.session.scalar(
                db.select(Invoice.id).where(Invoice.issuer_id == self._current.id, Invoice.status == Invoice.ISSUED)
            )
        )


class LayoutForm(FlaskForm):
    pdf_layout = RadioField("Disposition", choices=list(Issuer.LAYOUTS.items()), default="classique")
    app_color = BooleanField("Reprendre la couleur de l'appli (Paramètres → Apparence)", default=True)
    pdf_color = StringField(
        "Couleur",
        validators=[Optional(), Regexp(r"^#[0-9a-fA-F]{6}$", message="Couleur au format #rrggbb.")],
        filters=[lambda v: v.strip().lower() if isinstance(v, str) else v],
        render_kw={"type": "color"},
    )
    logo_size = SelectField("Taille du logo", choices=list(Issuer.LOGO_SIZES.items()), default="moyen")
    logo_position = SelectField("Position du logo", choices=list(Issuer.LOGO_POSITIONS.items()), default="gauche")
    recipient_label = StringField(
        "Intitulé du destinataire",
        validators=[Optional(), Length(max=40)],
        filters=[_strip],
        render_kw={"placeholder": Issuer.DEFAULT_RECIPIENT_LABEL},
    )
    designation_label = StringField(
        "Intitulé de la colonne des lignes",
        validators=[Optional(), Length(max=40)],
        filters=[_strip],
        render_kw={"placeholder": Issuer.DEFAULT_DESIGNATION_LABEL},
    )
    intro_text = TextAreaField(
        "Texte d'introduction",
        validators=[Optional(), Length(max=1000)],
        filters=[_strip],
        description="Imprimé au-dessus des lignes.",
        render_kw={"placeholder": "Suite à notre prestation, veuillez trouver ci-dessous le détail…"},
    )
    closing_text = TextAreaField(
        "Texte de conclusion",
        validators=[Optional(), Length(max=500)],
        filters=[_strip],
        description="Imprimé en fin de pièce, après le règlement.",
        render_kw={"placeholder": "Merci de votre confiance."},
    )

    LAYOUT_FIELDS = (
        "pdf_layout",
        "pdf_color",
        "logo_size",
        "logo_position",
        "recipient_label",
        "designation_label",
        "intro_text",
        "closing_text",
    )

    def values(self):
        values = {name: getattr(self, name).data or None for name in self.LAYOUT_FIELDS}
        if self.app_color.data:
            values["pdf_color"] = None
        return values


def _values(form):
    values = {name: getattr(form, name).data for name in form._fields if name not in ("csrf_token",)}
    values = {k: (v if v != "" else None) for k, v in values.items()}
    if values.get("default_vat_rate") is None:
        values["default_vat_rate"] = Decimal("20")
    for key in ("vat_mention", "email_subject", "email_body", "late_penalty", "payment_instructions", "footer"):
        values[key] = values.get(key) or None
    return values


def _counts():
    rows = db.session.execute(db.select(Invoice.issuer_id, db.func.count(Invoice.id)).group_by(Invoice.issuer_id)).all()
    return dict(rows)


@bp.route("")
@require("invoice.view")
def index():
    return render_template(
        "issuers/index.html",
        issuers=billing.issuers(include_archived=True),
        counts=_counts(),
        manage=can(current_user, "issuer.manage"),
    )


@bp.route("/nouveau", methods=["GET", "POST"])
@require("issuer.manage")
def create():
    form = IssuerForm()
    if form.validate_on_submit():
        issuer = Issuer(**_values(form))
        db.session.add(issuer)
        db.session.flush()
        log("issuer.create", f"Émetteur créé : {issuer.name} ({issuer.regime_label})")
        db.session.commit()
        flash(f"Émetteur « {issuer.name} » créé.", "success")
        return redirect(url_for(".detail", issuer_id=issuer.id))
    return render_template("issuers/form.html", form=form, issuer=None, placeholders=invoice_mail.PLACEHOLDERS)


@bp.route("/<int:issuer_id>")
@require("invoice.view")
def detail(issuer_id):
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    return render_template(
        "issuers/detail.html",
        issuer=issuer,
        manage=can(current_user, "issuer.manage"),
        count=_counts().get(issuer.id, 0),
        default_penalty=billing.DEFAULT_LATE_PENALTY,
        sample=f"{issuer.invoice_prefix}{datetime.now().year}-{1:0{issuer.number_digits}d}",
        layouts=Issuer.LAYOUTS,
        logo_sizes=Issuer.LOGO_SIZES,
        logo_positions=Issuer.LOGO_POSITIONS,
    )


@bp.route("/<int:issuer_id>/modifier", methods=["GET", "POST"])
@require("issuer.manage")
def edit(issuer_id):
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    form = IssuerForm(obj=issuer, current=issuer)
    if form.validate_on_submit():
        values = _values(form)
        changes = diff(issuer, list(values), values)
        if changes:
            log("issuer.update", f"Émetteur modifié : {issuer.name}", changes=changes)
        db.session.commit()
        flash("Émetteur enregistré. Les pièces déjà émises gardent les mentions d'origine.", "success")
        return redirect(url_for(".detail", issuer_id=issuer.id))
    return render_template("issuers/form.html", form=form, issuer=issuer, placeholders=invoice_mail.PLACEHOLDERS)


@bp.route("/<int:issuer_id>/archiver", methods=["POST"])
@require("issuer.manage")
def archive(issuer_id):
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    if issuer.archived:
        issuer.archived_at = None
        log("issuer.restore", f"Émetteur rétabli : {issuer.name}")
        flash(f"« {issuer.name} » est rétabli.", "success")
    else:
        issuer.archived_at = datetime.now()
        log("issuer.archive", f"Émetteur archivé : {issuer.name}")
        flash(f"« {issuer.name} » est archivé : il n'est plus proposé pour de nouvelles factures.", "success")
    db.session.commit()
    return redirect(url_for(".detail", issuer_id=issuer.id))


@bp.route("/<int:issuer_id>/mise-en-page", methods=["GET", "POST"])
@require("issuer.manage")
def layout(issuer_id):
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    form = LayoutForm(obj=issuer)
    if request.method == "GET":
        form.app_color.data = not issuer.pdf_color
        form.pdf_color.data = issuer.pdf_color or branding.current().theme_color
    if form.validate_on_submit():
        values = form.values()
        changes = diff(issuer, list(values), values)
        if changes:
            log("issuer.layout", f"Mise en page modifiée : {issuer.name}", changes=changes)
        db.session.commit()
        flash("Mise en page enregistrée. Les pièces déjà émises gardent leur apparence d'origine.", "success")
        return redirect(url_for(".layout", issuer_id=issuer.id))
    return render_template("issuers/layout.html", issuer=issuer, form=form)


@bp.route("/<int:issuer_id>/apercu.pdf", methods=["GET", "POST"])
@require("invoice.view")
def preview(issuer_id):
    """Facture d'exemple : avec la mise en page enregistrée (GET), ou celle du formulaire, pas encore enregistrée
    (POST, depuis le bouton « Aperçu »)."""
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    overrides = None
    if request.method == "POST":
        if not can(current_user, "issuer.manage"):
            abort(403)
        form = LayoutForm()
        if not form.validate_on_submit():
            abort(400, " ".join(e for errors in form.errors.values() for e in errors))
        overrides = form.values()
    return Response(
        pdf.preview(issuer, overrides),
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="apercu-{issuer.code}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


@bp.route("/<int:issuer_id>/logo", methods=["POST"])
@require("issuer.manage")
def set_logo(issuer_id):
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    file = request.files.get("logo")
    if request.form.get("remove"):
        photos.remove(issuer.logo)
        issuer.logo = None
        log("issuer.logo", f"Logo retiré : {issuer.name}")
        db.session.commit()
        flash("Logo retiré.", "success")
    elif not file or not file.filename:
        flash("Choisissez une image.", "warning")
    else:
        try:
            new = photos.save_logo(file, size=photos.PRINT_LOGO_SIZE)
        except InvalidImage as exc:
            flash(str(exc), "error")
        else:
            # L'ancien fichier reste : il a pu servir à des PDF émis (qui l'ont déjà intégré, mais par prudence).
            issuer.logo = new
            log("issuer.logo", f"Logo modifié : {issuer.name}")
            db.session.commit()
            flash("Logo enregistré.", "success")
    return redirect(url_for(".layout", issuer_id=issuer.id))


@bp.route("/logo/<name>")
@require("invoice.view")
def logo_file(name):
    if not re.fullmatch(r"[0-9a-f]{32}\.png", name):
        abort(404)
    from pathlib import Path

    return send_from_directory(Path(current_app.config["UPLOAD_DIR"]) / "logo", name)
