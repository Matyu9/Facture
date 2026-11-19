"""Format des numéros de pièce, réglable par émetteur et par série (factures, avoirs, devis).

Un format est un texte fixe avec des balises entre accolades :

- ``{AAAA}`` l'année (2026), ``{AA}`` sur deux chiffres (26), ``{MM}`` le mois (11), ``{JJ}`` le jour (19) ;
- le compteur, une seule fois : ``{N}`` en chiffres (7), ``{NNNN}`` complété de zéros (0007), ou ``{L}`` en lettres
  (A, B… Z, AA, AB…).

Le compteur repart à 1 quand la période imprimée change : chaque mois si le format porte l'année et le mois, chaque
année s'il porte l'année, jamais sinon. Deux pièces d'une même série ne peuvent donc pas recevoir le même numéro.
"""

import re
from dataclasses import dataclass
from datetime import date

FORMAT_MAX = 40
NUMBER_MAX = 40  # Invoice.number
TOKEN = re.compile(r"\{([^{}]*)\}")
LITERAL = re.compile(r"^[A-Za-z0-9._/-]*$")
DATE_TOKENS = {
    "AAAA": lambda d: f"{d.year:04d}",
    "AA": lambda d: f"{d.year % 100:02d}",
    "MM": lambda d: f"{d.month:02d}",
    "JJ": lambda d: f"{d.day:02d}",
}
YEARLY, MONTHLY, NEVER = "annee", "mois", "jamais"
RESETS = {YEARLY: "chaque année", MONTHLY: "chaque mois", NEVER: "jamais"}
HELP = {
    "{AAAA}": "année (2026)",
    "{AA}": "année sur 2 chiffres (26)",
    "{MM}": "mois (11)",
    "{JJ}": "jour (19)",
    "{N}": "compteur (1, 2… 10)",
    "{NNNN}": "compteur sur 4 chiffres (0001) — autant de N que de chiffres, jusqu'à 8",
    "{L}": "compteur en lettres (A, B… Z, AA…)",
}


class FormatError(ValueError):
    pass


@dataclass(frozen=True)
class Format:
    text: str
    parts: tuple  # ("texte", "…") | ("date", "AAAA") | ("chiffres", largeur) | ("lettres", None)

    @property
    def reset(self):
        tokens = {value for kind, value in self.parts if kind == "date"}
        has_year = bool(tokens & {"AAAA", "AA"})
        if has_year and "MM" in tokens:
            return MONTHLY
        return YEARLY if has_year else NEVER

    def period(self, day):
        """Clé de la série dans NumberSequence : le compteur repart à 1 quand elle change."""
        return {MONTHLY: f"{day.year:04d}-{day.month:02d}", YEARLY: f"{day.year:04d}", NEVER: ""}[self.reset]

    def render(self, day, n):
        out = []
        for kind, value in self.parts:
            if kind == "texte":
                out.append(value)
            elif kind == "date":
                out.append(DATE_TOKENS[value](day))
            elif kind == "chiffres":
                out.append(f"{n:0{value}d}")
            else:
                out.append(letters(n))
        return "".join(out)

    @property
    def signature(self):
        """Forme du numéro, sans les largeurs : deux séries de même signature produiraient les mêmes numéros."""
        return "".join(
            value
            if kind == "texte"
            else "{" + value + "}"
            if kind == "date"
            else "{#}"
            if kind == "chiffres"
            else "{@}"
            for kind, value in self.parts
        )


def letters(n):
    """1 → A, 26 → Z, 27 → AA, 28 → AB… (comme les colonnes d'un tableur)."""
    out = ""
    while n > 0:
        n, rest = divmod(n - 1, 26)
        out = chr(65 + rest) + out
    return out


def parse(text):
    text = (text or "").strip()
    if not text:
        raise FormatError("Indiquez un format.")
    if len(text) > FORMAT_MAX:
        raise FormatError(f"{FORMAT_MAX} caractères au plus.")
    parts, counters, position = [], 0, 0
    for match in TOKEN.finditer(text):
        if match.start() > position:
            parts.append(("texte", text[position : match.start()]))
        raw = match.group(1).strip()
        token = raw.upper()
        if token in DATE_TOKENS:
            parts.append(("date", token))
        elif re.fullmatch(r"N{1,8}", token):
            parts.append(("chiffres", len(token)))
            counters += 1
        elif token == "L":
            parts.append(("lettres", None))
            counters += 1
        else:
            raise FormatError(f"Balise inconnue : {{{raw}}}. Balises possibles : " + ", ".join(HELP) + ".")
        position = match.end()
    if position < len(text):
        parts.append(("texte", text[position:]))
    for kind, value in parts:
        if kind == "texte":
            if "{" in value or "}" in value:
                raise FormatError("Accolade sans balise : écrivez par exemple {AAAA} ou {NNNN}.")
            if not LITERAL.match(value):
                raise FormatError("En dehors des balises : lettres sans accent, chiffres, tiret, point, barre / et _.")
    if counters != 1:
        raise FormatError("Il faut un compteur, et un seul : {N}, {NNNN} ou {L}.")
    return Format(text, tuple(parts))


def default(kind):
    from ..models import Invoice

    return {Invoice.CREDIT: "AV-{AAAA}-{NNNN}", Invoice.QUOTE: "DE-{AAAA}-{NNNN}"}.get(kind, "FA-{AAAA}-{NNNN}")


def of(issuer, kind):
    from ..models import Invoice

    text = {Invoice.CREDIT: issuer.credit_format, Invoice.QUOTE: issuer.quote_format}.get(kind, issuer.invoice_format)
    try:
        return parse(text or default(kind))
    except FormatError:
        return parse(default(kind))


def example(text, day=None, n=1):
    """Exemple affiché à côté du champ ; FormatError si le format est refusé."""
    fmt = parse(text)
    value = fmt.render(day or date.today(), n)
    if len(fmt.render(day or date.today(), 99_999)) > NUMBER_MAX:
        raise FormatError(f"Numéros trop longs : {NUMBER_MAX} caractères au plus.")
    return value
