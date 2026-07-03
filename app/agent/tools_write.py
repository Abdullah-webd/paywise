"""Agent WRITE tools.

Every write tool is split into TWO functions:
  - propose_<x>(...)   → builds a `pending_action` dict describing the change.
  - commit_<x>(token)  → executes the change (only after the merchant says yes).

The agent never calls commit_* itself. The graph's confirmation node holds the
pending_action, the merchant replies "yes"/"no", and only then is commit run.

Why split it this way? It is physically impossible for the model to skip the
human checkpoint — the execute path simply isn't exposed to the LLM. This is
stronger than "ask the model nicely to confirm" — it's structural.
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone, date
from typing import Optional

from app.db import get_db
from app.models import gen_ref
from app.utils import normalize_phone, naira_to_kobo, fmt_naira
from bson import ObjectId


def _oid(_id: str):
    return ObjectId(_id) if ObjectId.is_valid(_id) else _id


def _token() -> str:
    return secrets.token_urlsafe(12)


def _generate_password(length: int = 8) -> str:
    """Generate a simple, memorable password — letters + digits, no weird symbols.

    Example: "kaze7mOP" — easy to read off a WhatsApp message.
    """
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def _parse_date(value) -> "date":
    """Best-effort parse of an ISO date string OR a Mongo date object → date."""
    from datetime import date as date_cls
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_cls):
        return value
    return date_cls.fromisoformat(str(value)[:10])


# ============================================================ ONBOARD MERCHANT

async def propose_onboard_merchant(
    phone: str,
    name: str,
    business_name: str,
    preferred_lang: str = "pidgin",
) -> dict:
    """Propose creating a brand-new merchant.

    Onboarding is the ONE write that doesn't strictly need explicit 'yes' —
    because the act of volunteering their name IS the confirmation. But we
    still route it through propose/commit for symmetry and so the graph can
    show a summary before we commit.
    """
    norm = normalize_phone(phone)
    db = get_db()
    existing = await db.merchants.find_one({"phone": norm})
    if existing:
        return {"error": "already_exists", "merchant_id": str(existing["_id"])}

    return {
        "action_type": "onboard_merchant",
        "token": _token(),
        "params": {
            "phone": norm,
            "name": name.strip(),
            "business_name": business_name.strip(),
            "preferred_lang": preferred_lang,
        },
        "summary": f"Set up PayWise for {name} ({business_name}).",
    }


async def commit_onboard_merchant(params: dict) -> dict:
    db = get_db()
    now = datetime.now(timezone.utc)
    password = _generate_password(8)
    doc = {
        "public_id": gen_ref(),
        "phone": params["phone"],
        "name": params["name"],
        "business_name": params["business_name"],
        "preferred_lang": params.get("preferred_lang", "pidgin"),
        "balance_kobo": 0,
        "onboarded": True,
        "password_hash": _hash_password(password),
        "login_password": password,   # plaintext copy so the agent can remind them (hackathon)
        "created_at": now,
        "updated_at": now,
    }
    result = await db.merchants.insert_one(doc)
    merchant_id = str(result.inserted_id)

    # Kick off master wallet creation (Nomba) asynchronously — non-blocking.
    # We don't await it here: the merchant can start logging debts immediately,
    # and the master account fills in moments later.
    from app.services.nomba import nomba
    import asyncio
    asyncio.create_task(_create_master_wallet(merchant_id, params["business_name"]))
    return {"merchant_id": merchant_id, "password": password, **params}


async def _create_master_wallet(merchant_id: str, business_name: str) -> None:
    """Best-effort master virtual account so the merchant has a settlement home."""
    try:
        from app.services.nomba import nomba
        ref = gen_ref()
        acct = await nomba.create_virtual_account(
            account_ref=ref, amount_naira=0.0,  # informational only
        )
        db = get_db()
        await db.merchants.update_one(
            {"_id": ObjectId(merchant_id)},
            {"$set": {
                "master_account_number": acct["account_number"],
                "master_account_ref": ref,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        # also persist a record in virtual_accounts
        await db.virtual_accounts.insert_one({
            "merchant_id": merchant_id,
            "account_ref": ref,
            "account_number": acct["account_number"],
            "bank_name": acct.get("bank_name", "Nomba"),
            "bank_code": acct.get("bank_code"),
            "is_master": True,
            "is_active": True,
            "created_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        # Non-fatal: merchant still works; we retry later. Never crash onboarding.
        import logging
        logging.getLogger("paywise.tools").warning("master wallet create failed: %s", e)


# ============================================================ RECORD NEW DEBT
#
# Two outcomes:
#   - debtor_phone present & valid  → propose a normal PENDING debt (confirmed).
#   - debtor_phone MISSING / bad    → propose a DRAFT, which is AUTO-committed
#     (saving a draft is not a destructive write — nothing to confirm). The
#     merchant is then asked for the missing field. See complete_pending_debt
#     for the DRAFT → PENDING promotion.

async def propose_record_debt(
    merchant_id: str,
    debtor_name: str,
    debtor_phone: Optional[str],
    goods_description: str,
    amount_naira: float,
    due_date: Optional[str] = None,
) -> dict:
    """Propose recording a new credit sale.

    If the debtor's phone is missing or unparseable, this does NOT fail — it
    returns a `record_debt_draft` action that the graph auto-commits as a DRAFT
    so the sale is never lost. The merchant is then asked for the phone.
    """
    norm = normalize_phone(debtor_phone or "")
    amount_kobo = naira_to_kobo(amount_naira)
    name = (debtor_name or "").strip() or None
    goods = (goods_description or "").strip()

    if not norm:
        # ---- missing phone → DRAFT (no confirmation needed; nothing is lost) ----
        missing = ["debtor_phone"]
        if not name:
            missing.append("debtor_name")
        params = {
            "merchant_id": merchant_id,
            "debtor_name": name,
            "debtor_phone_raw": (debtor_phone or "").strip() or None,
            "debtor_phone_normalized": None,
            "goods_description": goods,
            "amount_kobo": amount_kobo,
            "due_date": due_date,
            "missing_fields": missing,
        }
        label = name or "this customer"
        summary = (
            f"I don save {fmt_naira(amount_kobo)} credit for {label} as DRAFT "
            f"({goods}). Wetin be {label}'s phone number make I complete am?"
        )
        return {
            "action_type": "record_debt_draft",
            "token": _token(),
            "params": params,
            "summary": summary,
            "auto_commit": True,   # saving a draft is not a destructive write
        }

    # ---- normal path: phone known → existing debtor or new ----
    db = get_db()
    existing_debtor = await db.debtors.find_one(
        {"merchant_id": merchant_id, "phone_normalized": norm}
    )

    params = {
        "merchant_id": merchant_id,
        "debtor_name": name,
        "debtor_phone_raw": debtor_phone,
        "debtor_phone_normalized": norm,
        "goods_description": goods,
        "amount_kobo": amount_kobo,
        "due_date": due_date,
        "existing_debtor_id": str(existing_debtor["_id"]) if existing_debtor else None,
    }

    debtor_label = existing_debtor["name"] if existing_debtor else name
    summary = (
        f"Record {fmt_naira(params['amount_kobo'])} credit for {debtor_label} "
        f"({norm}) — {goods}."
    )
    return {"action_type": "record_debt", "token": _token(), "params": params, "summary": summary}


async def commit_record_debt_draft(params: dict) -> dict:
    """Persist an incomplete sale as a DRAFT debt.

    No debtor row is created (we don't have a phone). The draft sits on the
    ledger until complete_pending_debt promotes it. Even if the merchant never
    returns, the record of the sale is not lost.
    """
    db = get_db()
    debt_doc = {
        "reference": gen_ref(),
        "merchant_id": params["merchant_id"],
        "debtor_id": None,
        "amount_kobo": params["amount_kobo"],
        "paid_kobo": 0,
        "goods_description": params["goods_description"],
        "due_date": params.get("due_date"),
        "status": "DRAFT",
        "draft_name": params.get("debtor_name"),
        "draft_phone_raw": params.get("debtor_phone_raw"),
        "missing_fields": params.get("missing_fields") or ["debtor_phone"],
        "created_at": datetime.now(timezone.utc),
    }
    inserted = await db.debts.insert_one(debt_doc)
    return {
        "debt_id": str(inserted.inserted_id),
        "reference": debt_doc["reference"],
        "status": "DRAFT",
        "amount": fmt_naira(debt_doc["amount_kobo"]),
        "debtor_name": debt_doc["draft_name"],
        "missing_fields": debt_doc["missing_fields"],
        "auto_reply": (
            f"I don save am as draft ✍️ Once you give me "
            f"{('/'.join(debt_doc['missing_fields']))} I go complete am."
        ),
    }


async def commit_record_debt(params: dict) -> dict:
    db = get_db()

    # 1) find-or-create the debtor
    debtor_id = params.get("existing_debtor_id")
    if not debtor_id:
        now = datetime.now(timezone.utc)
        result = await db.debtors.insert_one({
            "merchant_id": params["merchant_id"],
            "name": params["debtor_name"],
            "phone": params["debtor_phone_raw"],
            "phone_normalized": params["debtor_phone_normalized"],
            "created_at": now,
        })
        debtor_id = str(result.inserted_id)

    # 2) create the debt row
    debt_doc = {
        "reference": gen_ref(),
        "merchant_id": params["merchant_id"],
        "debtor_id": debtor_id,
        "amount_kobo": params["amount_kobo"],
        "paid_kobo": 0,
        "goods_description": params["goods_description"],
        "due_date": params.get("due_date"),
        "status": "PENDING",
        "created_at": datetime.now(timezone.utc),
    }
    inserted = await db.debts.insert_one(debt_doc)
    debt_id = str(inserted.inserted_id)

    return {
        "debt_id": debt_id,
        "reference": debt_doc["reference"],
        "debtor_id": debtor_id,
        "amount": fmt_naira(debt_doc["amount_kobo"]),
        "debtor_name": params["debtor_name"],
    }


# =================================================== CREATE COLLECTION ACCOUNT

async def propose_create_collection_account(
    merchant_id: str,
    debtor_id: str,
    debt_id: str,
    amount_naira: float,
) -> dict:
    """Propose generating a temp Nomba VA so the debtor can pay this specific debt.

    Validates that the debt actually exists BEFORE stashing the pending action,
    so we catch LLM-fabricated IDs immediately instead of failing silently at
    commit time.
    """
    db = get_db()
    debt = await db.debts.find_one({"_id": _oid(debt_id), "merchant_id": merchant_id})
    if not debt:
        return {"error": "debt_not_found",
                "detail": f"No debt found with id={debt_id}. "
                          "Call list_recent_debts to get the correct debt_id first."}
    
    # Validate debtor exists — LLMs sometimes confuse debt_id with debtor_id
    debtor = await db.debtors.find_one({"_id": _oid(debtor_id)})
    if not debtor:
        # The agent probably passed the wrong debtor_id. Try to recover from the debt record.
        actual_debtor_id = debt.get("debtor_id")
        if actual_debtor_id:
            actual_debtor = await db.debtors.find_one({"_id": _oid(actual_debtor_id)})
            if actual_debtor:
                debtor_id = str(actual_debtor["_id"])
                debtor = actual_debtor
            else:
                return {"error": "debtor_not_found",
                        "detail": f"debtor_id={debtor_id} doesn't exist. "
                                  "The debt record has debtor_id={actual_debtor_id} which also doesn't exist. "
                                  "The debt may be orphaned."}
        else:
            return {"error": "debtor_not_found",
                    "detail": f"debtor_id={debtor_id} doesn't exist. "
                              "Call find_debtors_by_name to find the correct debtor. "
                              f"The correct debtor_id for this debt is "
                              f"likely {debt.get('debtor_id', 'unknown')}."}

    return {
        "action_type": "create_collection_account",
        "token": _token(),
        "params": {
            "merchant_id": merchant_id,
            "debtor_id": debtor_id,
            "debt_id": debt_id,
            "amount_naira": float(amount_naira),
        },
        "summary": (
            f"Send {fmt_naira(int(amount_naira * 100))} payment request with a "
            f"temp account number to {debt.get('draft_name') or 'the debtor'}."
        ),
    }


async def commit_create_collection_account(params: dict) -> dict:
    """Actually call Nomba and persist the VA + notify the debtor."""
    db = get_db()
    from app.services.nomba import nomba, NombaError
    from app.services.whatsapp import get_whatsapp

    debt = await db.debts.find_one({"_id": _oid(params["debt_id"])})
    if not debt:
        return {"error": "debt_not_found"}
    debtor = await db.debtors.find_one({"_id": _oid(params["debtor_id"])})
    merchant = await db.merchants.find_one({"_id": _oid(params["merchant_id"])})
    if not debtor or not merchant:
        return {"error": "missing_debtor_or_merchant"}

    # ---- reuse existing active VA for this debt (idempotent) ----
    existing_ca = debt.get("collection_account") or {}
    if existing_ca.get("is_active") and existing_ca.get("account_number"):
        return {
            "account_number": existing_ca["account_number"],
            "bank_name": existing_ca.get("bank_name", "Nomba"),
            "debt_id": params["debt_id"],
            "reused": True,
        }

    ref = debt["reference"]

    # ---- Strategy: reuse expired VA if one exists (avoids 2-VA sandbox limit) ----
    expired_va = await db.virtual_accounts.find_one({
        "merchant_id": params["merchant_id"],
        "is_active": False,
        "is_master": {"$ne": True},
    }, sort=[("created_at", -1)])

    if expired_va:
        old_ref = expired_va["account_ref"]
        log.info("reusing expired VA %s for new debt %s", old_ref, ref)
        result = await nomba.update_virtual_account(
            account_ref=old_ref,
            new_account_ref=ref,
            amount_naira=params["amount_naira"],
        )
        if result.get("updated"):
            # Reactivate in DB with new details
            await db.virtual_accounts.update_one(
                {"_id": expired_va["_id"]},
                {"$set": {
                    "account_ref": ref,
                    "is_active": True,
                    "debt_id": params["debt_id"],
                    "created_at": datetime.now(timezone.utc),
                }},
            )
            acct = {
                "account_number": expired_va["account_number"],
                "bank_name": expired_va.get("bank_name", "Nomba"),
                "account_ref": ref,
            }
        else:
            log.warning("update VA failed, trying create...")
            acct = None
    else:
        acct = None

    # ---- If no reuse worked, try creating a new VA ----
    if acct is None:

        # ---- Nomba sandbox cap: 2 VAs per account. Deactivate old stale VAs first. ----
        active_va_count = await db.virtual_accounts.count_documents({
            "merchant_id": params["merchant_id"],
            "is_active": True,
            "is_master": {"$ne": True},
        })
        if active_va_count >= 2:
            # Deactivate the oldest active VA to make room
            oldest = await db.virtual_accounts.find_one_and_update(
                {"merchant_id": params["merchant_id"], "is_active": True, "is_master": {"$ne": True}},
                {"$set": {"is_active": False, "closed_at": datetime.now(timezone.utc)}},
                sort=[("created_at", 1)],
            )
            if oldest:
                log.warning("deactivated oldest VA %s (sandbox limit: 2) — making room", oldest.get("account_ref"))
                if oldest.get("debt_id"):
                    await db.debts.update_one(
                        {"_id": _oid(oldest["debt_id"])},
                        {"$set": {"collection_account.is_active": False}},
                    )

        try:
            acct = await nomba.create_virtual_account(
                account_ref=ref,
                amount_naira=params["amount_naira"],
            )
        except NombaError as e:
            err_msg = str(e)
            if "2 sandbox virtual accounts" in err_msg or "Only 2" in err_msg:
                # Sandbox full — reuse master VA slot by updating its accountRef
                master_ref = merchant.get('master_account_ref', '')
                master_acct = merchant.get('master_account_number', 'N/A')
                log.warning("sandbox full, updating master VA %s -> %s for debt tracking", master_ref, ref)
                update_result = await nomba.update_virtual_account(
                    account_ref=master_ref,
                    new_account_ref=ref,
                    amount_naira=params["amount_naira"],
                )
                if update_result.get("updated"):
                    # Master VA now points to this debt. Persist as collection_account.
                    acct = {
                        "account_number": master_acct,
                        "bank_name": "Nomba",
                        "account_ref": ref,
                    }
                    # IMPORTANT: the master_ref in merchants still points to old ref.
                    # Webhook will match by aliasAccountReference = ref (this debt's ref).
                    # After payment, we restore the master_ref. For demo, that's fine.
                    log.info("master VA updated to debt %s — webhook will match by aliasAccountReference", ref)
                else:
                    # Update failed, use master as-is (no per-debt tracking)
                    log.warning("update master VA failed: %s", update_result)
                    acct = {
                        "account_number": master_acct,
                        "bank_name": "Nomba",
                        "account_ref": master_ref,
                    }
            raise

    # ---- expiry is MERCHANT-DECIDED via due_date, NOT a fixed TTL ----
    # The merchant tells us when the debtor will pay ("next Friday"). That date
    # is the account's natural life. We add a small grace window so a slightly
    # late payment still lands. If no due_date, fall back to the config TTL.
    from datetime import timedelta, date as date_cls, datetime as dt_cls
    from app.config import settings
    due = debt.get("due_date")
    grace = timedelta(days=settings.collection_grace_days)
    if due:
        due_d = due if isinstance(due, date_cls) else _parse_date(due)
        # end of the due day (23:59 UTC) + grace
        expires_at = dt_cls.combine(due_d, dt_cls.max.time()).replace(tzinfo=timezone.utc) + grace
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.nomba_virtual_account_ttl_hours)
    await db.debts.update_one(
        {"_id": debt["_id"]},
        {"$set": {
            "collection_account": {
                "account_ref": ref,
                "account_number": acct["account_number"],
                "bank_name": acct.get("bank_name", "Nomba"),
                "bank_code": acct.get("bank_code"),
                "expected_amount_kobo": int(params["amount_naira"] * 100),
                "currency": "NGN",
                "is_active": True,
                "expires_at": expires_at,
                "created_at": datetime.now(timezone.utc),
            },
        }},
    )
    await db.virtual_accounts.insert_one({
        "merchant_id": params["merchant_id"],
        "debt_id": params["debt_id"],
        "account_ref": ref,
        "account_number": acct["account_number"],
        "bank_name": acct.get("bank_name", "Nomba"),
        "bank_code": acct.get("bank_code"),
        "is_master": False,
        "is_active": True,
        "expires_at": expires_at,
        "created_at": datetime.now(timezone.utc),
    })

    # notify the debtor (WhatsApp) — short text, merchant's preferred flavour
    wa = get_whatsapp()
    msg = _build_debtor_payment_request(
        debtor_name=debtor["name"],
        merchant_business=merchant.get("business_name", "your store"),
        amount_naira=params["amount_naira"],
        goods=debt.get("goods_description", ""),
        account_number=acct["account_number"],
        bank=acct.get("bank_name", "Nomba"),
        lang=merchant.get("preferred_lang", "pidgin"),
    )
    await wa.send_sms(debtor["phone_normalized"], msg)

    return {
        "account_number": acct["account_number"],
        "bank_name": acct.get("bank_name", "Nomba"),
        "debt_id": params["debt_id"],
    }


def _build_debtor_payment_request(
    *, debtor_name, merchant_business, amount_naira, goods,
    account_number, bank, lang,
) -> str:
    naira = f"₦{float(amount_naira):,.0f}"
    # keep it bilingual-ish for clarity; debtors may not match merchant's lang
    return (
        f"Hello {debtor_name}, {merchant_business} send dis message. "
        f"You get credit of {naira} for {goods}. "
        f"Abeg pay into: {account_number} ({bank}). "
        f"Dis account go expire soon. Once you pay, your balance go clear. 🙏"
    )


# ---------------------------------------------------------------- password

def _hash_password(plain: str) -> str:
    """SHA-256 hash of the password. Simple for hackathon; use bcrypt in prod."""
    import hashlib
    return hashlib.sha256(plain.encode()).hexdigest()


async def verify_password(phone: str, plain: str) -> dict:
    """Verify a merchant's phone + password. Used by the wallet login."""
    from app.utils import normalize_phone
    db = get_db()
    norm = normalize_phone(phone)
    hashed = _hash_password(plain)
    m = await db.merchants.find_one({"phone": norm, "password_hash": hashed})
    if not m:
        return {"valid": False}
    return {"valid": True, "merchant_id": str(m["_id"]), "name": m.get("name"),
            "business_name": m.get("business_name")}


# ================================================================== MARK PAID

async def propose_mark_paid(
    merchant_id: str,
    debt_id: str,
    amount_naira: float,
    source: str = "manual",
) -> dict:
    """Propose marking a debt (partially or fully) paid via a manual entry.

    Used when the merchant says 'Alhaji has paid me 5,000 in cash' and we need
    to record it on the ledger. NOT the same as the Nomba webhook path — this
    is a manual ledger correction and the merchant must confirm.
    """
    db = get_db()
    debt = await db.debts.find_one({"_id": _oid(debt_id)})
    if not debt:
        return {"error": "debt_not_found"}
    owed = debt["amount_kobo"] - debt.get("paid_kobo", 0)
    kobo = naira_to_kobo(amount_naira)
    if kobo > owed:
        return {
            "error": "over_payment",
            "detail": f"Amount exceeds outstanding balance of {fmt_naira(owed)}.",
        }
    return {
        "action_type": "mark_paid",
        "token": _token(),
        "params": {
            "merchant_id": merchant_id,
            "debt_id": debt_id,
            "amount_kobo": kobo,
            "source": source,
            "is_full": kobo == owed,
        },
        "summary": (
            f"Mark {fmt_naira(kobo)} as paid on this debt "
            f"({'full' if kobo == owed else 'partial'})."
        ),
    }


async def commit_mark_paid(params: dict) -> dict:
    """Apply a manual payment to a debt. Does NOT credit wallet (no money moved)."""
    db = get_db()
    debt = await db.debts.find_one({"_id": _oid(params["debt_id"])})
    if not debt:
        return {"error": "debt_not_found"}

    new_paid = debt.get("paid_kobo", 0) + params["amount_kobo"]
    update = {"$set": {"paid_kobo": new_paid}}
    fully_paid = new_paid >= debt["amount_kobo"]
    if fully_paid:
        update["$set"]["status"] = "PAID"
        update["$set"]["settled_at"] = datetime.now(timezone.utc)
    else:
        update["$set"]["status"] = "PARTIAL"
    await db.debts.update_one({"_id": debt["_id"]}, update)
    # if fully paid, the temp account should stop collecting
    if fully_paid:
        await _disable_collection_account(params["debt_id"])
    return {
        "debt_id": params["debt_id"],
        "new_paid": fmt_naira(new_paid),
        "balance": fmt_naira(max(0, debt["amount_kobo"] - new_paid)),
        "status": update["$set"]["status"],
    }


# ============================================================ COMPLETE A DRAFT
#
# When the merchant returns with the missing info ("his number is 0809..."), the
# agent finds the most relevant open DRAFT and promotes it DRAFT → PENDING. This
# is auto-committed — completing a draft is not a destructive write.

async def propose_complete_pending_debt(
    merchant_id: str,
    debtor_phone: str,
    debtor_name: Optional[str] = None,
    draft_id: Optional[str] = None,
) -> dict:
    """Finish a DRAFT debt by supplying the missing phone (and optionally name).

    If `draft_id` is omitted, the most recent open DRAFT for this merchant is
    used (optionally narrowed by `debtor_name`). Auto-committed.
    """
    norm = normalize_phone(debtor_phone)
    if not norm:
        return {"error": "invalid_debtor_phone",
                "detail": f"Couldn't parse phone: {debtor_phone}"}

    db = get_db()
    query = {"merchant_id": merchant_id, "status": "DRAFT"}
    if draft_id:
        query["_id"] = _oid(draft_id)
    if debtor_name:
        query["draft_name"] = {"$regex": debtor_name.strip(), "$options": "i"}
    draft = await db.debts.find_one(query, sort=[("created_at", -1)])
    if not draft:
        return {"error": "no_open_draft",
                "detail": "I no see any draft wey dey wait for this customer."}

    existing_debtor = await db.debtors.find_one(
        {"merchant_id": merchant_id, "phone_normalized": norm}
    )
    name = (debtor_name or draft.get("draft_name") or "").strip() or None
    params = {
        "merchant_id": merchant_id,
        "draft_id": str(draft["_id"]),
        "debtor_name": name,
        "debtor_phone_raw": debtor_phone,
        "debtor_phone_normalized": norm,
        "existing_debtor_id": str(existing_debtor["_id"]) if existing_debtor else None,
    }
    label = existing_debtor["name"] if existing_debtor else (name or "the customer")
    summary = (
        f"Complete the draft: {fmt_naira(draft['amount_kobo'])} for {label} "
        f"({norm}) — {draft.get('goods_description', '')}."
    )
    return {
        "action_type": "complete_pending_debt",
        "token": _token(),
        "params": params,
        "summary": summary,
        "auto_commit": True,
    }


async def commit_complete_pending_debt(params: dict) -> dict:
    """Promote a DRAFT to PENDING: create/find the debtor, attach, complete."""
    db = get_db()
    draft = await db.debts.find_one({"_id": _oid(params["draft_id"])})
    if not draft:
        return {"error": "draft_not_found"}
    if draft.get("status") != "DRAFT":
        return {"error": "not_a_draft", "detail": "That record no be draft again."}

    # find-or-create the debtor
    debtor_id = params.get("existing_debtor_id")
    if not debtor_id:
        result = await db.debtors.insert_one({
            "merchant_id": params["merchant_id"],
            "name": params["debtor_name"],
            "phone": params["debtor_phone_raw"],
            "phone_normalized": params["debtor_phone_normalized"],
            "created_at": datetime.now(timezone.utc),
        })
        debtor_id = str(result.inserted_id)

    now = datetime.now(timezone.utc)
    await db.debts.update_one(
        {"_id": draft["_id"]},
        {"$set": {
            "debtor_id": debtor_id,
            "status": "PENDING",
            "completed_at": now,
            "missing_fields": [],
        }},
    )
    return {
        "debt_id": str(draft["_id"]),
        "reference": draft["reference"],
        "debtor_id": debtor_id,
        "amount": fmt_naira(draft["amount_kobo"]),
        "debtor_name": params["debtor_name"],
        "status": "PENDING",
        "auto_reply": (
            f"Done ✅ Don complete am — {fmt_naira(draft['amount_kobo'])} for "
            f"{params['debtor_name']}. You wan make I send account make e pay?"
        ),
    }


# ============================================================ EDIT / DELETE
#
# These are MAJOR mutations — the merchant is changing or removing a record.
# They MUST be confirmed (no auto_commit). The agent proposes; the merchant
# says yes; the graph commits.

async def propose_edit_debt(
    merchant_id: str,
    debt_id: str,
    amount_naira: Optional[float] = None,
    goods_description: Optional[str] = None,
    due_date: Optional[str] = None,
    debtor_name: Optional[str] = None,
) -> dict:
    """Propose editing fields of an existing debt. Only provided fields change.

    Major change → must be confirmed. If `debt_id` is unknown, the agent should
    resolve it first via list_recent_debts / find_debtors_by_name and disambiguate.
    """
    db = get_db()
    debt = await db.debts.find_one({"_id": _oid(debt_id), "merchant_id": merchant_id})
    if not debt:
        return {"error": "debt_not_found"}

    changes = {}
    change_lines = []
    if amount_naira is not None:
        changes["amount_kobo"] = naira_to_kobo(amount_naira)
        change_lines.append(f"amount → {fmt_naira(changes['amount_kobo'])}")
    if goods_description is not None:
        changes["goods_description"] = goods_description.strip()
        change_lines.append(f"goods → {changes['goods_description']}")
    if due_date is not None:
        changes["due_date"] = due_date
        change_lines.append(f"due date → {due_date}")
    if debtor_name is not None:
        # rename the debtor on file (draft or attached)
        changes["_debtor_name"] = debtor_name.strip()
        change_lines.append(f"name → {debtor_name.strip()}")

    if not changes:
        return {"error": "no_changes", "detail": "Nothing was given to change."}

    params = {"merchant_id": merchant_id, "debt_id": debt_id, "changes": changes}
    summary = (
        f"Change this debt ({fmt_naira(debt['amount_kobo'])} — "
        f"{debt.get('goods_description', '')}): " + "; ".join(change_lines) + "."
    )
    return {"action_type": "edit_debt", "token": _token(), "params": params, "summary": summary}


async def commit_edit_debt(params: dict) -> dict:
    db = get_db()
    debt = await db.debts.find_one({"_id": _oid(params["debt_id"])})
    if not debt:
        return {"error": "debt_not_found"}

    changes = dict(params["changes"])
    name = changes.pop("_debtor_name", None)

    # rename attached debtor (or the draft name if still DRAFT)
    if name:
        if debt.get("status") == "DRAFT":
            changes["draft_name"] = name
        elif debt.get("debtor_id"):
            await db.debtors.update_one(
                {"_id": _oid(debt["debtor_id"])},
                {"$set": {"name": name}},
            )

    if changes:
        changes["updated_at"] = datetime.now(timezone.utc)
        await db.debts.update_one({"_id": debt["_id"]}, {"$set": changes})
    return {"debt_id": params["debt_id"], "applied": list(params["changes"].keys())}


async def propose_delete_debt(
    merchant_id: str,
    debt_id: str,
) -> dict:
    """Propose cancelling (soft-delete) a debt. Major change → must be confirmed.

    We CANCEL rather than hard-delete so the audit trail of the sale is preserved.
    """
    db = get_db()
    debt = await db.debts.find_one({"_id": _oid(debt_id), "merchant_id": merchant_id})
    if not debt:
        return {"error": "debt_not_found"}
    if debt.get("status") in ("CANCELLED", "PAID"):
        return {"error": "cannot_delete",
                "detail": f"This debt is already {debt['status']}."}

    label = debt.get("goods_description", "")
    summary = (
        f"Cancel this debt: {fmt_naira(debt['amount_kobo'])}"
        + (f" — {label}" if label else "") + "?"
    )
    return {
        "action_type": "delete_debt",
        "token": _token(),
        "params": {
            "merchant_id": merchant_id,
            "debt_id": debt_id,
            "previous_status": debt.get("status"),
        },
        "summary": summary,
    }


async def commit_delete_debt(params: dict) -> dict:
    db = get_db()
    now = datetime.now(timezone.utc)
    update = {"$set": {"status": "CANCELLED", "cancelled_at": now}}
    # a cancelled debt must not keep collecting money
    await db.debts.update_one({"_id": _oid(params["debt_id"])}, update)
    await _disable_collection_account(params["debt_id"])
    return {"debt_id": params["debt_id"], "status": "CANCELLED"}


# ============================================================ collection account helpers

async def _disable_collection_account(debt_id: str) -> bool:
    """Flip a debt's collection account is_active → False. Idempotent.

    Called whenever a debt is settled (paid) or cancelled, so a stale temp VA
    can't keep accepting money for a closed debt.
    """
    db = get_db()
    from bson import ObjectId
    if not ObjectId.is_valid(debt_id):
        return False
    debt = await db.debts.find_one({"_id": ObjectId(debt_id)})
    if not debt or not debt.get("collection_account"):
        return False
    if not debt["collection_account"].get("is_active"):
        return False
    closed_at = datetime.now(timezone.utc)
    await db.debts.update_one(
        {"_id": debt["_id"]},
        {"$set": {"collection_account.is_active": False,
                  "collection_account.closed_at": closed_at}},
    )
    await db.virtual_accounts.update_many(
        {"debt_id": debt_id, "is_active": True},
        {"$set": {"is_active": False, "closed_at": closed_at}},
    )
    return True


# ============================================================ REMINDER PREFERENCE

async def propose_set_reminder_preference(
    merchant_id: str,
    reminders_enabled: bool,
) -> dict:
    """Turn payment reminders on/off. Merchant-controlled, auto-committed."""
    state = "ON" if reminders_enabled else "OFF"
    return {
        "action_type": "set_reminder_preference",
        "token": _token(),
        "params": {"merchant_id": merchant_id, "reminders_enabled": reminders_enabled},
        "summary": f"Turn reminders {state}.",
        "auto_commit": True,
    }


async def commit_set_reminder_preference(params: dict) -> dict:
    db = get_db()
    await db.merchants.update_one(
        {"_id": _oid(params["merchant_id"])},
        {"$set": {"reminders_enabled": params["reminders_enabled"],
                  "updated_at": datetime.now(timezone.utc)}},
    )
    return {"merchant_id": params["merchant_id"],
            "reminders_enabled": params["reminders_enabled"]}


# ============================================================ ACTION REGISTRY

# Maps action_type -> commit function. The graph uses this to execute a
# confirmed pending_action without the LLM ever seeing the commit_* names.
COMMIT_REGISTRY = {
    "onboard_merchant":            commit_onboard_merchant,
    "record_debt":                 commit_record_debt,
    "record_debt_draft":           commit_record_debt_draft,
    "complete_pending_debt":       commit_complete_pending_debt,
    "create_collection_account":   commit_create_collection_account,
    "mark_paid":                   commit_mark_paid,
    "edit_debt":                   commit_edit_debt,
    "delete_debt":                 commit_delete_debt,
    "set_reminder_preference":     commit_set_reminder_preference,
}


async def commit_pending_action(pending: dict) -> dict:
    """Execute a confirmed pending_action by dispatching to its commit_* fn."""
    action_type = pending["action_type"]
    fn = COMMIT_REGISTRY.get(action_type)
    if not fn:
        return {"error": "unknown_action", "action_type": action_type}
    return await fn(pending["params"])
