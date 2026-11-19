"""Clients : recherche, et fiche tenue à jour par une autre appli (Adhérents) via l'API."""

from sqlalchemy import or_

from ..extensions import db, fold_contains
from ..models import Customer

FIELDS = (
    "kind",
    "name",
    "contact_name",
    "email",
    "phone",
    "address",
    "address_extra",
    "postcode",
    "city",
    "country",
    "siret",
    "vat_number",
)
LIMITS = {
    "kind": 20,
    "name": 200,
    "contact_name": 160,
    "email": 255,
    "phone": 40,
    "address": 255,
    "address_extra": 255,
    "postcode": 20,
    "city": 120,
    "country": 80,
    "siret": 20,
    "vat_number": 30,
}


def search(q=None, include_archived=False):
    query = db.select(Customer)
    if not include_archived:
        query = query.where(Customer.archived_at.is_(None))
    if q:
        query = query.where(
            or_(*(fold_contains(c, q) for c in (Customer.name, Customer.contact_name, Customer.email, Customer.city)))
        )
    return query.order_by(db.func.fold(Customer.name), Customer.id)


def active():
    return db.session.scalars(search()).all()


def clean(values):
    """Valeurs d'une fiche client, nettoyées et bornées. ValueError si le nom ou le type manquent."""
    data = {}
    for field in FIELDS:
        if field not in values:
            continue
        value = values[field]
        value = " ".join(str(value).split()) if value is not None else ""
        data[field] = value[: LIMITS[field]] or None
    if "kind" in data and data["kind"] not in Customer.KINDS:
        raise ValueError(f"Type de client inconnu : {data['kind']} (attendu : {', '.join(Customer.KINDS)}).")
    if "name" in data and not data["name"]:
        raise ValueError("Le nom du client est obligatoire.")
    if data.get("email"):
        data["email"] = data["email"].lower()
    return data


def upsert_external(source, external_id, values):
    """Client de l'appli `source` désigné par sa référence là-bas : créé, ou mis à jour avec ce qu'elle envoie.

    Renvoie (client, créé ?). Sans commit."""
    external_id = str(external_id).strip()[:80]
    if not external_id:
        raise ValueError("Référence du client (external_id) manquante.")
    data = clean(values)
    customer = db.session.scalar(db.select(Customer).filter_by(source=source, external_id=external_id))
    created = customer is None
    if created:
        if not data.get("name"):
            raise ValueError("Le nom du client est obligatoire.")
        customer = Customer(source=source, external_id=external_id, kind=Customer.PERSON)
        db.session.add(customer)
    for field, value in data.items():
        setattr(customer, field, value)
    if customer.kind is None:
        customer.kind = Customer.PERSON
    customer.archived_at = None
    db.session.flush()
    return customer, created
