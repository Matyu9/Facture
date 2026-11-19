"""Articles : prestations et produits qui reviennent souvent, pour remplir une ligne de facture en un clic."""

from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional, ValidationError

from ..extensions import db
from ..models import InvoiceLine, Product
from ..permissions import can, require
from ..services import billing, money
from ..services.activity import diff, log

bp = Blueprint("products", __name__, url_prefix="/articles")


class ProductForm(FlaskForm):
    label = StringField("Désignation", validators=[DataRequired(), Length(max=200)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=2000)])
    unit = StringField("Unité", validators=[Optional(), Length(max=30)], description="« forfait », « heure »…")
    unit_price = StringField("Prix unitaire (HT si TVA)", validators=[DataRequired()])
    vat_rate = StringField(
        "Taux de TVA (%)",
        validators=[Optional()],
        description="Seulement pour un émetteur assujetti. Vide : le taux par défaut de l'émetteur.",
    )
    issuer_id = SelectField("Émetteur", coerce=int)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.price_cents, self.rate = 0, None
        self.issuer_id.choices = [(0, "Tous les émetteurs")] + [(i.id, i.name) for i in billing.issuers()]

    def validate_unit_price(self, field):
        try:
            self.price_cents = money.parse_amount(field.data)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None

    def validate_vat_rate(self, field):
        self.rate = None
        if field.data and field.data.strip():
            try:
                self.rate = money.parse_decimal(field.data, places=2)
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
            if not 0 <= self.rate <= 100:
                raise ValidationError("Entre 0 et 100.")


def _values(form):
    return {
        "label": form.label.data.strip(),
        "description": (form.description.data or "").strip() or None,
        "unit": (form.unit.data or "").strip() or None,
        "unit_price_cents": form.price_cents,
        "vat_rate": form.rate,
        "issuer_id": form.issuer_id.data or None,
    }


@bp.route("")
@require("invoice.view")
def index():
    archived = request.args.get("archives") == "1"
    query = db.select(Product).order_by(db.func.fold(Product.label))
    query = query.where(Product.archived_at.is_not(None) if archived else Product.archived_at.is_(None))
    return render_template(
        "products/index.html",
        items=db.session.scalars(query).all(),
        archived=archived,
        edit=can(current_user, "invoice.edit"),
    )


@bp.route("/nouveau", methods=["GET", "POST"])
@require("invoice.edit")
def create():
    form = ProductForm()
    if form.validate_on_submit():
        product = Product(**_values(form))
        db.session.add(product)
        log("product.create", f"Article créé : {product.label} ({money.amount(product.unit_price_cents)})")
        db.session.commit()
        flash(f"Article « {product.label} » créé.", "success")
        return redirect(url_for(".index"))
    return render_template("products/form.html", form=form, product=None)


@bp.route("/<int:product_id>/modifier", methods=["GET", "POST"])
@require("invoice.edit")
def edit(product_id):
    product = db.session.get(Product, product_id) or abort(404)
    form = ProductForm(
        obj=product,
        data={
            "unit_price": money.amount_input(product.unit_price_cents),
            "vat_rate": money.number(product.vat_rate) if product.vat_rate is not None else "",
            "issuer_id": product.issuer_id or 0,
        }
        if request.method == "GET"
        else None,
    )
    if form.validate_on_submit():
        values = _values(form)
        changes = diff(product, list(values), values)
        if changes:
            log("product.update", f"Article modifié : {product.label}", changes=changes)
        db.session.commit()
        flash("Article enregistré. Les factures déjà faites ne changent pas.", "success")
        return redirect(url_for(".index"))
    return render_template("products/form.html", form=form, product=product)


@bp.route("/<int:product_id>/archiver", methods=["POST"])
@require("invoice.edit")
def archive(product_id):
    product = db.session.get(Product, product_id) or abort(404)
    used = db.session.scalar(db.select(InvoiceLine.id).filter_by(product_id=product.id).limit(1))
    if not used and request.form.get("delete"):
        db.session.delete(product)
        log("product.delete", f"Article supprimé : {product.label}")
        flash(f"Article « {product.label} » supprimé.", "success")
    elif product.archived:
        product.archived_at = None
        log("product.restore", f"Article rétabli : {product.label}")
        flash(f"Article « {product.label} » rétabli.", "success")
    else:
        product.archived_at = datetime.now()
        log("product.archive", f"Article archivé : {product.label}")
        flash(f"Article « {product.label} » archivé.", "success")
    db.session.commit()
    return redirect(url_for(".index"))


def choices_for(issuer):
    """Articles proposés sur une facture de cet émetteur."""
    query = db.select(Product).where(Product.archived_at.is_(None)).order_by(db.func.fold(Product.label))
    if issuer is not None:
        query = query.where((Product.issuer_id.is_(None)) | (Product.issuer_id == issuer.id))
    return db.session.scalars(query).all()
