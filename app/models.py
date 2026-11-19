"""Modèle de données.

Les montants sont en centimes (entiers) : aucune erreur d'arrondi ne s'accumule. Les taux de TVA et les quantités
sont des Decimal (colonnes Numeric).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from flask_login import UserMixin
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


class TimestampMixin:
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, server_default=func.now())
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.now, server_default=func.now(), onupdate=datetime.now
    )


class ArchivableMixin:
    """Fiche qu'on n'efface pas une fois utilisée : on l'archive, elle disparaît des listes de choix."""

    archived_at = db.Column(db.DateTime)

    @property
    def archived(self):
        return self.archived_at is not None


# ---------------------------------------------------------------------------
# Comptes
# ---------------------------------------------------------------------------


class Role(db.Model):
    """Rôle attribuable aux comptes, avec ses droits (cochés dans l'écran « Rôles »).

    Les trois rôles de base existent toujours (non supprimables) ; l'administrateur a tous les droits
    quelle que soit sa liste, pour qu'on ne puisse pas se retirer l'accès à l'appli.
    """

    ADMIN = "admin"
    GESTIONNAIRE = "gestionnaire"
    LECTEUR = "lecteur"
    BUILTIN = (ADMIN, GESTIONNAIRE, LECTEUR)
    DEFAULT_NAMES = {ADMIN: "Administrateur", GESTIONNAIRE: "Gestionnaire", LECTEUR: "Lecteur"}

    key = db.Column(db.String(20), primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    description = db.Column(db.String(255))
    permissions = db.Column(db.JSON, nullable=False, default=list)
    # Groupes du fournisseur OIDC (séparés par des virgules) qui donnent ce rôle à la connexion.
    oidc_groups = db.Column(db.String(255))

    @property
    def builtin(self):
        return self.key in self.BUILTIN

    @property
    def oidc_group_list(self):
        return [g.strip() for g in (self.oidc_groups or "").split(",") if g.strip()]

    def __repr__(self):
        return f"<Role {self.key}>"


class Account(UserMixin, TimestampMixin, db.Model):
    """Compte de connexion : par mot de passe, ou par le fournisseur OIDC (Adhérents)."""

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True)
    email_verified = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(20), nullable=False, default=Role.LECTEUR)
    # Un compte inactif ne peut pas se connecter.
    active = db.Column(db.Boolean, nullable=False, default=False)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    last_login_at = db.Column(db.DateTime)
    # Identité chez le fournisseur OIDC (claim « sub »), une fois le compte rattaché.
    oidc_sub = db.Column(db.String(255), unique=True)
    # Rôle donné par les groupes OIDC (et non choisi à la main) : il suit les groupes à chaque connexion.
    role_from_oidc = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    # Double authentification : secret chiffré (cf. services/totp.py), actif seulement une fois `totp_enabled_at` posé.
    totp_secret = db.Column(db.String(255))
    totp_enabled_at = db.Column(db.DateTime)
    totp_last_counter = db.Column(db.Integer)  # dernier code accepté : un code ne sert qu'une fois
    theme = db.Column(db.String(10))  # "light", "dark" ou None (celui du système)
    text_size = db.Column(db.String(10))  # "large", "xlarge" ou None (normale)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def initials(self):
        return f"{self.first_name[:1]}{self.last_name[:1]}".upper()

    @property
    def is_active(self):  # utilisé par Flask-Login
        return self.active

    @property
    def is_admin(self):
        return self.role == Role.ADMIN

    @property
    def totp_enabled(self):
        return self.totp_enabled_at is not None

    @property
    def role_label(self):
        role = db.session.get(Role, self.role)
        return role.name if role else Role.DEFAULT_NAMES.get(self.role, self.role)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    # Jeton de la session en cours (voir LoginSession), fixé à la connexion ou au chargement.
    session_token = None

    def get_id(self):  # utilisé par Flask-Login : stocké dans le cookie de session et « rester connecté »
        return f"{self.id}:{self.session_token}"

    def __repr__(self):
        return f"<Account {self.full_name} ({self.role})>"


class LoginSession(db.Model):
    """Connexion ouverte sur un appareil. La supprimer invalide ses cookies, même copiés ailleurs."""

    id = db.Column(db.String(64), primary_key=True)  # jeton secret
    # Identifiant public de la session, qui peut circuler (écrans, formulaires) sans rien donner : jamais `id`.
    sid = db.Column(db.String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    account_id = db.Column(db.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    last_seen_at = db.Column(db.DateTime)
    user_agent = db.Column(db.String(255))
    ip = db.Column(db.String(45))
    # Session ouverte via le fournisseur OIDC : son `sid` (pour l'avis de déconnexion qui la désigne) et le jeton
    # d'identité reçu, renvoyé au fournisseur pour qu'il déconnecte aussi la personne de chez lui.
    oidc_sid = db.Column(db.String(255), index=True)
    oidc_id_token = db.Column(db.Text)

    account = db.relationship("Account")


class AccountToken(db.Model):
    """Lien à usage unique envoyé par email : invitation, mot de passe oublié, changement d'email.

    Seule l'empreinte du jeton est stockée : une fuite de la base ne donne aucun lien valable.
    """

    INVITE = "invite"
    RESET = "reset"
    EMAIL = "email"

    id = db.Column(db.String(64), primary_key=True)  # empreinte du jeton
    account_id = db.Column(db.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True)
    purpose = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(255))  # adresse à confirmer
    # Vrai si le lien n'a circulé que par email : un lien transmis à la main ne prouve pas que l'adresse est la bonne.
    proves_email = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    expires_at = db.Column(db.DateTime, nullable=False)

    account = db.relationship("Account")


class RecoveryCode(db.Model):
    """Code de secours à usage unique, pour un téléphone perdu. Seule son empreinte est stockée."""

    id = db.Column(db.String(64), primary_key=True)  # empreinte du code
    account_id = db.Column(db.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True)


@login_manager.user_loader
def load_user(user_id):
    account_id, _, token = user_id.partition(":")
    login = db.session.get(LoginSession, token) if token else None
    if login is None or str(login.account_id) != account_id:
        return None
    login.account.session_token = token
    from .services.sessions import touch

    if not touch(login):  # fermée pour inactivité
        return None
    return login.account


class ThrottleEvent(db.Model):
    """Tentative comptée par services/throttle.py. En base, pour que la limite vaille pour tous les workers.

    `key_hash` est une empreinte : le texte d'origine (email saisi, IP) n'est jamais conservé.
    """

    id = db.Column(db.Integer, primary_key=True)
    key_hash = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)


class ApiToken(db.Model):
    """Jeton d'une appli qui crée ou lit des factures par l'API (Adhérents, à terme). Seule son empreinte est gardée."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    prefix = db.Column(db.String(12), nullable=False)  # début du jeton, pour le reconnaître dans la liste
    # Sans ce droit, l'API ne crée que des brouillons, émis ensuite par une personne.
    can_issue = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    created_by_id = db.Column(db.ForeignKey("account.id", ondelete="SET NULL"))
    last_used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)

    created_by = db.relationship("Account")

    @property
    def active(self):
        return self.revoked_at is None


# ---------------------------------------------------------------------------
# Facturation
# ---------------------------------------------------------------------------


class Issuer(ArchivableMixin, TimestampMixin, db.Model):
    """Émetteur des factures : l'association, ou l'une de ses régies, avec ses mentions, sa TVA et sa numérotation."""

    # Régime de TVA : il décide des colonnes de la facture et de la mention obligatoire.
    FRANCHISE = "franchise"  # franchise en base (art. 293 B du CGI)
    EXEMPT = "exonere"  # opérations exonérées (association : art. 261-7-1° du CGI)
    SUBJECT = "assujetti"  # TVA facturée
    VAT_REGIMES = {
        FRANCHISE: "Franchise en base de TVA",
        EXEMPT: "Exonéré de TVA",
        SUBJECT: "Assujetti à la TVA",
    }
    DEFAULT_MENTIONS = {
        FRANCHISE: "TVA non applicable, art. 293 B du CGI.",
        EXEMPT: "Exonération de TVA, article 261-7-1° du CGI.",
        SUBJECT: "",
    }
    # Mise en page du PDF, réglable par émetteur (Émetteurs → Mise en page). Les mentions légales, elles, sont toujours
    # imprimées par l'appli : la mise en page change l'apparence, jamais le contenu obligatoire.
    LAYOUTS = {
        "classique": "Classique",
        "bandeau": "Bandeau de couleur",
        "sobre": "Sobre (noir et blanc)",
    }
    LOGO_SIZES = {"aucun": "Pas de logo", "petit": "Petit", "moyen": "Moyen", "grand": "Grand"}
    LOGO_POSITIONS = {"gauche": "À gauche", "droite": "À droite"}
    DEFAULT_RECIPIENT_LABEL = "Facturé à"
    DEFAULT_DESIGNATION_LABEL = "Désignation"

    id = db.Column(db.Integer, primary_key=True)
    # Identifiant court et stable, utilisé par l'API (« ecole », « orchestre ») : ne change pas après création.
    code = db.Column(db.String(30), nullable=False, unique=True)
    name = db.Column(db.String(120), nullable=False)  # nom affiché dans l'appli
    legal_name = db.Column(db.String(200), nullable=False)  # dénomination imprimée sur les factures
    address = db.Column(db.String(255))
    postcode = db.Column(db.String(20))
    city = db.Column(db.String(120))
    country = db.Column(db.String(80))
    email = db.Column(db.String(255))
    phone = db.Column(db.String(40))
    website = db.Column(db.String(255))
    siret = db.Column(db.String(20))
    rna = db.Column(db.String(20))  # numéro RNA de l'association (W…)
    vat_number = db.Column(db.String(30))
    legal_form = db.Column(db.String(120))  # « Association loi 1901 »
    vat_regime = db.Column(db.String(20), nullable=False, default=FRANCHISE)
    default_vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=Decimal("20.00"))
    # Mention imprimée sous les totaux (vide : celle du régime).
    vat_mention = db.Column(db.String(255))
    # Numérotation (services/numbering.py) : factures et avoirs par série (NumberSeries), devis ici.
    quote_format = db.Column(
        db.String(40), nullable=False, default="DE-{AAAA}-{NNNN}", server_default="DE-{AAAA}-{NNNN}"
    )
    quote_validity_days = db.Column(db.Integer, nullable=False, default=30, server_default="30")
    payment_terms_days = db.Column(db.Integer, nullable=False, default=30)
    iban = db.Column(db.String(40))
    bic = db.Column(db.String(15))
    payment_instructions = db.Column(db.Text)  # « Règlement par virement ou par chèque à l'ordre de… »
    # Pénalités de retard : obligatoires sur les factures entre professionnels.
    late_penalty = db.Column(db.Text)
    footer = db.Column(db.Text)
    logo = db.Column(db.String(255))  # chemin relatif dans UPLOAD_DIR
    pdf_layout = db.Column(db.String(20), nullable=False, default="classique", server_default="classique")
    pdf_color = db.Column(db.String(7))  # #rrggbb ; vide : la couleur de l'appli (Paramètres → Apparence)
    logo_size = db.Column(db.String(10), nullable=False, default="moyen", server_default="moyen")
    logo_position = db.Column(db.String(10), nullable=False, default="gauche", server_default="gauche")
    intro_text = db.Column(db.Text)  # imprimé au-dessus des lignes
    closing_text = db.Column(db.Text)  # imprimé en fin de pièce (« Merci de votre confiance »)
    recipient_label = db.Column(db.String(40))  # vide : « Facturé à »
    designation_label = db.Column(db.String(40))  # vide : « Désignation »
    email_subject = db.Column(db.String(200))
    email_body = db.Column(db.Text)

    series = db.relationship(
        "NumberSeries",
        back_populates="issuer",
        order_by="(NumberSeries.is_default.desc(), NumberSeries.name)",
        lazy="selectin",
    )

    @property
    def default_series(self):
        return next((s for s in self.series if s.is_default), None)

    @property
    def active_series(self):
        return [s for s in self.series if not s.archived]

    @property
    def subject_to_vat(self):
        return self.vat_regime == self.SUBJECT

    @property
    def mention(self):
        return (self.vat_mention or "").strip() or self.DEFAULT_MENTIONS.get(self.vat_regime, "")

    @property
    def regime_label(self):
        return self.VAT_REGIMES.get(self.vat_regime, self.vat_regime)

    def __repr__(self):
        return f"<Issuer {self.code}>"


class NumberSeries(ArchivableMixin, TimestampMixin, db.Model):
    """Série de numérotation des factures et avoirs d'un émetteur (« Générale », « CE »…), choisie sur chaque facture.
    Chaque série a ses numéros, continus et sans trou ; une seule est la série par défaut."""

    __table_args__ = (
        db.UniqueConstraint("issuer_id", "code", name="uq_number_series_issuer_code"),
        db.Index("uq_number_series_default", "issuer_id", unique=True, postgresql_where=db.text("is_default")),
    )

    id = db.Column(db.Integer, primary_key=True)
    issuer_id = db.Column(db.ForeignKey("issuer.id", ondelete="RESTRICT"), nullable=False, index=True)
    code = db.Column(db.String(30), nullable=False)  # pour l'API : "series": "ce"
    name = db.Column(db.String(80), nullable=False)
    invoice_format = db.Column(db.String(40), nullable=False)
    credit_format = db.Column(db.String(40), nullable=False)
    is_default = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    issuer = db.relationship("Issuer", back_populates="series")

    def __repr__(self):
        return f"<NumberSeries {self.code}>"


class NumberSequence(db.Model):
    """Dernier numéro attribué, par émetteur, type de pièce et période. Lu et incrémenté sous verrou : pas de trou."""

    issuer_id = db.Column(db.ForeignKey("issuer.id", ondelete="RESTRICT"), primary_key=True)
    kind = db.Column(db.String(10), primary_key=True)  # Invoice.INVOICE, Invoice.CREDIT ou Invoice.QUOTE
    # NumberSeries.id pour les factures et avoirs ; 0 pour les devis (une seule série par émetteur).
    series_id = db.Column(db.Integer, primary_key=True, default=0, server_default="0", autoincrement=False)
    # « 2026 », « 2026-11 » ou « » (jamais remis à zéro), selon le format de la série.
    period = db.Column(db.String(7), primary_key=True)
    last_number = db.Column(db.Integer, nullable=False, default=0)


# Coordonnées du destinataire imprimées sur une pièce (copie figée dans Invoice.customer_snapshot).
RECIPIENT_FIELDS = (
    "kind",
    "name",
    "contact_name",
    "email",
    "address",
    "address_extra",
    "postcode",
    "city",
    "country",
    "siret",
    "vat_number",
)


class Customer(ArchivableMixin, TimestampMixin, db.Model):
    """Client : une structure (mairie, comité des fêtes, entreprise) ou un particulier (famille d'adhérents)."""

    ORGANISATION = "structure"
    PERSON = "particulier"
    KINDS = {ORGANISATION: "Structure", PERSON: "Particulier"}

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False, default=ORGANISATION)
    name = db.Column(db.String(200), nullable=False)
    contact_name = db.Column(db.String(160))  # à l'attention de…
    email = db.Column(db.String(255))
    phone = db.Column(db.String(40))
    address = db.Column(db.String(255))
    address_extra = db.Column(db.String(255))
    postcode = db.Column(db.String(20))
    city = db.Column(db.String(120))
    country = db.Column(db.String(80))
    siret = db.Column(db.String(20))
    vat_number = db.Column(db.String(30))
    notes = db.Column(db.Text)  # interne, jamais imprimé
    # Client venu d'une autre appli (Adhérents) : sa référence là-bas, pour le retrouver sans doublon.
    source = db.Column(db.String(30))
    external_id = db.Column(db.String(80))

    __table_args__ = (db.UniqueConstraint("source", "external_id", name="uq_customer_source_external_id"),)

    invoices = db.relationship("Invoice", back_populates="customer", lazy="dynamic")

    @property
    def kind_label(self):
        return self.KINDS.get(self.kind, self.kind)

    def snapshot(self):
        """Coordonnées imprimées sur une pièce : copiées dans la facture à son émission."""
        return {field: getattr(self, field) for field in RECIPIENT_FIELDS}

    def __repr__(self):
        return f"<Customer {self.name}>"


class Product(ArchivableMixin, TimestampMixin, db.Model):
    """Article du catalogue : une prestation ou un produit qui revient souvent, pour remplir une ligne en un clic."""

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    unit = db.Column(db.String(30))  # « forfait », « heure »…
    unit_price_cents = db.Column(db.Integer, nullable=False, default=0)
    # Taux de TVA propre à l'article (émetteur assujetti) ; vide : le taux par défaut de l'émetteur.
    vat_rate = db.Column(db.Numeric(5, 2))
    # Article propre à un émetteur ; vide : proposé pour tous.
    issuer_id = db.Column(db.ForeignKey("issuer.id", ondelete="SET NULL"), index=True)

    issuer = db.relationship("Issuer")


class Invoice(TimestampMixin, db.Model):
    """Facture ou avoir. Brouillon modifiable, puis émis : numéroté, figé et archivé en PDF, il ne change plus.

    Une erreur sur une pièce émise se corrige par un avoir, jamais en la modifiant ou en la supprimant.
    """

    INVOICE = "facture"
    CREDIT = "avoir"
    QUOTE = "devis"
    KINDS = {INVOICE: "Facture", CREDIT: "Avoir", QUOTE: "Devis"}
    # Pièces comptables (journal des ventes, encaissements) : un devis n'en est pas une.
    BILLING_KINDS = (INVOICE, CREDIT)
    # Décision du client sur un devis émis.
    ACCEPTED = "accepte"
    REFUSED = "refuse"
    DECISIONS = {ACCEPTED: "Accepté", REFUSED: "Refusé"}
    DRAFT = "brouillon"
    ISSUED = "emise"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False, default=INVOICE)
    status = db.Column(db.String(12), nullable=False, default=DRAFT, index=True)
    issuer_id = db.Column(db.ForeignKey("issuer.id", ondelete="RESTRICT"), nullable=False, index=True)
    # Série de numérotation d'une facture ou d'un avoir (celle de sa facture) ; vide pour un devis.
    series_id = db.Column(db.ForeignKey("number_series.id", ondelete="RESTRICT"), index=True)
    # Vide : destinataire ponctuel, saisi sur la pièce sans fiche client (ses coordonnées sont dans customer_snapshot).
    customer_id = db.Column(db.ForeignKey("customer.id", ondelete="RESTRICT"), index=True)
    number = db.Column(db.String(40), unique=True)
    sequence_year = db.Column(db.Integer)
    sequence_number = db.Column(db.Integer)
    issue_date = db.Column(db.Date)
    due_date = db.Column(db.Date)
    # Date de la prestation ou de la vente (mention obligatoire), ou début d'une période…
    service_date = db.Column(db.Date)
    service_end = db.Column(db.Date)  # … et sa fin
    subject = db.Column(db.String(255))  # objet : « Concert du 14 juillet »
    customer_reference = db.Column(db.String(80))  # référence ou bon de commande du client
    notes = db.Column(db.Text)  # imprimé sur la facture
    internal_note = db.Column(db.Text)  # jamais imprimé
    # Copie de l'émetteur et du client au moment de l'émission : la facture reste telle quelle s'ils changent ensuite.
    # Pour un destinataire ponctuel, customer_snapshot porte ses coordonnées dès le brouillon.
    issuer_snapshot = db.Column(db.JSON)
    customer_snapshot = db.Column(db.JSON)
    total_ht_cents = db.Column(db.Integer, nullable=False, default=0)
    total_vat_cents = db.Column(db.Integer, nullable=False, default=0)
    total_ttc_cents = db.Column(db.Integer, nullable=False, default=0)
    vat_breakdown = db.Column(db.JSON)  # [[taux, base HT, TVA], …]
    # Avoir : la facture qu'il annule ou corrige.
    credited_invoice_id = db.Column(db.ForeignKey("invoice.id", ondelete="RESTRICT"), index=True)
    issued_at = db.Column(db.DateTime)
    issued_by_id = db.Column(db.ForeignKey("account.id", ondelete="SET NULL"))
    created_by_id = db.Column(db.ForeignKey("account.id", ondelete="SET NULL"))
    pdf_path = db.Column(db.String(255))  # relatif à ARCHIVE_DIR
    pdf_sha256 = db.Column(db.String(64))
    sent_at = db.Column(db.DateTime)
    sent_to = db.Column(db.String(255))
    # Devis : validité, décision du client, et facture qui en est issue (Invoice.quote_id).
    valid_until = db.Column(db.Date)
    decision = db.Column(db.String(10))
    decided_at = db.Column(db.DateTime)
    quote_id = db.Column(db.ForeignKey("invoice.id", ondelete="RESTRICT"), index=True)
    # Pièce créée par une autre appli (API) : sa référence, et la clé qui rend une nouvelle tentative sans effet.
    source = db.Column(db.String(30))
    external_id = db.Column(db.String(80))
    idempotency_key = db.Column(db.String(80), unique=True)

    issuer = db.relationship("Issuer")
    series = db.relationship("NumberSeries")
    customer = db.relationship("Customer", back_populates="invoices")
    lines = db.relationship(
        "InvoiceLine", back_populates="invoice", order_by="InvoiceLine.position", cascade="all, delete-orphan"
    )
    payments = db.relationship(
        "Payment", back_populates="invoice", order_by="Payment.paid_on", cascade="all, delete-orphan"
    )
    credited_invoice = db.relationship(
        "Invoice", remote_side=[id], back_populates="credit_notes", foreign_keys=[credited_invoice_id]
    )
    credit_notes = db.relationship(
        "Invoice", back_populates="credited_invoice", order_by="Invoice.id", foreign_keys=[credited_invoice_id]
    )
    quote = db.relationship("Invoice", remote_side=[id], back_populates="quote_invoices", foreign_keys=[quote_id])
    quote_invoices = db.relationship("Invoice", back_populates="quote", order_by="Invoice.id", foreign_keys=[quote_id])
    issued_by = db.relationship("Account", foreign_keys=[issued_by_id])
    created_by = db.relationship("Account", foreign_keys=[created_by_id])

    @property
    def is_draft(self):
        return self.status == self.DRAFT

    @property
    def is_credit(self):
        return self.kind == self.CREDIT

    @property
    def is_quote(self):
        return self.kind == self.QUOTE

    @property
    def kind_label(self):
        return self.KINDS[self.kind]

    @property
    def display_number(self):
        return self.number or f"Brouillon n° {self.id}"

    @property
    def recipient(self):
        """Coordonnées du destinataire telles qu'imprimées : la copie figée d'une pièce émise ou d'un destinataire
        ponctuel, sinon la fiche client du moment."""
        if self.customer is None or (not self.is_draft and self.customer_snapshot):
            return dict(self.customer_snapshot or {})
        return self.customer.snapshot()

    @property
    def recipient_name(self):
        return self.recipient.get("name") or "—"

    @property
    def is_one_off(self):
        """Destinataire ponctuel : pas de fiche client."""
        return self.customer is None

    @property
    def paid_cents(self):
        return sum(p.amount_cents for p in self.payments)

    @property
    def credited_cents(self):
        return sum(c.total_ttc_cents for c in self.credit_notes if c.status == self.ISSUED)

    @property
    def balance_cents(self):
        """Reste à payer d'une facture émise : total, moins les règlements et les avoirs émis."""
        if self.kind != self.INVOICE or self.is_draft:
            return 0
        return self.total_ttc_cents - self.paid_cents - self.credited_cents

    @property
    def overdue(self):
        return (
            not self.is_draft
            and self.kind == self.INVOICE
            and self.balance_cents > 0
            and self.due_date is not None
            and self.due_date < date.today()
        )

    def __repr__(self):
        return f"<Invoice {self.display_number}>"


class InvoiceLine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False, default=0)
    product_id = db.Column(db.ForeignKey("product.id", ondelete="SET NULL"))
    label = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=Decimal("1"))
    unit = db.Column(db.String(30))
    unit_price_cents = db.Column(db.Integer, nullable=False, default=0)  # hors taxe
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=Decimal("0"))
    total_ht_cents = db.Column(db.Integer, nullable=False, default=0)

    invoice = db.relationship("Invoice", back_populates="lines")
    product = db.relationship("Product")


class Payment(TimestampMixin, db.Model):
    """Règlement reçu pour une facture émise (en tout ou partie)."""

    METHODS = {
        "virement": "Virement",
        "cheque": "Chèque",
        "especes": "Espèces",
        "carte": "Carte bancaire",
        "prelevement": "Prélèvement",
        "en_ligne": "Paiement en ligne",
        "ancv": "Chèques vacances",
        "autre": "Autre",
    }

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False, index=True)
    paid_on = db.Column(db.Date, nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    method = db.Column(db.String(20), nullable=False)
    reference = db.Column(db.String(120))  # n° de chèque, libellé du virement…
    note = db.Column(db.String(255))
    created_by_id = db.Column(db.ForeignKey("account.id", ondelete="SET NULL"))

    invoice = db.relationship("Invoice", back_populates="payments")
    created_by = db.relationship("Account")

    @property
    def method_label(self):
        return self.METHODS.get(self.method, self.method)


# ---------------------------------------------------------------------------
# Journal, notifications, réglages
# ---------------------------------------------------------------------------


class Activity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.ForeignKey("account.id", ondelete="SET NULL"))
    action = db.Column(db.String(50), nullable=False)  # ex. role.update, auth.login
    message = db.Column(db.String(500), nullable=False)
    changes = db.Column(db.JSON)  # {champ: [ancien, nouveau]}
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, server_default=func.now(), index=True)
    # Compte concerné par l'action (rôle changé…), à distinguer de son auteur `account_id`.
    target_account_id = db.Column(db.ForeignKey("account.id", ondelete="SET NULL"), index=True)
    # Pièce ou client concernés, pour retrouver leur historique.
    invoice_id = db.Column(db.ForeignKey("invoice.id", ondelete="SET NULL"), index=True)
    customer_id = db.Column(db.ForeignKey("customer.id", ondelete="SET NULL"), index=True)
    # Action faite par une appli via l'API : le jeton utilisé.
    api_token_id = db.Column(db.ForeignKey("api_token.id", ondelete="SET NULL"))

    account = db.relationship("Account", foreign_keys=[account_id])
    target_account = db.relationship("Account", foreign_keys=[target_account_id])
    api_token = db.relationship("ApiToken")


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True)
    message = db.Column(db.String(500), nullable=False)
    link = db.Column(db.String(255))
    read = db.Column(db.Boolean, nullable=False, default=False)
    # Clé de déduplication pour les relances automatiques : relancer une tâche ne crée pas de doublon.
    dedup_key = db.Column(db.String(120), unique=True)
    emailed = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, server_default=func.now())
    # Supprimée par son destinataire : cachée, mais gardée pour que sa clé de déduplication empêche de la recréer.
    dismissed_at = db.Column(db.DateTime)

    account = db.relationship("Account")


class AppSetting(db.Model):
    """Réglage de l'appli modifiable depuis l'interface (nom, logo, couleur…), stocké en clé / valeur."""

    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, nullable=False)
