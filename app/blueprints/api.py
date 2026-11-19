"""API (/api/v1) : une autre appli de l'association (Adhérents, à terme) crée et lit des factures.

Authentification par jeton d'appli (Paramètres → API), dans l'en-tête `Authorization: Bearer fct_…`. Le cookie de
session n'est pas accepté. Un jeton sans le droit d'émettre ne crée que des brouillons, qu'une personne émet ensuite.
Une création porte une clé d'idempotence (`idempotency_key`) : rejouée, elle renvoie la même pièce sans en créer
une autre. Documentation : docs/api.md.
"""

from datetime import date

from flask import Blueprint, Response, abort, g, jsonify, request, url_for
from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import TooManyRequests, Unauthorized

from ..extensions import csrf, db
from ..models import Customer, Invoice, Issuer
from ..services import api_tokens, archive, billing, customers, money
from ..services.activity import log
from ..services.throttle import Limiter

bp = Blueprint("api", __name__, url_prefix="/api/v1")
csrf.exempt(bp)

SOURCE = "api"
MAX_BODY = 512 * 1024
PER_PAGE, MAX_PER_PAGE = 50, 200
_rate = Limiter("api", 600, 60)
_failures = Limiter("api-auth", 30, 15 * 60)


@bp.before_request
def authenticate():
    keys = (f"ip:{request.remote_addr}",)
    if _failures.blocked(keys):
        raise TooManyRequests("Trop de tentatives : patientez quelques minutes.", retry_after=900)
    scheme, _, raw = (request.headers.get("Authorization") or "").partition(" ")
    token = api_tokens.find(raw.strip()) if scheme.lower() == "bearer" else None
    if token is None:
        _failures.hit(keys)
        db.session.commit()
        raise Unauthorized(
            "Jeton d'accès absent, invalide ou révoqué.", www_authenticate=WWWAuthenticate("bearer", {"realm": "api"})
        )
    if _rate.blocked((f"token:{token.id}",)):
        raise TooManyRequests("Trop d'appels : patientez une minute.", retry_after=60)
    _rate.hit((f"token:{token.id}",))
    db.session.commit()
    g.api_token = token


@bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"  # données personnelles : pas de cache intermédiaire
    return response


# ---------------------------------------------------------------------------
# Représentation
# ---------------------------------------------------------------------------


def _issuer_json(issuer):
    return {
        "code": issuer.code,
        "name": issuer.name,
        "legal_name": issuer.legal_name,
        "vat_regime": issuer.vat_regime,
        "default_vat_rate": str(issuer.default_vat_rate) if issuer.subject_to_vat else "0",
        "archived": issuer.archived,
    }


def _customer_json(invoice):
    """Destinataire de la pièce : sa fiche client, ou, pour un destinataire ponctuel (saisi dans l'appli), ses
    coordonnées sans identifiant."""
    customer, recipient = invoice.customer, invoice.recipient
    return {
        "id": customer.id if customer else None,
        "source": customer.source if customer else None,
        "external_id": customer.external_id if customer else None,
        "kind": recipient.get("kind"),
        "name": recipient.get("name"),
        "email": recipient.get("email"),
        "city": recipient.get("city"),
    }


def _invoice_json(invoice):
    issued = not invoice.is_draft
    return {
        "id": invoice.id,
        "kind": invoice.kind,
        "status": invoice.status,
        "state": billing.state(invoice),
        "number": invoice.number,
        "issuer": invoice.issuer.code,
        "customer": _customer_json(invoice),
        "subject": invoice.subject,
        "external_id": invoice.external_id,
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "service_date": invoice.service_date.isoformat() if invoice.service_date else None,
        "service_end": invoice.service_end.isoformat() if invoice.service_end else None,
        "total_ht": invoice.total_ht_cents,
        "total_vat": invoice.total_vat_cents,
        "total_ttc": invoice.total_ttc_cents,
        "paid": invoice.paid_cents if issued and not invoice.is_credit else 0,
        "balance": invoice.balance_cents,
        "credited_invoice": invoice.credited_invoice.number if invoice.credited_invoice else None,
        "lines": [
            {
                "label": line.label,
                "description": line.description,
                "quantity": str(line.quantity.normalize()),
                "unit": line.unit,
                "unit_price": line.unit_price_cents,
                "vat_rate": str(line.vat_rate),
                "total_ht": line.total_ht_cents,
            }
            for line in invoice.lines
        ],
        "pdf_url": url_for("api.invoice_pdf", invoice_id=invoice.id, _external=False) if issued else None,
        "web_url": url_for("invoices.detail", invoice_id=invoice.id),
    }


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------


@bp.route("/emetteurs")
def issuers():
    return jsonify({"issuers": [_issuer_json(i) for i in billing.issuers(include_archived=True)]})


@bp.route("/factures")
def invoices():
    query = db.select(Invoice).outerjoin(Customer)
    if source := request.args.get("source"):
        query = query.where(Customer.source == source)
    if external := request.args.get("client"):
        query = query.where(Customer.external_id == external)
    if reference := request.args.get("reference"):
        query = query.where(Invoice.external_id == reference)
    if code := request.args.get("emetteur"):
        query = query.join(Issuer).where(Issuer.code == code)
    try:
        page = max(int(request.args.get("page", 1)), 1)
        per_page = min(max(int(request.args.get("par_page", PER_PAGE)), 1), MAX_PER_PAGE)
    except ValueError:
        abort(400, "« page » et « par_page » doivent être des nombres entiers.")
    total = db.session.scalar(db.select(db.func.count()).select_from(query.subquery()))
    items = db.session.scalars(query.order_by(Invoice.id.desc()).limit(per_page).offset((page - 1) * per_page)).all()
    return jsonify({"total": total, "page": page, "per_page": per_page, "invoices": [_invoice_json(i) for i in items]})


@bp.route("/factures/<int:invoice_id>")
def invoice(invoice_id):
    return jsonify(_invoice_json(db.session.get(Invoice, invoice_id) or abort(404)))


@bp.route("/factures/<int:invoice_id>/pdf")
def invoice_pdf(invoice_id):
    invoice = db.session.get(Invoice, invoice_id) or abort(404)
    if invoice.is_draft:
        abort(409, "Cette pièce est un brouillon : elle n'a pas encore de PDF.")
    try:
        content = archive.read(invoice)
    except archive.ArchiveError as exc:
        abort(500, str(exc))
    return Response(
        content,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{archive.filename(invoice)}"'},
    )


# ---------------------------------------------------------------------------
# Création
# ---------------------------------------------------------------------------


def _text(data, key, limit, required=False):
    value = data.get(key)
    if value is None or value == "":
        if required:
            abort(422, f"« {key} » est obligatoire.")
        return None
    if not isinstance(value, str):
        abort(422, f"« {key} » doit être un texte.")
    value = value.strip()
    if len(value) > limit:
        abort(422, f"« {key} » : {limit} caractères au plus.")
    return value or None


def _date(data, key):
    value = data.get(key)
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        abort(422, f"« {key} » doit être une date AAAA-MM-JJ.")


def _lines(raw, issuer):
    if not isinstance(raw, list) or not raw:
        abort(422, "« lines » doit être une liste non vide.")
    if len(raw) > billing.MAX_LINES:
        abort(422, f"{billing.MAX_LINES} lignes au plus.")
    out = []
    for n, line in enumerate(raw, start=1):
        if not isinstance(line, dict):
            abort(422, f"Ligne {n} : un objet est attendu.")
        price = line.get("unit_price")
        if not isinstance(price, int) or isinstance(price, bool) or abs(price) >= money.MAX_CENTS:
            abort(422, f"Ligne {n} : « unit_price » doit être un nombre entier de centimes.")
        try:
            quantity = money.parse_decimal(str(line.get("quantity", "1")), places=3)
        except ValueError:
            abort(422, f"Ligne {n} : « quantity » invalide.")
        if abs(quantity) >= 10**9:
            abort(422, f"Ligne {n} : « quantity » trop grande.")
        if issuer.subject_to_vat:
            try:
                rate = money.parse_decimal(str(line.get("vat_rate", issuer.default_vat_rate)), places=2)
            except ValueError:
                abort(422, f"Ligne {n} : « vat_rate » invalide.")
            if not 0 <= rate <= 100:
                abort(422, f"Ligne {n} : « vat_rate » doit être entre 0 et 100.")
        else:
            rate = billing.default_vat_rate(issuer)
        out.append(
            billing.LineInput(
                label=_text(line, "label", 200, required=True),
                description=_text(line, "description", 2000),
                quantity=quantity,
                unit=_text(line, "unit", 30),
                unit_price_cents=price,
                vat_rate=rate,
            )
        )
    return out


@bp.route("/factures", methods=["POST"])
def create_invoice():
    """Crée une facture (brouillon, ou émise si demandé et permis par le jeton) pour un client désigné par sa
    référence dans l'appli appelante, créé ou mis à jour au passage."""
    token = g.api_token
    if (request.content_length or 0) > MAX_BODY:
        abort(413)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Corps JSON attendu.")
    key = _text(data, "idempotency_key", 80, required=True)
    existing = db.session.scalar(db.select(Invoice).filter_by(idempotency_key=f"{token.id}:{key}"))
    if existing is not None:
        return jsonify(_invoice_json(existing)), 200
    issuer = db.session.scalar(db.select(Issuer).filter_by(code=_text(data, "issuer", 30, required=True)))
    if issuer is None or issuer.archived:
        abort(422, "Émetteur inconnu ou archivé (voir GET /api/v1/emetteurs).")
    wants_issue = data.get("issue") is True
    if wants_issue and not token.can_issue:
        abort(403, "Ce jeton ne peut créer que des brouillons.")
    client = data.get("customer")
    if not isinstance(client, dict):
        abort(422, "« customer » doit être un objet.")
    source = _text(client, "source", 30) or token.name.lower()[:30] or SOURCE
    try:
        customer, created = customers.upsert_external(source, client.get("external_id") or "", client)
    except ValueError as exc:
        abort(422, str(exc))
    lines = _lines(data.get("lines"), issuer)
    invoice = billing.new_draft(issuer, customer)
    invoice.subject = _text(data, "subject", 255)
    invoice.customer_reference = _text(data, "customer_reference", 80)
    invoice.notes = _text(data, "notes", 2000)
    invoice.service_date = _date(data, "service_date") or date.today()
    invoice.service_end = _date(data, "service_end")
    invoice.due_date = _date(data, "due_date")
    invoice.source = source
    invoice.external_id = _text(data, "external_id", 80)
    invoice.idempotency_key = f"{token.id}:{key}"
    billing.set_lines(invoice, lines)
    db.session.flush()
    if created:
        log("customer.create", f"Client créé par l'API : {customer.name}", customer=customer, api_token=token)
    log(
        "invoice.create",
        f"Brouillon créé par l'API ({token.name}) pour {customer.name} : {money.amount(invoice.total_ttc_cents)}",
        invoice=invoice,
        customer=customer,
        api_token=token,
    )
    if wants_issue:
        try:
            billing.issue(invoice)
        except billing.BillingError as exc:
            db.session.rollback()
            abort(422, f"Émission impossible : {exc}")
        log(
            "invoice.issue",
            f"Facture {invoice.number} émise par l'API ({token.name}) : {money.amount(invoice.total_ttc_cents)}",
            invoice=invoice,
            customer=customer,
            api_token=token,
        )
    db.session.commit()
    return jsonify(_invoice_json(invoice)), 201
