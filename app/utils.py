"""Shared utilities — phone normalization & money conversion.

Phone normalization is the backbone of debtor identity. "0809", "+234809",
"234 809", "0809-191-8235" must all collapse to one canonical E.164 form,
otherwise we'd create duplicate debtors for the same human.
"""
from __future__ import annotations

import re


# Nigerian prefixes that mean "drop the leading zero and prepend 234".
# Other countries keep their +. This is a pragmatic NG-first normalizer;
# upgrade to phonenumber_utils if you go international.
_NG_MOBILE_PREFIXES = ("070", "080", "081", "090", "091")


def normalize_phone(raw: str) -> str:
    """Return E.164-ish canonical phone, or '' if unparseable.

    Examples (NG):
      "08091918235"      -> "+2348091918235"
      "+234 809 191 8235"-> "+2348091918235"
      "2348091918235"    -> "+2348091918235"
      "0809-191-8235"    -> "+2348091918235"
    """
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)

    if digits.startswith("234") and len(digits) == 13:
        return f"+{digits}"
    if digits.startswith("0") and len(digits) == 11:
        return f"+234{digits[1:]}"
    if len(digits) == 10 and digits[0] != "0":
        # ambiguous — assume NG mobile missing leading 0
        return f"+234{digits}"
    if raw.strip().startswith("+") and len(digits) > 7:
        return f"+{digits}"
    return f"+{digits}" if digits else ""


def is_valid_phone(raw: str) -> bool:
    n = normalize_phone(raw)
    return bool(n) and 8 <= len(n) <= 16


# ---- money ------------------------------------------------------------
# Internal: integer kobo.  External (Nomba): float Naira.

def naira_to_kobo(naira: float | int | str) -> int:
    """₦2,500.00 -> 250000"""
    return int(round(float(naira) * 100))


def kobo_to_naira(kobo: int) -> float:
    """250000 -> 2500.0"""
    return kobo / 100.0


def fmt_naira(kobo: int) -> str:
    """250000 -> '₦2,500'"""
    return f"₦{kobo_to_naira(kobo):,.0f}"
