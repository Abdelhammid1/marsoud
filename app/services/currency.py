"""MARSOUD-CURRENCY-AR — one place that turns a currency code into Arabic.

`companies.base_currency` stores the 3-letter ISO code, which is right and
stays that way. Only the *display* changes: users read "جنيه مصري", not "EGP".

Before this module the mapping existed three times, inline and inconsistent
(companies/form.html said "دولار", auth/register.html said "دولار أمريكي",
auth/choose_plan.html carried a symbol map), and most of the app printed the
raw code with no mapping at all.

Exposed two ways because a Jinja filter can't reach everything:
  · `currency_ar` filter — templates, emails, the WeasyPrint PDFs
  · `currency_name_ar()` — the ReportLab exports and CSV/XLSX backups,
    which build strings in Python
"""

# ISO 4217 code → Arabic name. Keys are upper-case; lookup normalises.
CURRENCY_NAMES_AR = {
    "SAR": "ريال سعودي",
    "EGP": "جنيه مصري",
    "AED": "درهم إماراتي",
    "USD": "دولار أمريكي",
    "EUR": "يورو",
}

# Display order for the pickers, so every currency dropdown in the app
# lists the same currencies in the same order.
CURRENCY_ORDER = ["EGP", "SAR", "AED", "USD", "EUR"]


def currency_name_ar(code):
    """Arabic name for an ISO currency code.

    Falls back to the raw code for anything unmapped (the ticket asks for
    the code rather than an empty string), and returns "" for None/blank
    so templates don't print "None".
    """
    key = (code or "").strip().upper()
    if not key:
        return ""
    return CURRENCY_NAMES_AR.get(key, key)


def currency_choices():
    """[(code, "EGP — جنيه مصري"), ...] for the currency <select> lists."""
    return [(c, f"{c} — {CURRENCY_NAMES_AR[c]}") for c in CURRENCY_ORDER]
