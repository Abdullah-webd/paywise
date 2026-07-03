"""Nomba payment webhook — closes the money loop.

When a debtor pays into a temp virtual account, Nomba POSTs here. We:
  1. Verify the signature using Nomba's documented algorithm.
  2. Open a Mongo transaction.
  3. Idempotency check on the Nomba transactionId.
  4. Locate the debt via aliasAccountReference == our debt.reference.
  5. Update debt (PAID / PARTIAL) + credit merchant wallet — atomically.
  6. After commit, send WhatsApp receipts to both parties.

CRITICAL: signature verification here uses the COLON-JOINED-STRING algorithm
from Nomba's docs, NOT HMAC-over-raw-body. Field order matters.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, BackgroundTasks, Header
from fastapi.responses import JSONResponse
from bson import ObjectId

from app.db import get_db
import app.db as _db
from app.services.nomba import verify_nomba_webhook_signature
from app.services.whatsapp import get_whatsapp
from app.utils import naira_to_kobo, fmt_naira

router = APIRouter()
log = logging.getLogger("paywise.nomba_webhook")


@router.post("/webhooks/nomba")
async def nomba_webhook(
    request: Request,
    bg: BackgroundTasks,
    nomba_signature: str | None = Header(default=None),
):
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"status": "ignored", "reason": "bad_json"}, status_code=400)

    log.info("Nomba webhook: %s", json.dumps(payload)[:500])

    # 1) ------- pull the signature-relevant fields out of the envelope -------
    data = payload.get("data", payload)
    biz = data.get("business") or {}
    txn = data.get("transaction") or data  # vact_transfer nests under transaction

    event_type  = payload.get("event") or payload.get("event_type")
    request_id  = payload.get("requestId")
    user_id     = biz.get("userId") or payload.get("userId")
    wallet_id   = biz.get("walletId") or txn.get("walletId") or payload.get("walletId")
    txn_id      = txn.get("transactionId") or txn.get("id")
    type_       = txn.get("type") or payload.get("type")
    time_field  = str(txn.get("time") or payload.get("time") or "")
    resp_code   = str(txn.get("responseCode") or payload.get("responseCode") or "")
    timestamp   = str(payload.get("timestamp") or "")

    # 2) ------- verify signature -------
    ok = verify_nomba_webhook_signature(
        signature_header=nomba_signature,
        event_type=event_type, request_id=request_id, user_id=user_id,
        wallet_id=wallet_id, transaction_id=txn_id, type_=type_,
        time=time_field, response_code=resp_code, timestamp=timestamp,
    )
    if not ok:
        log.warning("Nomba signature INVALID — rejecting")
        return JSONResponse({"status": "rejected", "reason": "bad_signature"}, status_code=401)

    # 3) ------- only act on a successful virtual-account credit -------
    if type_ != "vact_transfer":
        return JSONResponse({"status": "ignored", "reason": f"type={type_}"})
    if resp_code not in ("00", "0", "") and resp_code:
        # non-empty non-zero response code = failure
        if resp_code != "00":
            return JSONResponse({"status": "ignored", "reason": f"resp_code={resp_code}"})

    alias_ref = txn.get("aliasAccountReference") or txn.get("accountRef")
    amount_naira = txn.get("amount") or txn.get("transactionAmount") or 0
    amount_kobo = naira_to_kobo(amount_naira)
    txn_ref = txn_id

    if not alias_ref or not txn_ref:
        log.error("missing alias_ref or txn_id in payload")
        return JSONResponse({"status": "ignored", "reason": "missing_refs"})

    # 4) ------- settle atomically -------
    settled = await _settle(alias_ref, txn_ref, amount_kobo, payload)
    if settled and settled.get("debt_id"):
        bg.add_task(_send_receipts, settled["debt_id"], settled["fully_paid"])
    return JSONResponse({"status": "processed", **{k: v for k, v in settled.items() if k != "raw"}})


async def _settle(alias_ref: str, txn_ref: str, amount_kobo: int, raw: dict) -> dict:
    """All money mutations happen inside ONE Mongo transaction."""
    db = get_db()

    async with await _db.client.start_session() as session:
        async with session.start_transaction():
            # idempotency: bail if we've already settled this Nomba txn
            existing = await db.transactions.find_one(
                {"reference": txn_ref, "status": "SUCCESS"}, session=session
            )
            if existing:
                await session.abort_transaction()
                log.info("duplicate txn %s — skipping", txn_ref)
                return {"status": "duplicate"}

            debt = await db.debts.find_one({"reference": alias_ref}, session=session)
            if not debt:
                await session.abort_transaction()
                log.error("no debt for alias_ref %s", alias_ref)
                return {"status": "orphan"}

            # Guard: reject if debt is already PAID or CANCELLED
            debt_status = debt.get("status", "")
            if debt_status in ("PAID", "CANCELLED", "EXPIRED"):
                await session.abort_transaction()
                log.warning("rejecting payment on %s debt %s (already %s, amount_kobo=%s)",
                            debt_status, alias_ref, amount_kobo)
                return {"status": "rejected", "reason": f"debt already {debt_status}"}

            # Guard: reject if collection account is disabled
            ca = debt.get("collection_account") or {}
            if not ca.get("is_active", False):
                await session.abort_transaction()
                log.warning("rejecting payment on inactive collection account for debt %s (amount_kobo=%s)",
                            alias_ref, amount_kobo)
                return {"status": "rejected", "reason": "collection account no longer active"}

            # Guard: reject if payment would exceed the outstanding balance
            outstanding = debt["amount_kobo"] - debt.get("paid_kobo", 0)
            if amount_kobo > outstanding:
                await session.abort_transaction()
                log.warning("rejecting over-payment on debt %s: incoming=%s but outstanding=%s",
                            alias_ref, amount_kobo, outstanding)
                return {"status": "rejected",
                        "reason": f"payment N{amount_kobo/100:,.0f} exceeds outstanding N{outstanding/100:,.0f}"}

            # insert transaction record
            await db.transactions.insert_one({
                "reference": txn_ref,
                "debt_id": str(debt["_id"]),
                "merchant_id": debt["merchant_id"],
                "amount_kobo": amount_kobo,
                "currency": "NGN",
                "status": "SUCCESS",
                "raw_payload": json.dumps(raw, default=str),
                "created_at": datetime.now(timezone.utc),
            }, session=session)

            # update debt ledger
            new_paid = debt.get("paid_kobo", 0) + amount_kobo
            fully_paid = new_paid >= debt["amount_kobo"]
            update = {"$set": {"paid_kobo": new_paid}}
            if fully_paid:
                update["$set"]["status"] = "PAID"
                update["$set"]["settled_at"] = datetime.now(timezone.utc)
                # a settled debt's temp account must stop collecting
                update["$set"]["collection_account.is_active"] = False
            else:
                update["$set"]["status"] = "PARTIAL"
            await db.debts.update_one({"_id": debt["_id"]}, update, session=session)
            # mirror is_active onto the virtual_accounts row too
            if fully_paid:
                await db.virtual_accounts.update_many(
                    {"debt_id": str(debt["_id"]), "is_active": True},
                    {"$set": {"is_active": False, "closed_at": datetime.now(timezone.utc)}},
                    session=session,
                )

            # credit merchant wallet — same transaction, always
            await db.merchants.update_one(
                {"_id": ObjectId(debt["merchant_id"])},
                {"$inc": {"balance_kobo": amount_kobo}},
                session=session,
            )
            await session.commit_transaction()

            log.info("settled debt %s (+%s) → merchant %s",
                     str(debt["_id"]), fmt_naira(amount_kobo), debt["merchant_id"])
            return {
                "debt_id": str(debt["_id"]),
                "merchant_id": debt["merchant_id"],
                "amount": fmt_naira(amount_kobo),
                "fully_paid": fully_paid,
            }


async def _send_receipts(debt_id: str, fully_paid: bool) -> None:
    """Send polite WhatsApp receipts — only after commit."""
    db = get_db()
    wa = get_whatsapp()
    debt = await db.debts.find_one({"_id": ObjectId(debt_id)})
    if not debt:
        return
    debtor = await db.debtors.find_one({"_id": ObjectId(debt["debtor_id"])})
    merchant = await db.merchants.find_one({"_id": ObjectId(debt["merchant_id"])})
    if not debtor or not merchant:
        return

    amt = fmt_naira(debt.get("paid_kobo", 0))

    # debtor receipt
    if fully_paid:
        debtor_msg = (f"✅ Thank you, {debtor['name']}! We don receive {amt} "
                      f"for {debt.get('goods_description', '')}. "
                      f"Your balance na ₦0. We appreciate you. 🙏")
    else:
        remaining = fmt_naira(debt["amount_kobo"] - debt.get("paid_kobo", 0))
        debtor_msg = (f"💵 We don receive {amt}. Remaining balance: {remaining}. "
                      f"Thank you, abeg pay the rest soon.")

    # merchant receipt
    if fully_paid:
        merchant_msg = (f"💰 Payment alert!\n\n{debtor['name']} don pay {amt} "
                        f"in full for {debt.get('goods_description', '')}. ✅\n"
                        f"Your PayWise wallet don credit.")
    else:
        merchant_msg = (f"💵 {debtor['name']} pay part — {amt}. "
                        f"Balance remaining: "
                        f"{fmt_naira(debt['amount_kobo'] - debt.get('paid_kobo', 0))}. "
                        f"Wallet don credit with {amt}.")

    try:
        await wa.send_sms(debtor.get("phone_normalized", debtor.get("phone", "")), debtor_msg)
    except Exception:
        log.exception("debtor receipt failed")
    try:
        await wa.send_text(merchant.get("phone", ""), merchant_msg)
    except Exception:
        log.exception("merchant receipt failed")



@router.post("/webhooks/nomba/test-settle")
async def nomba_test_webhook(request: Request, bg: BackgroundTasks):
    """DEV-ONLY: simulate a Nomba payment webhook without signature verification.

    Accepts the exact same payload format as the real webhook.
    Only works when app_env is 'development'.
    Use for testing the full payment pipeline without real money.
    """
    from app.config import settings as _s
    if not _s.is_dev:
        return JSONResponse({"status": "forbidden", "reason": "test endpoint only available in development"},
                            status_code=403)

    raw = await request.body()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"status": "ignored", "reason": "bad_json"}, status_code=400)

    log.info("Nomba TEST webhook: %s", json.dumps(payload)[:500])

    data = payload.get("data", payload)
    txn = data.get("transaction") or data
    txn_id = txn.get("transactionId") or txn.get("id")
    type_ = txn.get("type") or payload.get("type")
    resp_code = str(txn.get("responseCode") or payload.get("responseCode") or "")
    alias_ref = txn.get("aliasAccountReference") or txn.get("accountRef")
    amount_naira = txn.get("amount") or txn.get("transactionAmount") or 0
    amount_kobo = naira_to_kobo(amount_naira)

    if type_ != "vact_transfer":
        return JSONResponse({"status": "ignored", "reason": f"type={type_}"})

    if not alias_ref or not txn_id:
        log.error("TEST webhook: missing alias_ref or txn_id")
        return JSONResponse({"status": "ignored", "reason": "missing_refs"})

    try:
        settled = await _settle(alias_ref, txn_id, amount_kobo, payload)
    except Exception as e:
        log.exception("TEST webhook _settle failed")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

    if settled and settled.get("debt_id"):
        bg.add_task(_send_receipts, settled["debt_id"], settled.get("fully_paid", False))
    return JSONResponse({"status": "test_processed", **{k: v for k, v in (settled or {}).items() if k != "raw"}})