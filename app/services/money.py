"""Montants, quantités et taux : lecture de ce qu'on saisit, et écriture à la française.

Les montants sont des centimes entiers ; les quantités et les taux, des Decimal. Les arrondis se font au demi supérieur
(0,005 € → 0,01 €), comme le veut l'usage comptable.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NBSP = " "  # espace fine insécable : séparateur des milliers et avant « € » ou « % »
MAX_CENTS = 10**11  # un milliard d'euros : au-delà, c'est une faute de frappe


def _clean(text):
    text = str(text or "").strip().replace(" ", "").replace(NBSP, "").replace(" ", "")
    return text.removesuffix("€").removesuffix("%").strip().replace(",", ".")


def parse_decimal(text, places=None):
    """« 1 234,5 » → Decimal("1234.5"). ValueError si ce n'est pas un nombre."""
    cleaned = _clean(text)
    if not re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
        raise ValueError(f"« {text} » n'est pas un nombre.")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:  # pragma: no cover - déjà filtré par l'expression régulière
        raise ValueError(f"« {text} » n'est pas un nombre.") from exc
    if places is not None and value != value.quantize(Decimal(1).scaleb(-places)):
        raise ValueError(f"« {text} » : {places} chiffre(s) après la virgule au plus.")
    return value


def parse_amount(text):
    """« 12,50 € » → 1250 (centimes). ValueError si ce n'est pas un montant."""
    value = parse_decimal(text)
    if value != value.quantize(Decimal("0.01")):
        raise ValueError(f"« {text} » : deux chiffres après la virgule au plus.")
    cents = int(value * 100)
    if abs(cents) >= MAX_CENTS:
        raise ValueError(f"« {text} » : montant trop élevé.")
    return cents


def round_cents(value):
    """Decimal (en centimes, éventuellement fractionnaire) → centimes entiers, arrondi au demi supérieur."""
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _group(digits):
    out = ""
    while len(digits) > 3:
        out = NBSP + digits[-3:] + out
        digits = digits[:-3]
    return digits + out


def amount(cents, currency=True):
    """1234567 → « 12 345,67 € » ; -250 → « −2,50 € »."""
    if cents is None:
        return ""
    cents = int(cents)
    sign = "−" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    text = f"{sign}{_group(str(euros))},{rest:02d}"
    return f"{text}{NBSP}€" if currency else text


def amount_input(cents):
    """Valeur d'un champ de saisie : 1250 → « 12,50 » (sans séparateur de milliers, relisible par parse_amount)."""
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(int(cents)), 100)
    return f"{sign}{euros},{rest:02d}"


def number(value):
    """Decimal("1.500") → « 1,5 » ; Decimal("2") → « 2 »."""
    if value is None:
        return ""
    text = format(Decimal(value).normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    integer, _, decimals = text.partition(".")
    sign = "−" if integer.startswith("-") else ""
    integer = integer.lstrip("-")
    return sign + _group(integer) + ("," + decimals if decimals else "")


def rate(value):
    """Decimal("5.50") → « 5,5 % »."""
    return f"{number(value)}{NBSP}%"
