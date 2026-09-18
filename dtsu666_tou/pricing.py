"""Electricity cost: tariffs.ini loading and per-day/per-month cost math.

All monetary values produced here are gross (VAT-inclusive).  This module
is pure (no MQTT / no DB) so the cost math is unit-testable in isolation.
"""

import configparser as _cp

from datetime import datetime

from .config import TARIFFS_FILE


# ============================================================
# Tariff loading
# ============================================================

_COST_LAST_MTIME = 0.0
_COST_CACHE = None   # (mtime, parsed_tariffs_list)


def load_tariffs(path=TARIFFS_FILE):
    """Parse tariffs.ini: base [COST] + optional [COST YYYY-MM-DD] sections.

    Returns list of dicts sorted by valid_from (the base section has
    valid_from = date(1,1,1))."""
    cfg = _cp.ConfigParser()
    if not cfg.read(path):
        return []

    entries = []
    for section in cfg.sections():
        if not section.startswith("COST"):
            continue
        raw = section[len("COST"):].strip()
        if raw:
            valid_from = datetime.strptime(raw, "%Y-%m-%d").date()  # noqa: DTZ007
        else:
            valid_from = datetime(2000, 1, 1).date()  # noqa: DTZ001  base section - long ago

        def _f(section=section, k=""):
            try: return float(cfg[section][k])
            except (KeyError, ValueError): return None

        entries.append({
            "valid_from": valid_from,
            "energija_vt": _f(k="energija_vt"),
            "energija_nt": _f(k="energija_nt"),
            "prijenos_vt": _f(k="prijenos_vt"),
            "prijenos_nt": _f(k="prijenos_nt"),
            "distribucija_vt": _f(k="distribucija_vt"),
            "distribucija_nt": _f(k="distribucija_nt"),
            "oie_kwh": _f(k="oie_kwh"),
            "opskrba_month": _f(k="opskrba_month"),
            "mjerno_mjesto_month": _f(k="mjerno_mjesto_month"),
            "vat_percent": _f(k="vat_percent"),
        })

    # Fill missing values from the base section (first one)
    base = entries[0] if entries else {}
    for e in entries[1:]:
        for k in ("energija_vt", "energija_nt", "prijenos_vt", "prijenos_nt",
                  "distribucija_vt", "distribucija_nt", "oie_kwh",
                  "opskrba_month", "mjerno_mjesto_month", "vat_percent"):
            if e[k] is None:
                e[k] = base[k]

    entries.sort(key=lambda e: e["valid_from"])
    return entries


def tariff_for_date(tariffs, d):
    """Return the tariff dictionary valid on date *d*."""
    best = None
    for t in tariffs:
        if t["valid_from"] <= d:
            best = t
    return best or tariffs[0] if tariffs else {}


def calculate_cost(vt_kwh, nt_kwh, total_kwh, tariff, include_fixed):
    """Return cost breakdown dict - all monetary values are gross (VAT-inclusive).
    *include_fixed* adds monthly fees (opskrba+mjerno mjesto)."""
    e_vt = tariff.get("energija_vt", 0) or 0
    e_nt = tariff.get("energija_nt", 0) or 0
    p_vt = tariff.get("prijenos_vt", 0) or 0
    p_nt = tariff.get("prijenos_nt", 0) or 0
    d_vt = tariff.get("distribucija_vt", 0) or 0
    d_nt = tariff.get("distribucija_nt", 0) or 0
    oie  = tariff.get("oie_kwh", 0) or 0
    ops  = tariff.get("opskrba_month", 0) or 0
    mm   = tariff.get("mjerno_mjesto_month", 0) or 0
    vatp = tariff.get("vat_percent", 0) or 0
    factor = 1.0 + vatp / 100.0

    _net = lambda n: round(n, 6)
    energija_n   = vt_kwh * e_vt + nt_kwh * e_nt
    prijenos_n   = vt_kwh * p_vt + nt_kwh * p_nt
    distrib_n    = vt_kwh * d_vt + nt_kwh * d_nt
    oie_n        = total_kwh * oie
    ops_n        = ops if include_fixed else 0.0
    mm_n         = mm if include_fixed else 0.0
    sub_net      = energija_n + prijenos_n + distrib_n + oie_n + ops_n + mm_n
    vat_total    = sub_net * vatp / 100.0

    return {
        "energija":       round(energija_n * factor, 4),
        "prijenos":       round(prijenos_n * factor, 4),
        "distribucija":   round(distrib_n * factor, 4),
        "oie":            round(oie_n * factor, 4),
        "opskrba":        round(ops_n * factor, 4),
        "mjerno_mjesto":  round(mm_n * factor, 4),
        "subtotal":       round(sub_net * factor, 4),
        "vat":            round(vat_total, 4),
        "total":          round(sub_net * factor, 4),
    }
