from datetime import date, datetime

from .services import money

MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def date_fr(value, with_time=False):
    if not value:
        return ""
    if isinstance(value, datetime):
        s = f"{value.day} {MONTHS[value.month - 1]} {value.year}"
        return f"{s} à {value:%H:%M}" if with_time else s
    if isinstance(value, date):
        return f"{value.day} {MONTHS[value.month - 1]} {value.year}"
    return value


def register_filters(app):
    app.add_template_filter(date_fr, "date_fr")
    app.add_template_filter(lambda v: date_fr(v, with_time=True), "datetime_fr")
    app.add_template_filter(money.amount, "money")
    app.add_template_filter(money.number, "number")
    app.add_template_filter(money.rate, "rate")
