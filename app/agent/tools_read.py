"""Agent READ tools.

These are safe to execute immediately — they never mutate the ledger.
Each returns a JSON-serialisable dict the LLM can read and reason about.
"""
from __future__ import annotations

from typing import Optional

from app.db import get_db
from app.utils import fmt_naira


# ---------------------------------------------------------------- merchants

async def lookup_merchant_by_phone(phone: str) -> dict:
    """Return the merchant for a phone, or {exists: False}.

    Used at the very start of every inbound message to decide
    onboarding vs. returning-user flow.
    """
    db = get_db()
    from app.utils import normalize_phone
    norm = normalize_phone(phone)
    m = await db.merchants.find_one({"phone": norm})
    if not m:
        return {"exists": False}
    return {
        "exists": True,
        "merchant_id": str(m["_id"]),
        "name": m.get("name"),
        "business_name": m.get("business_name"),
        "preferred_lang": m.get("preferred_lang", "pidgin"),
        "onboarded": m.get("onboarded", False),
        "wallet_balance": fmt_naira(m.get("balance_kobo", 0)),
    }


# ----------------------------------------------------------------- debtors

async def find_debtors_by_name(merchant_id: str, name_query: str) -> dict:
    """Case-insensitive regex search over a merchant's debtors.

    Returns up to 10 matches. The agent uses this to disambiguate
    'Alhaji' when there are several.
    """
    db = get_db()
    import re
    safe = re.escape(name_query.strip())
    cursor = db.debtors.find(
        {"merchant_id": merchant_id, "name": {"$regex": safe, "$options": "i"}},
    ).limit(10)
    out = []
    async for d in cursor:
        out.append({
            "debtor_id": str(d["_id"]),
            "name": d.get("name"),
            "phone": d.get("phone"),
            "phone_normalized": d.get("phone_normalized"),
        })
    return {"count": len(out), "debtors": out}


async def get_debtor_outstanding(merchant_id: str, debtor_id: str) -> dict:
    """Return the running balance owed by one debtor (across all open debts)."""
    db = get_db()
    owed_kobo = 0
    paid_kobo = 0
    open_debts = []
    cursor = db.debts.find({
        "merchant_id": merchant_id,
        "debtor_id": debtor_id,
        "status": {"$in": ["PENDING", "PARTIAL"]},
    })
    async for debt in cursor:
        owed_kobo += debt["amount_kobo"]
        paid_kobo += debt.get("paid_kobo", 0)
        open_debts.append({
            "reference": debt["reference"],
            "goods": debt.get("goods_description"),
            "amount": fmt_naira(debt["amount_kobo"]),
            "paid": fmt_naira(debt.get("paid_kobo", 0)),
            "balance": fmt_naira(debt["amount_kobo"] - debt.get("paid_kobo", 0)),
            "due_date": str(debt.get("due_date") or ""),
            "status": debt.get("status"),
        })
    return {
        "debtor_id": debtor_id,
        "total_owed": fmt_naira(owed_kobo),
        "total_paid": fmt_naira(paid_kobo),
        "outstanding": fmt_naira(owed_kobo - paid_kobo),
        "open_debts": open_debts,
        "open_count": len(open_debts),
    }


# -------------------------------------------------------------------- debts

async def list_recent_debts(merchant_id: str, limit: int = 10) -> dict:
    """Most-recent-first list of a merchant's debts (any status)."""
    db = get_db()
    cursor = db.debts.find({"merchant_id": merchant_id}).sort("created_at", -1).limit(limit)
    out = []
    async for debt in cursor:
        # DRAFTs have no debtor yet — fall back to the draft name.
        debtor = None
        if debt.get("debtor_id"):
            debtor = await db.debtors.find_one({"_id": _oid(debt["debtor_id"])})
        out.append({
            "debt_id": str(debt["_id"]),
            "reference": debt["reference"],
            "debtor_name": (debtor or {}).get("name") or debt.get("draft_name") or "Unknown",
            "goods": debt.get("goods_description"),
            "amount": fmt_naira(debt["amount_kobo"]),
            "paid": fmt_naira(debt.get("paid_kobo", 0)),
            "balance": fmt_naira(debt["amount_kobo"] - debt.get("paid_kobo", 0)),
            "status": debt.get("status"),
            "due_date": str(debt.get("due_date") or ""),
        })
    return {"count": len(out), "debts": out}


async def get_wallet_summary(merchant_id: str) -> dict:
    """Dashboard numbers: wallet balance, outstanding, paid-this-month."""
    from datetime import datetime
    db = get_db()
    m = await db.merchants.find_one({"_id": _oid(merchant_id)})
    if not m:
        return {"exists": False}

    # sum outstanding across open debts
    outstanding_kobo = 0
    async for debt in db.debts.find(
        {"merchant_id": merchant_id, "status": {"$in": ["PENDING", "PARTIAL"]}}
    ):
        outstanding_kobo += debt["amount_kobo"] - debt.get("paid_kobo", 0)

    return {
        "wallet_balance": fmt_naira(m.get("balance_kobo", 0)),
        "outstanding_owed_to_you": fmt_naira(outstanding_kobo),
        "account_number": m.get("master_account_number"),
    }


# ------------------------------------------------------------------- helper

from bson import ObjectId


def _oid(_id: str) -> ObjectId:
    """Safely coerce string id -> ObjectId."""
    return ObjectId(_id) if ObjectId.is_valid(_id) else _id


async def who_owes_me(merchant_id: str) -> dict:
    """Return a per-debtor breakdown of outstanding money, summed by the DB.

    This is the tool for 'how much is everyone owing me?' — the LLM should
    call THIS, not try to sum list_recent_debts itself. Mongo does the math.

    Returns each debtor with an outstanding balance, sorted biggest first,
    plus the grand total so the agent can lead with it.
    """
    db = get_db()

    # Aggregate per-debtor across all open debts. Mongo does the summing.
    pipeline = [
        {"$match": {
            "merchant_id": merchant_id,
            "status": {"$in": ["PENDING", "PARTIAL"]},
        }},
        {"$group": {
            "_id": "$debtor_id",
            "total_owed_kobo": {"$sum": "$amount_kobo"},
            "total_paid_kobo": {"$sum": {"$ifNull": ["$paid_kobo", 0]}},
            "debt_count": {"$sum": 1},
        }},
        {"$addFields": {
            "outstanding_kobo": {"$subtract": ["$total_owed_kobo", "$total_paid_kobo"]},
        }},
        {"$match": {"outstanding_kobo": {"$gt": 0}}},   # skip fully-paid groups
        {"$sort": {"outstanding_kobo": -1}},             # biggest debtor first
    ]

    rows = []
    grand_total_kobo = 0
    async for doc in db.debts.aggregate(pipeline):
        debtor = await db.debtors.find_one({"_id": _oid(doc["_id"])})
        rows.append({
            "name": (debtor or {}).get("name", "Unknown"),
            "phone": (debtor or {}).get("phone_normalized"),
            "outstanding": fmt_naira(doc["outstanding_kobo"]),
            "open_debts": doc["debt_count"],
        })
        grand_total_kobo += doc["outstanding_kobo"]

    return {
        "currency": "NGN",
        "debtor_count": len(rows),
        "total_outstanding": fmt_naira(grand_total_kobo),
        "debtors": rows,
    }


async def list_drafts(merchant_id: str) -> dict:
    """Return all open DRAFT debts for this merchant, newest first.

    The agent calls this to find drafts that need completing, or to tell
    the merchant 'you get 3 drafts wey dey wait for phone numbers'.
    """
    db = get_db()
    cursor = db.debts.find(
        {"merchant_id": merchant_id, "status": "DRAFT"},
    ).sort("created_at", -1).limit(20)
    out = []
    async for d in cursor:
        out.append({
            "debt_id": str(d["_id"]),
            "reference": d.get("reference"),
            "debtor_name": d.get("draft_name"),
            "goods": d.get("goods_description"),
            "amount": fmt_naira(d["amount_kobo"]),
            "due_date": str(d.get("due_date") or ""),
            "missing_fields": d.get("missing_fields") or [],
            "created_at": str(d.get("created_at", "")),
        })
    return {"count": len(out), "drafts": out}


async def get_merchant_login_details(phone: str) -> dict:
    """Retrieve a merchant's login URL and password so the agent can tell them.

    The password is stored hashed; we re-generate it only once at onboarding and
    store the plaintext in a separate `login_password` field so we can show it
    to the merchant when they ask. Not prod-secure, but correct for a hackathon
    where merchants need to hear their password over WhatsApp.
    """
    from app.utils import normalize_phone
    from app.config import settings
    db = get_db()
    norm = normalize_phone(phone)
    m = await db.merchants.find_one({"phone": norm})
    if not m:
        return {"exists": False}
    return {
        "exists": True,
        "name": m.get("name"),
        "wallet_url": settings.app_base_url + "/wallet/login",
        "phone": m.get("phone"),
        "password": m.get("login_password", "not_set"),
    }
