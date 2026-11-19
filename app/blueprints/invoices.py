"""Factures et avoirs : brouillons, émission, PDF, envoi, règlements, avoirs, export."""

import csv
import io
from datetime import date
from decimal import Decimal

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import or_
from wtforms import BooleanField, DateField, RadioField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional, Regexp

from ..extensions import db, fold_contains
from ..models import RECIPIENT_FIELDS, Activity, Customer, Invoice, Issuer, Payment, Product
from ..permissions import can, require
from ..services import archive, billing, customers, invoice_mail, money, pdf
from ..services.activity import log
from .customers import CustomerForm
from .products import choices_for

bp = Blueprint("invoices", __name__, url_prefix="/factures")

PER_PAGE = 100


# ---------------------------------------------------------------------------
# Formulaires
# ---------------------------------------------------------------------------


CLIENT, ONE_OFF = "client", "ponctuel"


class RecipientForm(CustomerForm):
    """Destinataire ponctuel : les coordonnées imprimées d'une fiche client, saisies sur la pièce."""

    class Meta:
        csrf = False  # sous-formulaire : le jeton est porté par le formulaire principal

    phone = None
    notes = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, prefix="dest", **kwargs)
        if not self.is_submitted() and not kwargs.get("data"):
            self.kind.data = Customer.PERSON

    def values(self):
        return {field: getattr(self, field).data for field in RECIPIENT_FIELDS}


class RecipientChoice:
    """Champs communs : client enregistré, ou destinataire saisi sur la pièce (et, au besoin, enregistré)."""

    recipient_mode = RadioField(
        "Destinataire",
        choices=[(CLIENT, "Un client enregistré"), (ONE_OFF, "Un autre destinataire, saisi sur la facture")],
        default=CLIENT,
    )
    # Vérifié par _chosen_recipient, et seulement pour un client enregistré : absent ou vide, il vaut 0.
    customer_id = SelectField("Client", coerce=lambda v: int(v) if str(v or "").isdigit() else 0, validate_choice=False)
    save_customer = BooleanField("L'enregistrer aussi dans les clients")

    def _customer_choices(self, current=None):
        self.customer_id.choices = [(0, "— Choisir —")] + [
            (c.id, c.name + (f" ({c.city})" if c.city else "")) for c in customers.active()
        ]
        if current is not None and current.archived:
            self.customer_id.choices.insert(1, (current.id, current.name + " (archivé)"))


class NewForm(RecipientChoice, FlaskForm):
    issuer_id = SelectField("Émetteur", coerce=int, validators=[DataRequired(message="Choisissez un émetteur.")])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.issuer_id.choices = [(i.id, i.name) for i in billing.issuers()]
        self._customer_choices()


class HeaderForm(RecipientChoice, FlaskForm):
    issuer_id = SelectField("Émetteur", coerce=int)
    subject = StringField("Objet", validators=[Optional(), Length(max=255)])
    service_date = DateField("Date de la prestation", validators=[Optional()])
    service_end = DateField("Jusqu'au (période)", validators=[Optional()])
    due_date = DateField("Échéance", validators=[Optional()])
    customer_reference = StringField("Référence du client", validators=[Optional(), Length(max=80)])
    notes = TextAreaField("Remarques imprimées", validators=[Optional(), Length(max=2000)])
    internal_note = TextAreaField("Note interne", validators=[Optional(), Length(max=2000)])

    def __init__(self, *args, invoice=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.issuer_id.choices = [(i.id, i.name) for i in billing.issuers()]
        if invoice is not None and invoice.issuer.archived:
            self.issuer_id.choices.insert(0, (invoice.issuer.id, invoice.issuer.name + " (archivé)"))
        self._customer_choices(invoice.customer if invoice is not None else None)


class IssueForm(FlaskForm):
    issue_date = DateField("Date d'émission", validators=[DataRequired()], default=date.today)


class PaymentForm(FlaskForm):
    amount = StringField("Montant", validators=[DataRequired()])
    paid_on = DateField("Reçu le", validators=[DataRequired()], default=date.today)
    method = SelectField("Mode", choices=list(Payment.METHODS.items()))
    reference = StringField("Référence", validators=[Optional(), Length(max=120)])
    note = StringField("Note", validators=[Optional(), Length(max=255)])


class SendForm(FlaskForm):
    to = StringField(
        "Destinataire",
        validators=[
            DataRequired(),
            Regexp(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", message="Adresse email invalide."),
            Length(max=255),
        ],
    )
    subject = StringField("Objet", validators=[DataRequired(), Length(max=200)])
    body = TextAreaField("Message", validators=[DataRequired(), Length(max=8000)])


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------


def _invoice(invoice_id):
    return db.session.get(Invoice, invoice_id) or abort(404)


def _draft(invoice_id):
    invoice = _invoice(invoice_id)
    if not invoice.is_draft:
        flash("Cette pièce est émise : elle ne se modifie plus. Faites un avoir pour la corriger.", "error")
        abort(redirect(url_for(".detail", invoice_id=invoice.id)))
    return invoice


def _chosen_recipient(form, dest):
    """Destinataire choisi dans le formulaire : (ok, client, coordonnées ponctuelles). Un destinataire saisi qu'on
    demande d'enregistrer devient un client (créé ici, sans commit). Les erreurs sont posées sur les champs."""
    if form.recipient_mode.data == ONE_OFF:
        if not dest.validate():
            return False, None, None
        values = dest.values()
        if not form.save_customer.data:
            return True, None, values
        customer = Customer(**billing.clean_recipient(values))
        db.session.add(customer)
        db.session.flush()
        log("customer.create", f"Client créé depuis une facture : {customer.name}", customer=customer)
        return True, customer, None
    customer = db.session.get(Customer, form.customer_id.data or 0)
    if customer is None:
        form.customer_id.errors = [*form.customer_id.errors, "Choisissez un client."]
        return False, None, None
    return True, customer, None


def _parse_lines(form, issuer):
    """Lignes envoyées par le formulaire (listes parallèles). Renvoie (entrées valides, lignes à réafficher, erreurs).

    Une ligne sans désignation ni prix est ignorée : c'est ainsi qu'on retire une ligne sans JavaScript."""
    labels = form.getlist("label")
    columns = {
        name: form.getlist(name) for name in ("description", "quantity", "unit", "unit_price", "vat_rate", "product_id")
    }
    products = {p.id: p for p in db.session.scalars(db.select(Product))}
    inputs, rows, errors = [], [], []
    for i, label in enumerate(labels):
        row = {name: (values[i] if i < len(values) else "") for name, values in columns.items()}
        row["label"] = label
        product = products.get(int(row["product_id"])) if row["product_id"].isdigit() else None
        if product is not None and not label.strip():
            # Article choisi sans JavaScript : la ligne se remplit au prochain enregistrement.
            row.update(
                label=product.label,
                description=row["description"] or (product.description or ""),
                unit=row["unit"] or (product.unit or ""),
                unit_price=row["unit_price"] or money.amount_input(product.unit_price_cents),
                vat_rate=row["vat_rate"] or money.number(billing.default_vat_rate(issuer, product)),
            )
        if not row["label"].strip() and not row["unit_price"].strip():
            continue
        rows.append(row)
        n = len(rows)
        line_errors = []
        if not row["label"].strip():
            line_errors.append(f"Ligne {n} : la désignation manque.")
        try:
            quantity = money.parse_decimal(row["quantity"] or "1", places=3)
        except ValueError as exc:
            line_errors.append(f"Ligne {n}, quantité : {exc}")
            quantity = None
        try:
            price = money.parse_amount(row["unit_price"] or "0")
        except ValueError as exc:
            line_errors.append(f"Ligne {n}, prix : {exc}")
            price = None
        if issuer.subject_to_vat:
            try:
                rate = money.parse_decimal(row["vat_rate"] or str(issuer.default_vat_rate), places=2)
                if not 0 <= rate <= 100:
                    raise ValueError("entre 0 et 100.")
            except ValueError as exc:
                line_errors.append(f"Ligne {n}, TVA : {exc}")
                rate = None
        else:
            rate = Decimal("0")
        if quantity is not None and abs(quantity) >= 10**9:
            line_errors.append(f"Ligne {n} : quantité trop grande.")
        errors += line_errors
        if not line_errors:
            inputs.append(
                billing.LineInput(
                    label=row["label"],
                    description=row["description"],
                    quantity=quantity,
                    unit=row["unit"],
                    unit_price_cents=price,
                    vat_rate=rate,
                    product_id=product.id if product else None,
                )
            )
    return inputs, rows, errors


def _rows_of(invoice):
    return [
        {
            "label": line.label,
            "description": line.description or "",
            "quantity": money.number(line.quantity).replace(" ", ""),
            "unit": line.unit or "",
            "unit_price": money.amount_input(line.unit_price_cents),
            "vat_rate": money.number(line.vat_rate),
            "product_id": str(line.product_id or ""),
        }
        for line in invoice.lines
    ]


def _filters():
    args = request.args
    query = db.select(Invoice).outerjoin(Customer)
    q = (args.get("q") or "").strip()
    if q:
        query = query.where(
            or_(
                fold_contains(Customer.name, q),
                fold_contains(Invoice.customer_snapshot["name"].as_string(), q),
                fold_contains(Invoice.subject, q),
                Invoice.number.ilike(f"%{q}%"),
            )
        )
    issuer = db.session.get(Issuer, args.get("emetteur", type=int) or 0)
    if issuer:
        query = query.where(Invoice.issuer_id == issuer.id)
    kind = args.get("type")
    if kind in Invoice.KINDS:
        query = query.where(Invoice.kind == kind)
    year = args.get("annee", type=int)
    if year:
        query = query.where(
            or_(
                db.extract("year", Invoice.issue_date) == year,
                (Invoice.status == Invoice.DRAFT) & (db.extract("year", Invoice.created_at) == year),
            )
        )
    since, until = _date_arg("du"), _date_arg("au")
    if since:
        query = query.where(Invoice.issue_date >= since)
    if until:
        query = query.where(Invoice.issue_date <= until)
    wanted = args.get("etat")
    if wanted == billing.DRAFT:
        query = query.where(Invoice.status == Invoice.DRAFT)
    elif wanted in billing.STATES:
        query = query.where(Invoice.status == Invoice.ISSUED)
    params = {
        k: v
        for k, v in {
            "q": q,
            "emetteur": issuer.id if issuer else None,
            "type": kind if kind in Invoice.KINDS else None,
            "annee": year,
            "du": since.isoformat() if since else None,
            "au": until.isoformat() if until else None,
            "etat": wanted if wanted in billing.STATES else None,
        }.items()
        if v
    }
    query = query.order_by(
        (Invoice.status == Invoice.DRAFT).desc(), Invoice.issue_date.desc().nulls_first(), Invoice.id.desc()
    )
    return query, params, wanted if wanted in billing.STATES else None


def _date_arg(name):
    try:
        return date.fromisoformat(request.args.get(name) or "")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Liste et export
# ---------------------------------------------------------------------------


@bp.route("")
@require("invoice.view")
def index():
    query, params, wanted = _filters()
    items = db.session.scalars(query).all()
    if wanted and wanted != billing.DRAFT:
        items = [i for i in items if billing.state(i) == wanted]
    total = len(items)
    page = max(request.args.get("page", 1, type=int), 1)
    shown = items[(page - 1) * PER_PAGE : page * PER_PAGE]
    issued = [i for i in items if not i.is_draft]
    summary = {
        "invoiced": sum(i.total_ttc_cents for i in issued if not i.is_credit),
        "credited": sum(i.total_ttc_cents for i in issued if i.is_credit),
        "paid": sum(i.paid_cents for i in issued if not i.is_credit),
        "due": sum(i.balance_cents for i in issued if not i.is_credit),
    }
    years = sorted(
        {y for (y,) in db.session.execute(db.select(db.extract("year", Invoice.issue_date)).distinct()) if y}
        | {date.today().year},
        reverse=True,
    )
    return render_template(
        "invoices/index.html",
        items=shown,
        total=total,
        page=page,
        pages=max((total + PER_PAGE - 1) // PER_PAGE, 1),
        params=params,
        summary=summary,
        issuers=billing.issuers(include_archived=True),
        years=years,
        states=billing.STATES,
        state=billing.state,
        tones=billing.TONES,
        kinds=Invoice.KINDS,
        edit=can(current_user, "invoice.edit"),
    )


@bp.route("/export.csv")
@require("invoice.view")
def export():
    """Journal des ventes : une ligne par pièce émise (et une par taux de TVA), lisible par un tableur ou un logiciel
    de comptabilité. Les avoirs sont en négatif."""
    query, params, wanted = _filters()
    query = (
        query.where(Invoice.status == Invoice.ISSUED)
        .order_by(None)
        .order_by(Invoice.issue_date, Invoice.issued_at, Invoice.id)
    )
    items = db.session.scalars(query).all()
    if wanted and wanted != billing.DRAFT:
        items = [i for i in items if billing.state(i) == wanted]
    out = io.StringIO()
    out.write("﻿")  # Excel reconnaît l'UTF-8
    writer = csv.writer(out, delimiter=";")
    writer.writerow(
        [
            "Date",
            "Numéro",
            "Type",
            "Émetteur",
            "Client",
            "Objet",
            "Taux TVA",
            "Total HT",
            "TVA",
            "Total TTC",
            "Échéance",
            "Réglé",
            "Reste à payer",
            "Facture corrigée",
        ]
    )

    def fmt(cents, sign=1):
        return money.amount_input(cents * sign)

    for invoice in items:
        sign = -1 if invoice.is_credit else 1
        breakdown = invoice.vat_breakdown or [["0", invoice.total_ht_cents, invoice.total_vat_cents]]
        for i, (rate, base, vat) in enumerate(breakdown):
            first = i == 0
            writer.writerow(
                [
                    invoice.issue_date.strftime("%d/%m/%Y"),
                    invoice.number,
                    invoice.kind_label,
                    (invoice.issuer_snapshot or {}).get("legal_name") or invoice.issuer.legal_name,
                    invoice.recipient_name,
                    invoice.subject or "",
                    money.number(rate),
                    fmt(base, sign),
                    fmt(vat, sign),
                    fmt(base + vat, sign),
                    invoice.due_date.strftime("%d/%m/%Y") if invoice.due_date and first else "",
                    fmt(invoice.paid_cents) if first and not invoice.is_credit else "",
                    fmt(invoice.balance_cents) if first and not invoice.is_credit else "",
                    invoice.credited_invoice.number if invoice.credited_invoice else "",
                ]
            )
    log("invoice.export", f"Export du journal des ventes ({len(items)} pièce(s))")
    db.session.commit()
    name = f"journal-des-ventes-{date.today():%Y%m%d}.csv"
    return Response(
        out.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Brouillons
# ---------------------------------------------------------------------------


@bp.route("/nouvelle", methods=["GET", "POST"])
@require("invoice.edit")
def create():
    form = NewForm()
    dest = RecipientForm()
    if request.method == "GET":
        form.customer_id.data = request.args.get("client", type=int) or 0
        if request.args.get("destinataire") == ONE_OFF:
            form.recipient_mode.data = ONE_OFF
        if request.args.get("emetteur", type=int):
            form.issuer_id.data = request.args.get("emetteur", type=int)
    if not form.issuer_id.choices:
        flash("Créez d'abord un émetteur : l'association ou l'une de ses régies.", "warning")
        return redirect(url_for("issuers.index"))
    if form.validate_on_submit():
        issuer = db.session.get(Issuer, form.issuer_id.data) or abort(400)
        ok, customer, recipient = _chosen_recipient(form, dest)
        if ok:
            try:
                invoice = billing.new_draft(issuer, customer, by=current_user, recipient=recipient)
            except billing.BillingError as exc:
                db.session.rollback()
                flash(str(exc), "error")
            else:
                invoice.service_date = date.today()
                db.session.flush()
                log(
                    "invoice.create",
                    f"Brouillon de facture pour {invoice.recipient_name}",
                    invoice=invoice,
                    customer=customer,
                )
                db.session.commit()
                return redirect(url_for(".edit", invoice_id=invoice.id))
    return render_template("invoices/new.html", form=form, dest=dest)


@bp.route("/<int:invoice_id>/modifier", methods=["GET", "POST"])
@require("invoice.edit")
def edit(invoice_id):
    invoice = _draft(invoice_id)
    form = HeaderForm(obj=invoice, invoice=invoice)
    dest = RecipientForm(data=invoice.recipient if invoice.is_one_off else None)
    if request.method == "GET":
        form.recipient_mode.data = ONE_OFF if invoice.is_one_off else CLIENT
        form.customer_id.data = invoice.customer_id or 0
    rows = _rows_of(invoice)
    errors = []
    if form.validate_on_submit():
        issuer = db.session.get(Issuer, form.issuer_id.data) or abort(400)
        inputs, rows, errors = _parse_lines(request.form, issuer)
        if form.service_end.data and form.service_date.data and form.service_end.data < form.service_date.data:
            errors.append("La fin de la période est avant son début.")
        # Un avoir garde le destinataire de la facture qu'il corrige : le formulaire ne le propose pas.
        ok, customer, recipient = (True, invoice.customer, None) if invoice.is_credit else _chosen_recipient(form, dest)
        if not ok:
            errors.append("Destinataire incomplet : voir les champs signalés.")
        if not errors:
            try:
                if issuer.id != invoice.issuer_id:
                    billing.change_issuer(invoice, issuer)
                if not invoice.is_credit:
                    if customer is not None:
                        billing.set_customer(invoice, customer)
                    else:
                        billing.set_recipient(invoice, recipient)
                for name in ("subject", "customer_reference", "notes", "internal_note"):
                    setattr(invoice, name, (getattr(form, name).data or "").strip() or None)
                invoice.service_date = form.service_date.data
                invoice.service_end = form.service_end.data
                invoice.due_date = None if invoice.is_credit else form.due_date.data
                billing.set_lines(invoice, inputs)
            except billing.BillingError as exc:
                errors.append(str(exc))
                db.session.rollback()
                invoice = _invoice(invoice_id)
        else:
            db.session.rollback()  # un client créé au passage (« l'enregistrer aussi ») ne reste pas
            invoice = _invoice(invoice_id)
        if not errors:
            log(
                "invoice.update",
                f"Brouillon modifié ({money.amount(invoice.total_ttc_cents)})",
                invoice=invoice,
                customer=invoice.customer,
            )
            db.session.commit()
            flash("Brouillon enregistré.", "success")
            if request.form.get("then") == "emettre":
                return redirect(url_for(".detail", invoice_id=invoice.id) + "#emettre")
            return redirect(
                url_for(".edit", invoice_id=invoice.id)
                if request.form.get("then") == "rester"
                else url_for(".detail", invoice_id=invoice.id)
            )
    elif request.method == "POST":
        _, rows, errors = _parse_lines(request.form, invoice.issuer)
    issuer = invoice.issuer
    return render_template(
        "invoices/edit.html",
        invoice=invoice,
        form=form,
        dest=dest,
        rows=rows,
        errors=errors,
        products=choices_for(issuer),
        subject_to_vat=issuer.subject_to_vat,
        default_rate=money.number(issuer.default_vat_rate),
        default_vat_rate=billing.default_vat_rate,
        money=money,
    )


@bp.route("/<int:invoice_id>/supprimer", methods=["POST"])
@require("invoice.edit")
def delete(invoice_id):
    invoice = _draft(invoice_id)
    customer = invoice.customer
    label = f"{invoice.kind_label} en brouillon pour {invoice.recipient_name}"
    billing.delete_draft(invoice)
    log("invoice.delete", f"{label} supprimé(e)", customer=customer)
    db.session.commit()
    flash("Brouillon supprimé.", "success")
    return redirect(url_for(".index"))


@bp.route("/<int:invoice_id>/dupliquer", methods=["POST"])
@require("invoice.edit")
def duplicate(invoice_id):
    source = _invoice(invoice_id)
    try:
        copy = billing.duplicate(source, by=current_user)
    except billing.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for(".detail", invoice_id=source.id))
    copy.service_date = date.today()
    db.session.flush()
    log("invoice.create", f"Brouillon copié de {source.display_number}", invoice=copy, customer=copy.customer)
    db.session.commit()
    flash("Copie créée : vérifiez les dates et les montants avant d'émettre.", "success")
    return redirect(url_for(".edit", invoice_id=copy.id))


# ---------------------------------------------------------------------------
# Fiche, PDF, émission
# ---------------------------------------------------------------------------


@bp.route("/<int:invoice_id>")
@require("invoice.view")
def detail(invoice_id):
    invoice = _invoice(invoice_id)
    history = db.session.scalars(
        db.select(Activity)
        .where(Activity.invoice_id == invoice.id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(30)
    ).all()
    issue_form = IssueForm()
    payment_form = PaymentForm(formdata=None)
    if not invoice.is_draft and not invoice.is_credit:
        payment_form.amount.data = money.amount_input(invoice.balance_cents)
    return render_template(
        "invoices/detail.html",
        invoice=invoice,
        state=billing.state(invoice),
        states=billing.STATES,
        tones=billing.TONES,
        problems=billing.problems(invoice) if invoice.is_draft else [],
        history=history,
        issue_form=issue_form,
        payment_form=payment_form,
        creditable=billing.creditable_cents(invoice) if not invoice.is_draft and not invoice.is_credit else 0,
        money=money,
    )


@bp.route("/<int:invoice_id>/pdf")
@require("invoice.view")
def pdf_file(invoice_id):
    invoice = _invoice(invoice_id)
    if invoice.is_draft:
        content = pdf.render(invoice)
    else:
        try:
            content = archive.read(invoice)
        except archive.ArchiveError as exc:
            flash(str(exc), "error")
            return redirect(url_for(".detail", invoice_id=invoice.id))
    disposition = "attachment" if request.args.get("telecharger") == "1" else "inline"
    return Response(
        content,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="{archive.filename(invoice)}"',
            "Cache-Control": "private, no-store",
        },
    )


@bp.route("/<int:invoice_id>/emettre", methods=["POST"])
@require("invoice.issue")
def issue(invoice_id):
    invoice = _draft(invoice_id)
    form = IssueForm()
    if not form.validate_on_submit():
        flash("Date d'émission invalide.", "error")
        return redirect(url_for(".detail", invoice_id=invoice.id))
    try:
        billing.issue(invoice, by=current_user, issue_date=form.issue_date.data)
    except billing.BillingError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for(".detail", invoice_id=invoice_id))
    log(
        "invoice.issue",
        f"{invoice.kind_label} {invoice.number} émis(e) : {money.amount(invoice.total_ttc_cents)} pour "
        f"{invoice.recipient_name}",
        invoice=invoice,
        customer=invoice.customer,
    )
    db.session.commit()
    flash(f"{invoice.kind_label} {invoice.number} émis(e). Le PDF est archivé.", "success")
    return redirect(url_for(".detail", invoice_id=invoice.id))


@bp.route("/<int:invoice_id>/envoyer", methods=["GET", "POST"])
@require("invoice.issue")
def send(invoice_id):
    invoice = _invoice(invoice_id)
    if invoice.is_draft:
        flash("Émettez la pièce avant de l'envoyer.", "error")
        return redirect(url_for(".detail", invoice_id=invoice.id))
    reminder = request.args.get("relance") == "1" and invoice.balance_cents > 0
    form = SendForm()
    if request.method == "GET":
        form.to.data = invoice.recipient.get("email") or ""
        form.subject.data, form.body.data = invoice_mail.default_message(invoice, reminder=reminder)
    if form.validate_on_submit():
        try:
            invoice_mail.send(invoice, form.to.data.strip(), form.subject.data, form.body.data)
        except invoice_mail.SendError as exc:
            flash(str(exc), "error")
        else:
            log(
                "invoice.reminder" if reminder else "invoice.send",
                f"{'Relance' if reminder else 'Envoi'} de {invoice.number} à {invoice.sent_to}",
                invoice=invoice,
                customer=invoice.customer,
            )
            db.session.commit()
            flash(f"{invoice.kind_label} {invoice.number} envoyé(e) à {invoice.sent_to}.", "success")
            return redirect(url_for(".detail", invoice_id=invoice.id))
    return render_template("invoices/send.html", invoice=invoice, form=form, reminder=reminder)


@bp.route("/<int:invoice_id>/enregistrer-client", methods=["POST"])
@require("invoice.edit")
def save_customer(invoice_id):
    """Le destinataire ponctuel devient un client enregistré ; ses pièces y sont rattachées."""
    invoice = _invoice(invoice_id)
    try:
        customer = billing.save_recipient_as_customer(invoice)
    except billing.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for(".detail", invoice_id=invoice.id))
    log(
        "customer.create",
        f"Client créé depuis {invoice.kind_label.lower()} {invoice.display_number} : {customer.name}",
        invoice=invoice,
        customer=customer,
    )
    db.session.commit()
    flash(f"« {customer.name} » est enregistré dans les clients.", "success")
    return redirect(url_for(".detail", invoice_id=invoice.id))


# ---------------------------------------------------------------------------
# Avoirs et règlements
# ---------------------------------------------------------------------------


@bp.route("/<int:invoice_id>/avoir", methods=["POST"])
@require("invoice.edit")
def credit(invoice_id):
    invoice = _invoice(invoice_id)
    try:
        credit_note = billing.new_credit_note(invoice, by=current_user)
    except billing.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for(".detail", invoice_id=invoice.id))
    db.session.flush()
    log(
        "invoice.create",
        f"Brouillon d'avoir sur la facture {invoice.number}",
        invoice=credit_note,
        customer=invoice.customer,
    )
    db.session.commit()
    flash(
        "Avoir préparé avec toutes les lignes de la facture : réduisez-les pour un avoir partiel, puis émettez-le.",
        "info",
    )
    return redirect(url_for(".edit", invoice_id=credit_note.id))


@bp.route("/<int:invoice_id>/reglements", methods=["POST"])
@require("payment.edit")
def add_payment(invoice_id):
    invoice = _invoice(invoice_id)
    form = PaymentForm()
    if not form.validate_on_submit():
        flash("Règlement incomplet : montant, date et mode sont obligatoires.", "error")
        return redirect(url_for(".detail", invoice_id=invoice.id) + "#reglements")
    try:
        cents = money.parse_amount(form.amount.data)
        payment = billing.add_payment(
            invoice,
            cents,
            form.paid_on.data,
            form.method.data,
            reference=form.reference.data,
            note=form.note.data,
            by=current_user,
        )
    except (ValueError, billing.BillingError) as exc:
        flash(str(exc), "error")
        return redirect(url_for(".detail", invoice_id=invoice.id) + "#reglements")
    log(
        "payment.create",
        f"Règlement de {money.amount(payment.amount_cents)} ({payment.method_label}) sur {invoice.number}",
        invoice=invoice,
        customer=invoice.customer,
    )
    db.session.commit()
    flash(
        "Règlement enregistré : la facture est soldée." if invoice.balance_cents == 0 else "Règlement enregistré.",
        "success",
    )
    return redirect(url_for(".detail", invoice_id=invoice.id) + "#reglements")


@bp.route("/<int:invoice_id>/reglements/<int:payment_id>/supprimer", methods=["POST"])
@require("payment.edit")
def delete_payment(invoice_id, payment_id):
    invoice = _invoice(invoice_id)
    payment = db.session.get(Payment, payment_id)
    if payment is None or payment.invoice_id != invoice.id:
        abort(404)
    log(
        "payment.delete",
        f"Règlement de {money.amount(payment.amount_cents)} du {payment.paid_on:%d/%m/%Y} supprimé sur {invoice.number}",
        invoice=invoice,
        customer=invoice.customer,
    )
    db.session.delete(payment)
    db.session.commit()
    flash("Règlement supprimé.", "success")
    return redirect(url_for(".detail", invoice_id=invoice.id) + "#reglements")


def recent(limit=8):
    return db.session.scalars(
        db.select(Invoice).order_by(Invoice.updated_at.desc(), Invoice.id.desc()).limit(limit)
    ).all()
