"""PDF d'une facture ou d'un avoir (A4), en Python pur : aucune bibliothèque système à installer.

Une pièce émise est rendue d'après ses copies figées de l'émetteur et du client ; un brouillon, d'après les fiches
du moment, avec la mention « BROUILLON » en travers.

La mise en page (disposition, couleur, logo, textes d'introduction et de conclusion, intitulés) se règle par émetteur
et fait partie de sa copie figée : une pièce émise garde l'apparence qu'elle avait. Les mentions obligatoires sont
imprimées quelle que soit la mise en page.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from flask import current_app
from fpdf import FPDF
from PIL import Image

from ..models import Customer, Invoice, InvoiceLine, Issuer
from . import billing, branding, money

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
FONT = "DejaVu"
MONTHS = [
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
]
INK = (33, 37, 41)
MUTED = (108, 117, 125)
LINE = (210, 214, 219)
SHADE = (244, 246, 248)
WHITE = (255, 255, 255)
LOGO_HEIGHTS = {"petit": 12, "moyen": 18, "grand": 26}
LOGO_MAX_WIDTH = 80


def hex_to_rgb(value):
    value = (value or "").lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def _mix(color, other, share):
    """`share` de `color`, le reste de `other`."""
    return tuple(round(c * share + o * (1 - share)) for c, o in zip(color, other, strict=True))


def _readable_on(color):
    """Blanc ou encre, selon ce qui se lit le mieux sur ce fond (luminance relative, seuil WCAG)."""

    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in color)
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return WHITE if (1.05 / (luminance + 0.05)) >= ((luminance + 0.05) / (0.0212 + 0.05)) else INK


@dataclass
class Style:
    layout: str
    accent: tuple  # titre, en-tête du tableau, total
    on_accent: tuple  # texte posé sur un aplat de la couleur
    tint: tuple  # aplat léger
    logo: Path | None
    logo_w: float
    logo_h: float
    logo_right: bool
    recipient_label: str
    designation_label: str

    @classmethod
    def of(cls, issuer):
        layout = issuer.get("pdf_layout") if issuer.get("pdf_layout") in Issuer.LAYOUTS else "classique"
        try:
            accent = hex_to_rgb(issuer.get("pdf_color") or branding.current().theme_color)
        except ValueError:
            accent = INK
        if layout == "sobre":
            accent = INK
        logo, logo_w, logo_h = None, 0, 0
        size = issuer.get("logo_size") or "moyen"
        if size in LOGO_HEIGHTS:
            logo = _logo_path(issuer)
            if logo is not None:
                logo_w, logo_h = _logo_box(logo, LOGO_HEIGHTS[size])
                if not logo_w:
                    logo = None
        return cls(
            layout=layout,
            accent=accent,
            on_accent=_readable_on(accent),
            tint=SHADE if layout == "sobre" else _mix(accent, WHITE, 0.1),
            logo=logo,
            logo_w=logo_w,
            logo_h=logo_h,
            logo_right=issuer.get("logo_position") == "droite",
            recipient_label=(issuer.get("recipient_label") or Issuer.DEFAULT_RECIPIENT_LABEL).strip(),
            designation_label=(issuer.get("designation_label") or Issuer.DEFAULT_DESIGNATION_LABEL).strip(),
        )


def _logo_box(path, height):
    """Taille imprimée du logo (mm) : la hauteur voulue, réduite si le logo très large dépasserait LOGO_MAX_WIDTH."""
    try:
        with Image.open(path) as image:
            width_px, height_px = image.size
    except OSError:
        return 0, 0
    if not width_px or not height_px:
        return 0, 0
    ratio = width_px / height_px
    width = height * ratio
    if width > LOGO_MAX_WIDTH:
        width, height = LOGO_MAX_WIDTH, LOGO_MAX_WIDTH / ratio
    return width, height


def long_date(value):
    return f"{value.day}{'er' if value.day == 1 else ''} {MONTHS[value.month - 1]} {value.year}" if value else ""


def _address_lines(data, *, with_country=True):
    lines = [data.get("address"), data.get("address_extra")]
    city = " ".join(x for x in (data.get("postcode"), data.get("city")) if x)
    lines.append(city)
    country = (data.get("country") or "").strip()
    if with_country and country and country.lower() != "france":
        lines.append(country)
    return [line for line in lines if line]


class InvoicePDF(FPDF):
    def __init__(self, invoice, issuer, footer_text, style):
        super().__init__(format="A4", unit="mm")
        self.invoice = invoice
        self.issuer_data = issuer
        self.footer_text = footer_text
        self.style = style
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(True, margin=26)
        self.add_font(FONT, "", str(FONT_DIR / "DejaVuSansCondensed.ttf"))
        self.add_font(FONT, "B", str(FONT_DIR / "DejaVuSansCondensed-Bold.ttf"))
        self.add_font(FONT, "I", str(FONT_DIR / "DejaVuSansCondensed-Oblique.ttf"))
        title = f"{invoice.kind_label} {invoice.display_number}"
        self.set_title(title)
        self.set_author(issuer.get("legal_name") or "")
        self.set_creator("Facture")
        self.alias_nb_pages()

    def font(self, size=9, style="", color=INK):
        self.set_font(FONT, style, size)
        self.set_text_color(*color)

    def footer(self):
        self.set_y(-20)
        self.set_draw_color(*(self.style.accent if self.style.layout == "bandeau" else LINE))
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(1.5)
        self.font(7, color=MUTED)
        self.multi_cell(0, 3.4, self.footer_text, align="C")
        self.set_y(-9)
        self.cell(0, 4, f"{self.invoice.display_number} · page {self.page_no()}/{{nb}}", align="R")


def _footer_text(issuer):
    parts = [issuer.get("legal_name"), issuer.get("legal_form")]
    if issuer.get("siret"):
        parts.append(f"SIRET {issuer['siret']}")
    if issuer.get("rna"):
        parts.append(f"RNA {issuer['rna']}")
    if issuer.get("vat_number"):
        parts.append(f"TVA {issuer['vat_number']}")
    text = " · ".join(p for p in parts if p)
    if issuer.get("footer"):
        text += "\n" + issuer["footer"].strip()
    return text


def _logo_path(issuer_data):
    upload = Path(current_app.config["UPLOAD_DIR"])
    for relative in (issuer_data.get("logo"), branding.current().logo):
        if relative:
            path = upload / relative
            if path.is_file():
                return path
    return None


def render(invoice, issuer=None, customer=None, watermark="BROUILLON"):
    """Contenu du PDF (octets). `issuer` et `customer` remplacent les coordonnées de la pièce (aperçu d'une mise en
    page) ; `watermark` est la mention en travers d'une pièce non émise."""
    issued = not invoice.is_draft
    if issuer is None:
        issuer = (
            invoice.issuer_snapshot if issued and invoice.issuer_snapshot else billing.issuer_snapshot(invoice.issuer)
        )
    if customer is None:
        customer = invoice.recipient
    subject_to_vat = issuer.get("vat_regime") == Issuer.SUBJECT
    style = Style.of(issuer)
    pdf = InvoicePDF(invoice, issuer, _footer_text(issuer), style)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    header_bottom = _header(pdf, invoice, issuer, style)

    # Client, dans un cadre à droite.
    y = header_bottom + 6
    box_x, box_w = pdf.w - pdf.r_margin - 88, 88
    pdf.set_xy(box_x + 4, y + 3)
    pdf.font(7.5, "B" if style.layout != "sobre" else "", style.accent if style.layout != "sobre" else MUTED)
    label = style.recipient_label if not invoice.is_credit else "Client"
    pdf.cell(box_w - 8, 4, label.upper(), new_x="LEFT", new_y="NEXT")
    pdf.font(10.5, "B")
    pdf.multi_cell(box_w - 8, 5, customer.get("name") or "", new_x="LEFT", new_y="NEXT")
    pdf.font(9)
    client_lines = []
    if customer.get("contact_name"):
        client_lines.append(f"À l'attention de {customer['contact_name']}")
    client_lines += _address_lines(customer)
    if customer.get("siret"):
        client_lines.append(f"SIRET {customer['siret']}")
    if customer.get("vat_number"):
        client_lines.append(f"TVA {customer['vat_number']}")
    pdf.multi_cell(box_w - 8, 4.4, "\n".join(client_lines), new_x="LEFT", new_y="NEXT")
    box_bottom = pdf.get_y() + 3
    pdf.set_draw_color(*LINE)
    pdf.rect(box_x, y, box_w, box_bottom - y, round_corners=True, corner_radius=1.5)

    # Objet, prestation, références.
    pdf.set_xy(pdf.l_margin, y + 3)
    info = []
    if invoice.subject:
        info.append(("Objet", invoice.subject))
    if invoice.service_date:
        if invoice.service_end and invoice.service_end != invoice.service_date:
            info.append(("Période", f"du {long_date(invoice.service_date)} au {long_date(invoice.service_end)}"))
        else:
            info.append(("Date de la prestation", long_date(invoice.service_date)))
    if invoice.customer_reference:
        info.append(("Votre référence", invoice.customer_reference))
    if invoice.is_credit and invoice.credited_invoice is not None:
        original = invoice.credited_invoice
        info.append(("Facture corrigée", f"{original.number} du {long_date(original.issue_date)}"))
    for label, value in info:
        pdf.set_x(pdf.l_margin)
        pdf.font(7.5, color=MUTED)
        pdf.cell(width - box_w - 8, 4, label.upper(), new_x="LMARGIN", new_y="NEXT")
        pdf.font(9.5)
        pdf.multi_cell(width - box_w - 8, 4.6, value, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
    pdf.set_y(max(pdf.get_y(), box_bottom) + 7)

    if issuer.get("intro_text"):
        pdf.font(9.5)
        pdf.multi_cell(width, 4.8, issuer["intro_text"].strip(), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    _lines_table(pdf, invoice, subject_to_vat, width, style)
    _totals(pdf, invoice, subject_to_vat, issuer, width, style)
    _closing(pdf, invoice, issuer, customer, width)
    if issuer.get("closing_text"):
        pdf.ln(1)
        pdf.font(9.5, "I")
        pdf.multi_cell(width, 4.8, issuer["closing_text"].strip(), new_x="LMARGIN", new_y="NEXT")

    if invoice.is_draft and watermark:
        _watermark(pdf, watermark)
    return bytes(pdf.output())


def _image(pdf, style, x, y):
    pdf.image(str(style.logo), x=x, y=y, w=style.logo_w, h=style.logo_h)


def _issuer_block(pdf, issuer, y, color=INK, muted=MUTED):
    pdf.set_xy(pdf.l_margin, y)
    pdf.font(11, "B", color)
    pdf.multi_cell(95, 5, issuer.get("legal_name") or "", new_x="LMARGIN", new_y="NEXT")
    pdf.font(8.5, color=muted)
    lines = _address_lines(issuer)
    contact = " · ".join(x for x in (issuer.get("phone"), issuer.get("email")) if x)
    if contact:
        lines.append(contact)
    if issuer.get("website"):
        lines.append(issuer["website"])
    pdf.multi_cell(95, 4, "\n".join(lines), new_x="LMARGIN", new_y="NEXT")
    return pdf.get_y()


def _facts(pdf, invoice, x, y, title_color, *, with_title=True, color=MUTED):
    """Titre (FACTURE), numéro et dates, alignés à droite dans une colonne de 80 mm. Renvoie le bas du bloc."""
    pdf.set_xy(x, y)
    if with_title:
        pdf.font(20, "B", title_color)
        pdf.cell(80, 9, invoice.kind_label.upper(), align="R", new_x="LEFT", new_y="NEXT")
        pdf.font(10, "B", title_color)
        pdf.cell(80, 5.5, invoice.number or "Brouillon (sans numéro)", align="R", new_x="LEFT", new_y="NEXT")
    pdf.font(8.5, color=color)
    if invoice.issue_date:
        pdf.cell(80, 4.5, f"Date : {long_date(invoice.issue_date)}", align="R", new_x="LEFT", new_y="NEXT")
    if invoice.due_date and not invoice.is_credit:
        pdf.cell(80, 4.5, f"Échéance : {long_date(invoice.due_date)}", align="R", new_x="LEFT", new_y="NEXT")
    return pdf.get_y()


def _header(pdf, invoice, issuer, style):
    """En-tête selon la disposition : logo, émetteur, titre, numéro et dates. Renvoie le bas de l'en-tête."""
    top = pdf.t_margin
    right_x = pdf.w - pdf.r_margin - 80
    title_color = INK if style.layout == "sobre" else style.accent

    if style.layout == "bandeau":
        # Aplat de couleur pleine largeur : logo d'un côté, titre et numéro de l'autre.
        band = max(style.logo_h + 12, 30)
        pdf.set_fill_color(*style.accent)
        pdf.rect(0, 0, pdf.w, band, style="F")
        if style.logo:
            # Sur une pastille blanche : un logo sombre ou de la couleur du bandeau resterait sinon invisible.
            x = pdf.w - pdf.r_margin - style.logo_w - 2 if style.logo_right else pdf.l_margin + 2
            y = (band - style.logo_h) / 2
            pdf.set_fill_color(*WHITE)
            pdf.rect(x - 2, y - 2, style.logo_w + 4, style.logo_h + 4, style="F", round_corners=True, corner_radius=2)
            _image(pdf, style, x, y)
        title_x = pdf.l_margin if style.logo_right else right_x
        align = "L" if style.logo_right else "R"
        pdf.set_xy(title_x, band / 2 - 7.5)
        pdf.font(20, "B", style.on_accent)
        pdf.cell(80, 9, invoice.kind_label.upper(), align=align, new_x="LEFT", new_y="NEXT")
        pdf.font(10, "B", style.on_accent)
        pdf.cell(80, 5.5, invoice.number or "Brouillon (sans numéro)", align=align, new_x="LEFT", new_y="NEXT")
        y = band + 7
        left = _issuer_block(pdf, issuer, y)
        right = _facts(pdf, invoice, right_x, y, title_color, with_title=False)
        return max(left, right)

    # Classique et sobre : le logo au-dessus de l'émetteur (à gauche) ou au-dessus du titre (à droite).
    left_top = right_top = top
    if style.logo:
        if style.logo_right:
            _image(pdf, style, pdf.w - pdf.r_margin - style.logo_w, top)
            right_top = top + style.logo_h + 3
        else:
            _image(pdf, style, pdf.l_margin, top)
            left_top = top + style.logo_h + 2
    left = _issuer_block(pdf, issuer, left_top)
    right = _facts(pdf, invoice, right_x, right_top, title_color)
    if style.layout == "sobre":
        bottom = max(left, right) + 3
        pdf.set_draw_color(*INK)
        pdf.set_line_width(0.4)
        pdf.line(pdf.l_margin, bottom, pdf.w - pdf.r_margin, bottom)
        pdf.set_line_width(0.2)
        return bottom
    return max(left, right)


def _columns(subject_to_vat, width, designation="Désignation"):
    if subject_to_vat:
        widths = [width - 16 - 26 - 16 - 28, 16, 26, 16, 28]
        heads = [designation, "Qté", "Prix unit. HT", "TVA", "Total HT"]
    else:
        widths = [width - 18 - 30 - 30, 18, 30, 30]
        heads = [designation, "Qté", "Prix unitaire", "Total"]
    return widths, heads


def _table_head(pdf, widths, heads, style):
    if style.layout == "bandeau":
        fill, text = style.accent, style.on_accent
    elif style.layout == "sobre":
        fill, text = None, INK
    else:
        fill, text = style.tint, style.accent
    if fill:
        pdf.set_fill_color(*fill)
    pdf.font(8, "B", text)
    y = pdf.get_y()
    for i, (w, head) in enumerate(zip(widths, heads, strict=True)):
        pdf.cell(w, 7, head.upper(), fill=bool(fill), align="L" if i == 0 else "R")
    pdf.ln(7)
    if not fill:
        pdf.set_draw_color(*INK)
        pdf.line(pdf.l_margin, y + 7, pdf.l_margin + sum(widths), y + 7)


def _lines_table(pdf, invoice, subject_to_vat, width, style):
    widths, heads = _columns(subject_to_vat, width, style.designation_label)
    _table_head(pdf, widths, heads, style)
    for line in invoice.lines:
        label_w = widths[0] - 2
        pdf.font(9, "B")
        label_lines = pdf.multi_cell(label_w, 4.6, line.label, dry_run=True, output="LINES")
        pdf.font(8)
        desc_lines = (
            pdf.multi_cell(label_w, 4, line.description, dry_run=True, output="LINES") if line.description else []
        )
        height = 2 + len(label_lines) * 4.6 + len(desc_lines) * 4 + 2
        if pdf.get_y() + height > pdf.page_break_trigger:
            pdf.add_page()
            _table_head(pdf, widths, heads, style)
        y = pdf.get_y()
        x = pdf.l_margin
        pdf.set_xy(x + 1, y + 2)
        pdf.font(9, "B")
        pdf.multi_cell(label_w, 4.6, line.label, new_x="LEFT", new_y="NEXT")
        if line.description:
            pdf.font(8, color=MUTED)
            pdf.multi_cell(label_w, 4, line.description, new_x="LEFT", new_y="NEXT")
        quantity = money.number(line.quantity) + (f" {line.unit}" if line.unit else "")
        cells = [quantity, money.amount(line.unit_price_cents)]
        if subject_to_vat:
            cells.append(money.rate(line.vat_rate))
        cells.append(money.amount(line.total_ht_cents))
        pdf.font(9)
        cx = x + widths[0]
        for w, text in zip(widths[1:], cells, strict=True):
            pdf.set_xy(cx, y + 2)
            pdf.cell(w, 4.6, text, align="R")
            cx += w
        pdf.set_y(y + height)
        pdf.set_draw_color(*LINE)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())


def _totals(pdf, invoice, subject_to_vat, issuer, width, style):
    rows = []
    if subject_to_vat:
        rows.append(("Total HT", money.amount(invoice.total_ht_cents), False))
        for rate, base, vat in invoice.vat_breakdown or []:
            rows.append((f"TVA {money.rate(rate)} sur {money.amount(base)}", money.amount(vat), False))
        rows.append(("Total TTC", money.amount(invoice.total_ttc_cents), True))
    else:
        rows.append(("Total", money.amount(invoice.total_ttc_cents), True))
    needed = len(rows) * 6.5 + 12
    if pdf.get_y() + needed > pdf.page_break_trigger:
        pdf.add_page()
    pdf.ln(3)
    label_w, value_w = 62, 32
    x = pdf.l_margin + width - label_w - value_w
    for label, value, strong in rows:
        pdf.set_x(x)
        if strong:
            if style.layout == "bandeau":
                fill, text = style.accent, style.on_accent
            elif style.layout == "sobre":
                fill, text = None, INK
                pdf.set_draw_color(*INK)
                pdf.line(x, pdf.get_y(), x + label_w + value_w, pdf.get_y())
            else:
                fill, text = style.tint, style.accent
            if fill:
                pdf.set_fill_color(*fill)
            pdf.font(10.5, "B", text)
            pdf.cell(label_w, 8, label, fill=bool(fill), align="R")
            pdf.cell(value_w, 8, value, fill=bool(fill), align="R", new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.font(9, color=MUTED)
            pdf.cell(label_w, 6, label, align="R")
            pdf.font(9)
            pdf.cell(value_w, 6, value, align="R", new_x="LMARGIN", new_y="NEXT")
    mention = issuer.get("vat_mention")
    if mention:
        pdf.ln(1)
        pdf.font(8, "I", MUTED)
        pdf.cell(width, 4.5, mention, align="R", new_x="LMARGIN", new_y="NEXT")
    if invoice.is_credit:
        pdf.font(8.5, color=MUTED)
        pdf.cell(
            width,
            4.5,
            "Montant porté au crédit du client, à déduire de la facture corrigée.",
            align="R",
            new_x="LMARGIN",
            new_y="NEXT",
        )
    pdf.ln(5)


def _block(pdf, width, title, text):
    if not text:
        return
    pdf.font(7.5, "B", MUTED)
    pdf.cell(width, 4.5, title.upper(), new_x="LMARGIN", new_y="NEXT")
    pdf.font(8.5)
    pdf.multi_cell(width, 4.2, text.strip(), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)


def _closing(pdf, invoice, issuer, customer, width):
    _block(pdf, width, "Remarques", invoice.notes)
    if not invoice.is_credit:
        payment = []
        if invoice.due_date:
            payment.append(f"À régler au plus tard le {long_date(invoice.due_date)}.")
        if issuer.get("payment_instructions"):
            payment.append(issuer["payment_instructions"].strip())
        if issuer.get("iban"):
            bank = f"IBAN : {issuer['iban']}"
            if issuer.get("bic"):
                bank += f" · BIC : {issuer['bic']}"
            payment.append(bank)
        if invoice.number:
            payment.append(f"Merci de rappeler la référence {invoice.number} avec votre règlement.")
        _block(pdf, width, "Règlement", "\n".join(payment))
        if customer.get("kind") == Customer.ORGANISATION:
            _block(pdf, width, "Retard de paiement", issuer.get("late_penalty") or billing.DEFAULT_LATE_PENALTY)


def _watermark(pdf, text):
    for page in range(1, pdf.pages_count + 1):
        pdf.page = page
        with pdf.local_context(fill_opacity=0.08):
            pdf.set_font(FONT, "B", 90)
            pdf.set_text_color(200, 30, 30)
            with pdf.rotation(35, x=pdf.w / 2, y=pdf.h / 2):
                pdf.set_xy(0, pdf.h / 2 - 20)
                pdf.cell(pdf.w, 40, text, align="C")


# ---------------------------------------------------------------------------
# Aperçu d'une mise en page
# ---------------------------------------------------------------------------

SAMPLE_CUSTOMER = {
    "kind": Customer.ORGANISATION,
    "name": "Mairie de Saint-Exemple",
    "contact_name": "Service culture",
    "address": "Place de la Mairie",
    "postcode": "45000",
    "city": "Saint-Exemple",
}


def preview(issuer, overrides=None):
    """PDF d'une facture d'exemple avec la mise en page de l'émetteur, modifiée par `overrides` (valeurs du
    formulaire pas encore enregistrées). Rien n'est écrit en base."""
    data = billing.issuer_snapshot(issuer)
    data.update(overrides or {})
    today = date.today()
    subject = issuer.subject_to_vat
    rate = Decimal(issuer.default_vat_rate) if subject else Decimal("0")
    invoice = Invoice(
        kind=Invoice.INVOICE,
        status=Invoice.DRAFT,
        number=f"{issuer.invoice_prefix}{today.year}-{1:0{issuer.number_digits}d}",
        issue_date=today,
        due_date=today + timedelta(days=issuer.payment_terms_days),
        service_date=today,
        subject="Concert du 14 juillet (exemple)",
    )
    invoice.lines = [
        InvoiceLine(
            position=0,
            label="Concert",
            description="Prestation de l'ensemble, 1 h 30, sonorisation comprise",
            quantity=Decimal("1"),
            unit="forfait",
            unit_price_cents=80000,
            vat_rate=rate,
        ),
        InvoiceLine(
            position=1,
            label="Frais de déplacement",
            quantity=Decimal("42"),
            unit="km",
            unit_price_cents=60,
            vat_rate=rate,
        ),
    ]
    billing.compute(invoice, subject_to_vat=subject)
    if SAMPLE_CUSTOMER["kind"] == Customer.ORGANISATION and not data.get("late_penalty"):
        data["late_penalty"] = billing.DEFAULT_LATE_PENALTY
    return render(invoice, issuer=data, customer=SAMPLE_CUSTOMER, watermark="APERÇU")
