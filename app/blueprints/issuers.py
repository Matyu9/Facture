"""Émetteurs : l'association, ou chacune de ses régies, avec ses mentions légales, sa TVA et sa numérotation."""

import re
from datetime import date, datetime
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
from ..models import Invoice, Issuer, NumberSeries
from ..permissions import can, require
from ..services import billing, branding, invoice_mail, numbering, pdf, photos
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
    quote_validity_days = IntegerField(
        "Validité des devis (jours)",
        validators=[NumberRange(min=1, max=365, message="Entre 1 et 365 jours.")],
        default=30,
    )
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


FORMAT_LABELS = {"invoice_format": "factures", "credit_format": "avoirs", "quote_format": "devis"}


def _format_field(label):
    return StringField(label, validators=[DataRequired()], filters=[_strip], render_kw={"spellcheck": "false"})


def _next_field():
    return IntegerField(
        "Prochain numéro", validators=[NumberRange(min=1, max=9_999_999, message="Un nombre entre 1 et 9 999 999.")]
    )


class FormatsMixin:
    """Formats valides, et d'une forme qu'aucune autre série de l'appli ne produit (les numéros sont uniques)."""

    format_fields = ()

    def check_formats(self, taken):
        ok, seen = True, {}
        for name in self.format_fields:
            field = getattr(self, name)
            if field.errors:
                ok = False
                continue
            try:
                numbering.example(field.data)
                fmt = numbering.parse(field.data)
            except numbering.FormatError as exc:
                field.errors = [*field.errors, str(exc)]
                ok = False
                continue
            clash = seen.get(fmt.signature) or taken.get(fmt.signature)
            if clash:
                field.errors = [*field.errors, f"Même forme que les {clash} : les numéros se mélangeraient."]
                ok = False
            seen.setdefault(fmt.signature, f"{FORMAT_LABELS[name]} de cette série")
        return ok


class QuoteNumberingForm(FormatsMixin, FlaskForm):
    format_fields = ("quote_format",)
    quote_format = _format_field("Format des devis")
    quote_next = _next_field()

    def __init__(self, *args, issuer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._issuer = issuer

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators)
        return self.check_formats(billing.taken_signatures(skip_quote_of=self._issuer)) and ok


class SeriesForm(FormatsMixin, FlaskForm):
    format_fields = ("invoice_format", "credit_format")
    name = StringField("Nom", validators=[DataRequired(), Length(max=80)], filters=[_strip])
    code = StringField(
        "Code",
        validators=[
            DataRequired(),
            Length(max=30),
            Regexp(r"^[a-z0-9-]+$", message="Lettres minuscules sans accent, chiffres et tirets."),
        ],
        filters=[_strip],
        description='Pour l\'API ("series": "ce").',
    )
    invoice_format = _format_field("Format des factures")
    invoice_next = _next_field()
    credit_format = _format_field("Format des avoirs")
    credit_next = _next_field()
    is_default = BooleanField("Série par défaut des nouvelles factures")

    def __init__(self, *args, issuer=None, series=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._issuer, self._series = issuer, series

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators)
        for other in self._issuer.series:
            if other is self._series:
                continue
            if other.code == self.code.data:
                self.code.errors = [*self.code.errors, "Une autre série de cet émetteur a ce code."]
                ok = False
            if fold(other.name) == fold(self.name.data or ""):
                self.name.errors = [*self.name.errors, "Une autre série de cet émetteur porte ce nom."]
                ok = False
        return self.check_formats(billing.taken_signatures(skip_series=self._series)) and ok


class LayoutForm(FlaskForm):
    pdf_layout = RadioField("Disposition", choices=list(Issuer.LAYOUTS.items()), default="classique")
    app_color = BooleanField("Reprendre la couleur de l'appli (Paramètres → Apparence)", default=True)
    pdf_color = StringField(
        "Couleur",
        validators=[Optional(), Regexp(r"^#[0-9a-fA-F]{6}$", message="Couleur au format #rrggbb.")],
        filters=[lambda v: v.strip().lower() if isinstance(v, str) else v],
        render_kw={"type": "color"},
    )
    remove_logo = BooleanField("Retirer le logo")
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
        billing.default_series(issuer)
        if numbering.of(issuer, Invoice.QUOTE).signature in billing.taken_signatures(skip_quote_of=issuer):
            issuer.quote_format = f"{issuer.code.upper()[:20]}-{numbering.default(Invoice.QUOTE)}"
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
        rows=_series(issuer),
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


def _uploaded_logo():
    """Logo envoyé avec le formulaire de mise en page, ou None. InvalidImage si ce n'est pas une image lisible."""
    file = request.files.get("logo")
    if not file or not file.filename:
        return None
    return photos.save_logo(file, size=photos.PRINT_LOGO_SIZE)


@bp.route("/<int:issuer_id>/mise-en-page", methods=["GET", "POST"])
@require("issuer.manage")
def layout(issuer_id):
    """Mise en page et logo, dans un seul formulaire : changer le logo ne perd pas les autres réglages en cours."""
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    form = LayoutForm(obj=issuer)
    logo_error = None
    if request.method == "GET":
        form.app_color.data = not issuer.pdf_color
        form.pdf_color.data = issuer.pdf_color or branding.current().theme_color
    if form.validate_on_submit():
        try:
            new_logo = _uploaded_logo()
        except InvalidImage as exc:
            logo_error = str(exc)
        else:
            values = form.values()
            changes = diff(issuer, list(values), values)
            if new_logo:
                # L'ancien fichier reste : les PDF émis ont intégré le logo, mais par prudence on ne l'efface pas.
                issuer.logo = new_logo
                changes["logo"] = ["(ancien)", "(nouveau)"]
            elif form.remove_logo.data and issuer.logo:
                issuer.logo = None
                changes["logo"] = ["(ancien)", "(retiré)"]
            if changes:
                log("issuer.layout", f"Mise en page modifiée : {issuer.name}", changes=changes)
            db.session.commit()
            flash("Mise en page enregistrée. Les pièces déjà émises gardent leur apparence d'origine.", "success")
            return redirect(url_for(".layout", issuer_id=issuer.id))
    return render_template("issuers/layout.html", issuer=issuer, form=form, logo_error=logo_error)


@bp.route("/<int:issuer_id>/apercu.pdf", methods=["GET", "POST"])
@require("invoice.view")
def preview(issuer_id):
    """Facture d'exemple : avec la mise en page enregistrée (GET), ou celle du formulaire, pas encore enregistrée
    (POST, depuis le bouton « Aperçu »), logo choisi compris."""
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    overrides, temporary = None, None
    if request.method == "POST":
        if not can(current_user, "issuer.manage"):
            abort(403)
        form = LayoutForm()
        if not form.validate_on_submit():
            abort(400, " ".join(e for errors in form.errors.values() for e in errors))
        overrides = form.values()
        try:
            temporary = _uploaded_logo()
        except InvalidImage as exc:
            abort(400, str(exc))
        if temporary:
            overrides["logo"] = temporary
        elif form.remove_logo.data:
            overrides["logo"] = None
    try:
        content = pdf.preview(issuer, overrides)
    finally:
        if temporary:
            photos.remove(temporary)
    return Response(
        content,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="apercu-{issuer.code}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


def _series(issuer):
    """Ce qu'affichent la fiche et la page Numérotation : les devis, puis une ligne par série de factures."""
    today = date.today()

    def row(kind, series=None):
        fmt = numbering.of(issuer, kind, series)
        nxt = billing.next_number(issuer, kind, today, series)
        return {"format": fmt.text, "next": nxt, "sample": fmt.render(today, nxt), "reset": numbering.RESETS[fmt.reset]}

    billing.default_series(issuer)
    return {
        "quote": row(Invoice.QUOTE),
        "series": [
            {"series": s, "invoice": row(Invoice.INVOICE, s), "credit": row(Invoice.CREDIT, s)} for s in issuer.series
        ],
    }


def _issued(issuer, series=None):
    query = db.select(Invoice.id).where(Invoice.issuer_id == issuer.id, Invoice.status == Invoice.ISSUED)
    if series is not None:
        query = query.where(Invoice.series_id == series.id)
    return bool(db.session.scalar(query.limit(1)))


def _apply_next(form, issuer, pairs, day, series=None):
    """Applique les « prochain numéro » saisis (kind, champ). Renvoie les changements, ou None si l'un est refusé."""
    changes, failed = {}, False
    for kind, name in pairs:
        field = getattr(form, name)
        current = billing.next_number(issuer, kind, day, series)
        # Le numéro saisi vaut pour la série du format enregistré : l'exemple affiché est celui qui sortira.
        if field.data == current:
            continue
        try:
            billing.set_next_number(issuer, kind, field.data, day, series)
        except billing.BillingError as exc:
            lowest = billing.lowest_next_number(issuer, kind, day, series)
            field.errors = [*field.errors, f"{exc} Au moins {lowest}."]
            failed = True
        else:
            changes[name] = [current, field.data]
    return None if failed else changes


def _set_formats(target, form, names):
    changes = {}
    for name in names:
        new = getattr(form, name).data
        if new != getattr(target, name):
            changes[name] = [getattr(target, name), new]
            setattr(target, name, new)
    return changes


@bp.route("/<int:issuer_id>/numerotation", methods=["GET", "POST"])
@require("issuer.manage")
def numbers(issuer_id):
    """Format des devis, et liste des séries de factures."""
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    today = date.today()
    billing.default_series(issuer)
    form = QuoteNumberingForm(obj=issuer, issuer=issuer)
    if request.method == "GET":
        form.quote_next.data = billing.next_number(issuer, Invoice.QUOTE, today)
    if form.validate_on_submit():
        changes = _set_formats(issuer, form, ("quote_format",))
        applied = _apply_next(form, issuer, [(Invoice.QUOTE, "quote_next")], today)
        if applied is None:
            db.session.rollback()
        else:
            changes.update(applied)
            if changes:
                log("issuer.numbering", f"Numérotation des devis modifiée : {issuer.name}", changes=changes)
            db.session.commit()
            flash("Numérotation des devis enregistrée.", "success")
            return redirect(url_for(".numbers", issuer_id=issuer.id))
    db.session.commit()  # série par défaut créée au besoin
    return render_template(
        "issuers/numbering.html",
        issuer=issuer,
        form=form,
        rows=_series(issuer),
        help=numbering.HELP,
        example=_example(form.quote_format.data or "", form.quote_next.data, today),
        issued=_issued(issuer),
    )


def _series_or_404(issuer, series_id):
    series = db.session.get(NumberSeries, series_id)
    if series is None or series.issuer_id != issuer.id:
        abort(404)
    return series


@bp.route("/<int:issuer_id>/series/nouvelle", methods=["GET", "POST"])
@bp.route("/<int:issuer_id>/series/<int:series_id>", methods=["GET", "POST"])
@require("issuer.manage")
def series(issuer_id, series_id=None):
    """Création ou modification d'une série de factures : nom, code, formats, prochains numéros."""
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    current = _series_or_404(issuer, series_id) if series_id else None
    today = date.today()
    form = SeriesForm(obj=current, issuer=issuer, series=current)
    pairs = [(Invoice.INVOICE, "invoice_next"), (Invoice.CREDIT, "credit_next")]
    if request.method == "GET":
        if current is None:
            form.invoice_format.data = form.credit_format.data = ""
            form.invoice_next.data = form.credit_next.data = 1
        else:
            for kind, name in pairs:
                getattr(form, name).data = billing.next_number(issuer, kind, today, current)
    if form.validate_on_submit():
        target = current or NumberSeries(issuer=issuer, is_default=False)
        changes = _set_formats(target, form, ("name", "code", "invoice_format", "credit_format"))
        if current is None:
            db.session.add(target)
        if form.is_default.data and not target.is_default:
            if target.archived:
                form.is_default.errors = ["Une série archivée ne peut pas être celle par défaut."]
            else:
                for other in issuer.series:
                    other.is_default = False
                db.session.flush()  # une seule série par défaut à la fois, même le temps de l'écriture
                target.is_default = True
                changes["is_default"] = [False, True]
        applied = None if form.is_default.errors else _apply_next(form, issuer, pairs, today, target)
        if applied is None:
            db.session.rollback()
        else:
            changes.update(applied)
            verb = "créée" if current is None else "modifiée"
            log("issuer.series", f"Série « {target.name} » {verb} : {issuer.name}", changes=changes)
            db.session.commit()
            flash(f"Série « {target.name} » enregistrée.", "success")
            return redirect(url_for(".numbers", issuer_id=issuer.id))
    examples = {
        name: _example(getattr(form, name).data or "", getattr(form, name.replace("_format", "_next")).data, today)
        for name in ("invoice_format", "credit_format")
    }
    return render_template(
        "issuers/series_form.html",
        issuer=issuer,
        series=current,
        form=form,
        help=numbering.HELP,
        examples=examples,
        issued=current is not None and _issued(issuer, current),
    )


@bp.route("/<int:issuer_id>/series/<int:series_id>/archiver", methods=["POST"])
@require("issuer.manage")
def archive_series(issuer_id, series_id):
    issuer = db.session.get(Issuer, issuer_id) or abort(404)
    current = _series_or_404(issuer, series_id)
    if current.is_default:
        flash("La série par défaut ne s'archive pas : choisissez d'abord une autre série par défaut.", "error")
    else:
        current.archived_at = None if current.archived else datetime.now()
        state = "archivée" if current.archived else "rétablie"
        log("issuer.series", f"Série « {current.name} » {state} : {issuer.name}")
        db.session.commit()
        flash(f"Série « {current.name} » {state}.", "success")
    return redirect(url_for(".numbers", issuer_id=issuer.id))


def _example(text, n, day=None):
    """(exemple, remise à zéro, erreur) pour un format saisi."""
    day = day or date.today()
    try:
        fmt = numbering.parse(text)
        numbering.example(text)
    except numbering.FormatError as exc:
        return None, None, str(exc)
    return fmt.render(day, n if isinstance(n, int) and n > 0 else 1), numbering.RESETS[fmt.reset], None


@bp.route("/<int:issuer_id>/numerotation/exemple")
@require("issuer.manage")
def number_example(issuer_id):
    """Exemple mis à jour pendant la saisie (htmx)."""
    db.session.get(Issuer, issuer_id) or abort(404)
    name = request.args.get("serie", "")
    if name not in FORMAT_LABELS:
        abort(400)
    n = request.args.get(name.replace("_format", "_next"), type=int)
    sample, reset, error = _example(request.args.get(name, ""), n)
    return render_template("issuers/_number_example.html", sample=sample, reset=reset, error=error)


@bp.route("/logo/<name>")
@require("invoice.view")
def logo_file(name):
    if not re.fullmatch(r"[0-9a-f]{32}\.png", name):
        abort(404)
    from pathlib import Path

    return send_from_directory(Path(current_app.config["UPLOAD_DIR"]) / "logo", name)
