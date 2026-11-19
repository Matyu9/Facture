"""Clients : structures (mairies, comités, entreprises) et particuliers (familles d'adhérents)."""

from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional, Regexp

from ..extensions import db
from ..models import Activity, Customer, Invoice
from ..permissions import can, require
from ..services import billing, customers
from ..services.activity import diff, log

bp = Blueprint("customers", __name__, url_prefix="/clients")

PER_PAGE = 100


def _strip(value):
    return " ".join(value.split()) if isinstance(value, str) else value


class CustomerForm(FlaskForm):
    kind = SelectField("Type", choices=list(Customer.KINDS.items()), default=Customer.ORGANISATION)
    name = StringField(
        "Nom",
        validators=[DataRequired(), Length(max=200)],
        filters=[_strip],
        description="Raison sociale d'une structure, ou prénom et nom d'un particulier.",
    )
    contact_name = StringField("À l'attention de", validators=[Optional(), Length(max=160)], filters=[_strip])
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
    address = StringField("Adresse", validators=[Optional(), Length(max=255)], filters=[_strip])
    address_extra = StringField("Complément d'adresse", validators=[Optional(), Length(max=255)], filters=[_strip])
    postcode = StringField("Code postal", validators=[Optional(), Length(max=20)], filters=[_strip])
    city = StringField("Ville", validators=[Optional(), Length(max=120)], filters=[_strip])
    country = StringField("Pays", validators=[Optional(), Length(max=80)], filters=[_strip], default="France")
    siret = StringField(
        "SIRET",
        validators=[Optional(), Regexp(r"^\d{14}$", message="14 chiffres.")],
        filters=[lambda v: "".join(v.split()) if isinstance(v, str) else v],
    )
    vat_number = StringField(
        "N° de TVA intracommunautaire",
        validators=[Optional(), Length(max=30)],
        filters=[lambda v: "".join(v.split()).upper() if isinstance(v, str) else v],
    )
    notes = TextAreaField(
        "Notes internes", validators=[Optional(), Length(max=2000)], description="Jamais imprimées sur les factures."
    )


FIELDS = (*customers.FIELDS, "notes")


def _values(form):
    values = {field: getattr(form, field).data for field in FIELDS}
    values = {k: (v.strip() if isinstance(v, str) else v) or None for k, v in values.items()}
    if values.get("email"):
        values["email"] = values["email"].lower()
    values["kind"] = values["kind"] or Customer.ORGANISATION
    return values


def _customer(customer_id):
    return db.session.get(Customer, customer_id) or abort(404)


@bp.route("")
@require("invoice.view")
def index():
    q = (request.args.get("q") or "").strip()
    archived = request.args.get("archives") == "1"
    query = customers.search(q, include_archived=archived)
    if archived:
        query = query.where(Customer.archived_at.is_not(None))
    total = db.session.scalar(db.select(db.func.count()).select_from(query.subquery()))
    page = max(request.args.get("page", 1, type=int), 1)
    items = db.session.scalars(query.limit(PER_PAGE).offset((page - 1) * PER_PAGE)).all()
    balances = _balances([c.id for c in items])
    return render_template(
        "customers/index.html",
        items=items,
        total=total,
        q=q,
        archived=archived,
        page=page,
        pages=max((total + PER_PAGE - 1) // PER_PAGE, 1),
        balances=balances,
        edit=can(current_user, "invoice.edit"),
    )


def _balances(ids):
    """Reste à payer par client, sur ses factures émises."""
    if not ids:
        return {}
    invoices = db.session.scalars(
        db.select(Invoice).where(
            Invoice.customer_id.in_(ids), Invoice.kind == Invoice.INVOICE, Invoice.status == Invoice.ISSUED
        )
    ).all()
    out = {}
    for invoice in invoices:
        out[invoice.customer_id] = out.get(invoice.customer_id, 0) + invoice.balance_cents
    return out


@bp.route("/nouveau", methods=["GET", "POST"])
@require("invoice.edit")
def create():
    form = CustomerForm()
    if form.validate_on_submit():
        customer = Customer(**_values(form))
        db.session.add(customer)
        db.session.flush()
        log("customer.create", f"Client créé : {customer.name}", customer=customer)
        db.session.commit()
        flash(f"Client « {customer.name} » créé.", "success")
        if request.args.get("facturer") == "1":
            return redirect(url_for("invoices.create", client=customer.id))
        return redirect(url_for(".detail", customer_id=customer.id))
    return render_template("customers/form.html", form=form, customer=None)


@bp.route("/<int:customer_id>")
@require("invoice.view")
def detail(customer_id):
    customer = _customer(customer_id)
    invoices = db.session.scalars(
        db.select(Invoice)
        .where(Invoice.customer_id == customer.id)
        .order_by(Invoice.issue_date.desc().nulls_first(), Invoice.id.desc())
    ).all()
    history = db.session.scalars(
        db.select(Activity)
        .where(Activity.customer_id == customer.id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(20)
    ).all()
    balance = sum(i.balance_cents for i in invoices if i.kind == Invoice.INVOICE and not i.is_draft)
    return render_template(
        "customers/detail.html",
        customer=customer,
        invoices=invoices,
        history=history,
        balance=balance,
        state=billing.state,
        states=billing.STATES,
        tones=billing.TONES,
        edit=can(current_user, "invoice.edit"),
    )


@bp.route("/<int:customer_id>/modifier", methods=["GET", "POST"])
@require("invoice.edit")
def edit(customer_id):
    customer = _customer(customer_id)
    form = CustomerForm(obj=customer)
    if form.validate_on_submit():
        changes = diff(customer, FIELDS, _values(form), mask=("notes",))
        if changes:
            log("customer.update", f"Client modifié : {customer.name}", changes=changes, customer=customer)
        db.session.commit()
        flash("Client enregistré. Les pièces déjà émises gardent l'adresse d'origine.", "success")
        return redirect(url_for(".detail", customer_id=customer.id))
    return render_template("customers/form.html", form=form, customer=customer)


@bp.route("/<int:customer_id>/archiver", methods=["POST"])
@require("invoice.edit")
def archive(customer_id):
    customer = _customer(customer_id)
    if customer.archived:
        customer.archived_at = None
        log("customer.restore", f"Client rétabli : {customer.name}", customer=customer)
        flash(f"« {customer.name} » est rétabli.", "success")
    else:
        customer.archived_at = datetime.now()
        log("customer.archive", f"Client archivé : {customer.name}", customer=customer)
        flash(f"« {customer.name} » est archivé : il n'est plus proposé pour de nouvelles factures.", "success")
    db.session.commit()
    return redirect(url_for(".detail", customer_id=customer.id))


@bp.route("/<int:customer_id>/supprimer", methods=["POST"])
@require("invoice.edit")
def delete(customer_id):
    customer = _customer(customer_id)
    if customer.invoices.count():
        flash("Ce client a des pièces (même en brouillon) : archivez-le plutôt.", "error")
        return redirect(url_for(".detail", customer_id=customer.id))
    name = customer.name
    db.session.delete(customer)
    log("customer.delete", f"Client supprimé : {name}")
    db.session.commit()
    flash(f"Client « {name} » supprimé.", "success")
    return redirect(url_for(".index"))
