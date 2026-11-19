"""API (/api/v1) : une autre appli de l'association (Adhérents, à terme) crée, émet et envoie des factures et des
devis.

Authentification par jeton d'appli (Paramètres → API), dans l'en-tête `Authorization: Bearer fct_…`. Le cookie de
session n'est pas accepté. Un jeton sans le droit d'émettre ne crée et ne lit que des brouillons, qu'une personne
émet ensuite ; avec ce droit, il émet, envoie, et note la réponse du client à un devis. Une création porte une clé
d'idempotence (`idempotency_key`) : rejouée, elle renvoie la même pièce sans en créer une autre. Les actions
(émettre, facturer un devis) sont sans effet si elles ont déjà eu lieu. Documentation : docs/api.md.
"""

import re
from datetime import date

from flask import Blueprint, Response, abort, g, jsonify, request, url_for
from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import TooManyRequests, Unauthorized

from ..extensions import csrf, db
from ..models import Customer, Invoice, Issuer, NumberSeries
from ..services import api_tokens, archive, billing, customers, invoice_mail, money
from ..services.activity import log
from ..services.throttle import Limiter

bp = Blueprint("api", __name__, url_prefix="/api/v1")
csrf.exempt(bp)

SOURCE = "api"
# Collection de l'adresse (/factures, /devis) → types de pièces qu'elle sert.
COLLECTIONS = {"factures": Invoice.BILLING_KINDS, "devis": (Invoice.QUOTE,)}
# Domaine en morceaux séparés par des points : sans ambiguïté, donc sans retour arrière coûteux.
EMAIL = r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+"
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
        "series": [
            {"code": s.code, "name": s.name, "default": s.is_default, "archived": s.archived} for s in issuer.series
        ],
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


def _collection(invoice):
    return "devis" if invoice.is_quote else "factures"


def _invoice_json(invoice):
    issued = not invoice.is_draft
    collection = _collection(invoice)
    return {
        "id": invoice.id,
        "kind": invoice.kind,
        "status": invoice.status,
        "state": billing.state(invoice),
        "number": invoice.number,
        "issuer": invoice.issuer.code,
        "series": invoice.series.code if invoice.series else None,
        "customer": _customer_json(invoice),
        "valid_until": invoice.valid_until.isoformat() if invoice.valid_until else None,
        "decision": invoice.decision,
        "quote": {"id": invoice.quote.id, "number": invoice.quote.number} if invoice.quote else None,
        "invoices": [{"id": i.id, "number": i.number} for i in invoice.quote_invoices],
        "sent_at": invoice.sent_at.isoformat(timespec="seconds") if invoice.sent_at else None,
        "subject": invoice.subject,
        "external_id": invoice.external_id,
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "service_date": invoice.service_date.isoformat() if invoice.service_date else None,
        "service_end": invoice.service_end.isoformat() if invoice.service_end else None,
        "total_ht": invoice.total_ht_cents,
        "total_vat": invoice.total_vat_cents,
        "total_ttc": invoice.total_ttc_cents,
        "paid": invoice.paid_cents if issued and invoice.kind == Invoice.INVOICE else 0,
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
        "pdf_url": url_for("api.invoice_pdf", collection=collection, invoice_id=invoice.id) if issued else None,
        "web_url": url_for("invoices.detail", invoice_id=invoice.id),
    }


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------


@bp.route("/emetteurs")
def issuers():
    return jsonify({"issuers": [_issuer_json(i) for i in billing.issuers(include_archived=True)]})


@bp.route("/factures", defaults={"collection": "factures"})
@bp.route("/devis", defaults={"collection": "devis"})
def invoices(collection):
    kinds = COLLECTIONS[collection]
    if request.args.get("type") in kinds:
        kinds = (request.args["type"],)
    query = db.select(Invoice).outerjoin(Customer).where(Invoice.kind.in_(kinds))
    if source := request.args.get("source"):
        query = query.where(Customer.source == source)
    if external := request.args.get("client"):
        query = query.where(Customer.external_id == external)
    if reference := request.args.get("reference"):
        query = query.where(Invoice.external_id == reference)
    if code := request.args.get("emetteur"):
        query = query.join(Issuer).where(Issuer.code == code)
    if code := request.args.get("serie"):
        query = query.join(NumberSeries, Invoice.series_id == NumberSeries.id).where(NumberSeries.code == code)
    try:
        page = max(int(request.args.get("page", 1)), 1)
        per_page = min(max(int(request.args.get("par_page", PER_PAGE)), 1), MAX_PER_PAGE)
    except ValueError:
        abort(400, "« page » et « par_page » doivent être des nombres entiers.")
    total = db.session.scalar(db.select(db.func.count()).select_from(query.subquery()))
    items = db.session.scalars(query.order_by(Invoice.id.desc()).limit(per_page).offset((page - 1) * per_page)).all()
    return jsonify({"total": total, "page": page, "per_page": per_page, "invoices": [_invoice_json(i) for i in items]})


def _get(collection, invoice_id):
    """Pièce de la collection de l'adresse : un devis n'est pas servi sous /factures, ni l'inverse."""
    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None or invoice.kind not in COLLECTIONS[collection]:
        abort(404)
    return invoice


def _require_issue_right():
    if not g.api_token.can_issue:
        abort(403, "Ce jeton ne peut que créer des brouillons et lire : il n'a pas le droit d'émettre.")


@bp.route("/factures/<int:invoice_id>", defaults={"collection": "factures"})
@bp.route("/devis/<int:invoice_id>", defaults={"collection": "devis"})
def invoice(collection, invoice_id):
    return jsonify(_invoice_json(_get(collection, invoice_id)))


@bp.route("/factures/<int:invoice_id>/pdf", defaults={"collection": "factures"})
@bp.route("/devis/<int:invoice_id>/pdf", defaults={"collection": "devis"})
def invoice_pdf(collection, invoice_id):
    invoice = _get(collection, invoice_id)
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


def _customer(token, data):
    """Destinataire de la pièce, d'après « customer » : (client, coordonnées ponctuelles, client créé ?).

    - {"id": 12} : un client déjà enregistré ;
    - {"external_id": "foyer-12", …} : le client de l'appli appelante, créé ou mis à jour ;
    - sans id ni external_id : un destinataire ponctuel, imprimé sur la pièce sans fiche client."""
    client = data.get("customer")
    if not isinstance(client, dict):
        abort(422, "« customer » doit être un objet.")
    if "id" in client:
        customer = db.session.get(Customer, client["id"]) if isinstance(client["id"], int) else None
        if customer is None or customer.archived:
            abort(422, "Client inconnu ou archivé (« customer.id »).")
        return customer, None, False
    if client.get("external_id") not in (None, ""):
        source = _text(client, "source", 30) or token.name.lower()[:30] or SOURCE
        try:
            customer, created = customers.upsert_external(source, client["external_id"], client)
        except ValueError as exc:
            abort(422, str(exc))
        return customer, None, created
    try:
        return None, billing.clean_recipient(client), False
    except billing.BillingError as exc:
        abort(422, str(exc))


def _series(issuer, data, kind=Invoice.INVOICE):
    """Série demandée par son code (None : la série par défaut). 422 si elle n'existe pas ou n'a pas de sens."""
    code = _text(data, "series", 30)
    if code is None:
        return None
    if kind != Invoice.INVOICE:
        abort(422, "« series » ne vaut que pour une facture.")
    series = billing.find_series(issuer, code)
    if series is None or series.archived:
        abort(422, f"Série inconnue ou archivée pour {issuer.code} (voir GET /api/v1/emetteurs).")
    return series


@bp.route("/factures", methods=["POST"], defaults={"collection": "factures"})
@bp.route("/devis", methods=["POST"], defaults={"collection": "devis"})
def create_invoice(collection):
    """Crée une facture ou un devis (brouillon, ou émis si demandé et permis par le jeton), pour un client enregistré,
    un client de l'appli appelante (créé ou mis à jour au passage) ou un destinataire ponctuel."""
    token = g.api_token
    kind = Invoice.QUOTE if collection == "devis" else Invoice.INVOICE
    if (request.content_length or 0) > MAX_BODY:
        abort(413)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Corps JSON attendu.")
    key = _text(data, "idempotency_key", 80, required=True)
    existing = db.session.scalar(db.select(Invoice).filter_by(idempotency_key=f"{token.id}:{key}"))
    if existing is not None:
        if existing.kind != kind:
            abort(409, "Cette clé d'idempotence a déjà servi pour une pièce d'un autre type.")
        return jsonify(_invoice_json(existing)), 200
    issuer = db.session.scalar(db.select(Issuer).filter_by(code=_text(data, "issuer", 30, required=True)))
    if issuer is None or issuer.archived:
        abort(422, "Émetteur inconnu ou archivé (voir GET /api/v1/emetteurs).")
    wants_issue = data.get("issue") is True
    if wants_issue:
        _require_issue_right()
    series = _series(issuer, data, kind)
    customer, recipient, created = _customer(token, data)
    lines = _lines(data.get("lines"), issuer)
    invoice = billing.new_draft(issuer, customer, kind=kind, recipient=recipient)
    if series is not None:
        invoice.series = series
    invoice.subject = _text(data, "subject", 255)
    invoice.customer_reference = _text(data, "customer_reference", 80)
    invoice.notes = _text(data, "notes", 2000)
    invoice.service_date = _date(data, "service_date") or (None if kind == Invoice.QUOTE else date.today())
    invoice.service_end = _date(data, "service_end")
    if kind == Invoice.QUOTE:
        invoice.valid_until = _date(data, "valid_until")
    else:
        invoice.due_date = _date(data, "due_date")
    invoice.source = (customer.source if customer is not None else None) or token.name.lower()[:30] or SOURCE
    invoice.external_id = _text(data, "external_id", 80)
    invoice.idempotency_key = f"{token.id}:{key}"
    billing.set_lines(invoice, lines)
    db.session.flush()
    if created:
        log("customer.create", f"Client créé par l'API : {customer.name}", customer=customer, api_token=token)
    log(
        "invoice.create",
        f"Brouillon de {invoice.kind_label.lower()} créé par l'API ({token.name}) pour {invoice.recipient_name} : "
        f"{money.amount(invoice.total_ttc_cents)}",
        invoice=invoice,
        customer=customer,
        api_token=token,
    )
    if wants_issue:
        _issue(invoice, _date(data, "issue_date"), rollback=True)
    db.session.commit()
    return jsonify(_invoice_json(invoice)), 201


def _issue(invoice, issue_date=None, rollback=False):
    token = g.api_token
    try:
        billing.issue(invoice, issue_date=issue_date)
    except billing.BillingError as exc:
        if rollback:
            db.session.rollback()
        abort(422, f"Émission impossible : {exc}")
    log(
        "invoice.issue",
        f"{invoice.kind_label} {invoice.number} émis(e) par l'API ({token.name}) : "
        f"{money.amount(invoice.total_ttc_cents)}",
        invoice=invoice,
        customer=invoice.customer,
        api_token=token,
    )


def _body():
    if (request.content_length or 0) > MAX_BODY:
        abort(413)
    data = request.get_json(silent=True) if request.content_length else {}
    if not isinstance(data, dict):
        abort(400, "Corps JSON attendu (ou vide).")
    return data


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@bp.route("/factures/<int:invoice_id>/emettre", methods=["POST"], defaults={"collection": "factures"})
@bp.route("/devis/<int:invoice_id>/emettre", methods=["POST"], defaults={"collection": "devis"})
def issue(collection, invoice_id):
    """Émet un brouillon (date du jour, ou « issue_date »). Déjà émise : renvoyée telle quelle."""
    _require_issue_right()
    data = _body()
    invoice = _get(collection, invoice_id)
    if invoice.is_draft:
        _issue(invoice, _date(data, "issue_date"))
        db.session.commit()
    return jsonify(_invoice_json(invoice))


@bp.route("/factures/<int:invoice_id>/envoyer", methods=["POST"], defaults={"collection": "factures"})
@bp.route("/devis/<int:invoice_id>/envoyer", methods=["POST"], defaults={"collection": "devis"})
def send(collection, invoice_id):
    """Envoie la pièce émise par email, PDF joint : au destinataire de la pièce, ou à « to ». Objet et texte : ceux de
    l'émetteur, sauf « subject » / « body »."""
    _require_issue_right()
    data = _body()
    invoice = _get(collection, invoice_id)
    to = _text(data, "to", 255) or invoice.recipient.get("email")
    if not to or not re.fullmatch(EMAIL, to):
        abort(422, "Adresse du destinataire absente ou invalide (« to », ou l'email du client).")
    subject, body = invoice_mail.default_message(invoice)
    subject = _text(data, "subject", 200) or subject
    body = _text(data, "body", 8000) or body
    try:
        invoice_mail.send(invoice, to, subject, body)
    except invoice_mail.SendError as exc:
        abort(409 if invoice.is_draft else 503, str(exc))
    log(
        "invoice.send",
        f"Envoi de {invoice.number} à {invoice.sent_to} par l'API ({g.api_token.name})",
        invoice=invoice,
        customer=invoice.customer,
        api_token=g.api_token,
    )
    db.session.commit()
    return jsonify(_invoice_json(invoice))


@bp.route("/devis/<int:invoice_id>/<any(accepter, refuser):action>", methods=["POST"])
def decide(invoice_id, action):
    """Note la réponse du client à un devis émis."""
    _require_issue_right()
    quote = _get("devis", invoice_id)
    decision = Invoice.ACCEPTED if action == "accepter" else Invoice.REFUSED
    try:
        billing.decide(quote, decision)
    except billing.BillingError as exc:
        abort(409, str(exc))
    log(
        "quote.decision",
        f"Devis {quote.number} {Invoice.DECISIONS[decision].lower()} (API, {g.api_token.name})",
        invoice=quote,
        customer=quote.customer,
        api_token=g.api_token,
    )
    db.session.commit()
    return jsonify(_invoice_json(quote))


@bp.route("/devis/<int:invoice_id>/facturer", methods=["POST"])
def quote_to_invoice(invoice_id):
    """Facture tirée d'un devis (accepté au passage), en brouillon ou émise (« issue »: true). Rejouée, renvoie la
    facture déjà tirée du devis."""
    _require_issue_right()
    data = _body()
    quote = _get("devis", invoice_id)
    done = [i for i in quote.quote_invoices if not i.is_credit]
    if done:
        return jsonify(_invoice_json(done[0])), 200
    series = _series(quote.issuer, data)
    try:
        invoice = billing.invoice_from_quote(quote, series=series)
    except billing.BillingError as exc:
        abort(409, str(exc))
    invoice.service_date = _date(data, "service_date") or invoice.service_date or date.today()
    invoice.service_end = _date(data, "service_end") or invoice.service_end
    invoice.source = quote.source
    db.session.flush()
    log(
        "invoice.create",
        f"Facture tirée du devis {quote.number} par l'API ({g.api_token.name})",
        invoice=invoice,
        customer=invoice.customer,
        api_token=g.api_token,
    )
    if data.get("issue") is True:
        _issue(invoice, _date(data, "issue_date"), rollback=True)
    db.session.commit()
    return jsonify(_invoice_json(invoice)), 201
