"""Envoi d'une pièce émise au client, avec son PDF archivé en pièce jointe."""

import smtplib
from datetime import datetime

from ..models import Invoice
from . import archive, mailer, money
from .pdf import long_date

DEFAULT_SUBJECT = "{type} {numero} – {emetteur}"
DEFAULT_BODY = (
    "Bonjour,\n\n"
    "Veuillez trouver ci-joint {type_article} {numero} du {date}, d'un montant de {montant}.\n"
    "{echeance_phrase}\n\n"
    "Bien cordialement,\n"
    "{emetteur}"
)
REMINDER_BODY = (
    "Bonjour,\n\n"
    "Sauf erreur de notre part, la facture {numero} du {date} reste à régler : {reste} restant dû "
    "(échéance du {echeance}).\n"
    "Vous la trouverez de nouveau ci-joint. Si votre règlement est déjà parti, merci de ne pas tenir compte de ce message.\n\n"
    "Bien cordialement,\n"
    "{emetteur}"
)
PLACEHOLDERS = {
    "{numero}": "numéro de la pièce",
    "{date}": "date d'émission",
    "{montant}": "montant total",
    "{reste}": "reste à payer",
    "{echeance}": "date d'échéance",
    "{client}": "nom du client",
    "{emetteur}": "nom de l'émetteur",
    "{objet}": "objet de la pièce",
}


class SendError(RuntimeError):
    pass


def values(invoice):
    issuer = invoice.issuer_snapshot or {}
    credit = invoice.kind == Invoice.CREDIT
    due = long_date(invoice.due_date) if invoice.due_date else ""
    return {
        "type": invoice.kind_label,
        "type_article": "l'avoir" if credit else "la facture",
        "numero": invoice.number or "",
        "date": long_date(invoice.issue_date),
        "montant": money.amount(invoice.total_ttc_cents),
        "reste": money.amount(invoice.balance_cents),
        "echeance": due,
        "echeance_phrase": f"Elle est à régler au plus tard le {due}." if due and not credit else "",
        "client": invoice.recipient_name,
        "emetteur": issuer.get("legal_name") or invoice.issuer.legal_name,
        "objet": invoice.subject or "",
    }


def fill(template, invoice):
    text = template
    for key, value in values(invoice).items():
        text = text.replace("{" + key + "}", value)
    return text


def default_message(invoice, reminder=False):
    """(objet, texte) proposés dans le formulaire d'envoi."""
    issuer = invoice.issuer
    subject = issuer.email_subject or DEFAULT_SUBJECT
    body = REMINDER_BODY if reminder else (issuer.email_body or DEFAULT_BODY)
    if reminder:
        subject = "Rappel : " + subject
    return fill(subject, invoice), "\n".join(line.rstrip() for line in fill(body, invoice).splitlines()).replace(
        "\n\n\n", "\n\n"
    )


def send(invoice, to, subject, body):
    """Envoie la pièce. SendError (message affichable) si le SMTP manque ou refuse. Ne fait pas de commit."""
    if invoice.is_draft:
        raise SendError("Un brouillon ne s'envoie pas : émettez-le d'abord.")
    if not mailer.smtp_enabled():
        raise SendError("L'envoi d'emails n'est pas configuré (variables SMTP_* du fichier .env).")
    try:
        content = archive.read(invoice)
    except archive.ArchiveError as exc:
        raise SendError(str(exc)) from exc
    issuer = invoice.issuer_snapshot or {}
    try:
        mailer.send(
            to,
            " ".join(subject.split())[:200],
            body,
            attachments=[(archive.filename(invoice), "application/pdf", content)],
            sender_name=issuer.get("legal_name"),
            reply_to=issuer.get("email"),
        )
    except (OSError, smtplib.SMTPException) as exc:
        raise SendError(f"Échec de l'envoi : {exc}") from exc
    invoice.sent_at = datetime.now()
    invoice.sent_to = to[:255]
