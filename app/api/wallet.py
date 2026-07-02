"""Web wallet — auth + dashboard data + withdrawal.

Routes:
  Pages:   GET  /wallet/login    GET /wallet/dashboard    GET /wallet/logout
  Auth:    POST /wallet/login
  Data:    GET  /wallet/api/summary
           GET  /wallet/api/transactions
           GET  /wallet/api/debts
           POST /wallet/api/withdraw

Auth = signed cookie session (itsdangerous). Simple, no frontend framework.
The merchant logs in with phone + the password the agent generated at onboarding.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db import get_db
from app.agent.tools_write import verify_password
from app.services.nomba import nomba
from app.utils import normalize_phone, kobo_to_naira, fmt_naira, naira_to_kobo

router = APIRouter(prefix="/wallet")
log = logging.getLogger("paywise.wallet")

# Templates live in app/templates/wallet
templates = Jinja2Templates(directory="app/templates")

# Signed-cookie session helper
from itsdangerous import URLSafeSerializer, BadSignature
_session = URLSafeSerializer(settings.app_base_url, salt="paywise-session")


def _make_session_cookie(merchant_id: str) -> str:
    return _session.dumps({"mid": merchant_id})


def _read_session_cookie(cookie_value: str | None) -> str | None:
    if not cookie_value:
        return None
    try:
        return _session.loads(cookie_value).get("mid")
    except BadSignature:
        return None


async def _current_merchant(request: Request):
    """Return the merchant doc for the logged-in user, or None."""
    mid = _read_session_cookie(request.cookies.get("pw_session"))
    if not mid:
        return None
    db = get_db()
    return await db.merchants.find_one({"_id": ObjectId(mid)})


# ================================================================= PAGES

@router.get("")
@router.get("/")
async def wallet_root(request: Request):
    """Shortcut: logged in → dashboard, otherwise → login."""
    if await _current_merchant(request):
        return RedirectResponse("/wallet/dashboard", status_code=303)
    return RedirectResponse("/wallet/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # already logged in? bounce to dashboard
    if await _current_merchant(request):
        return RedirectResponse("/wallet/dashboard", status_code=303)
    return templates.TemplateResponse("wallet/login.html", {
        "request": request, "error": None,
    })


@router.post("/login")
async def login_submit(request: Request, phone: str = Form(...), password: str = Form(...)):
    result = await verify_password(phone, password)
    if not result.get("valid"):
        return templates.TemplateResponse("wallet/login.html", {
            "request": request, "error": "Wrong phone or password.",
        }, status_code=401)
    resp = RedirectResponse("/wallet/dashboard", status_code=303)
    resp.set_cookie("pw_session", _make_session_cookie(result["merchant_id"]),
                    httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/wallet/login", status_code=303)
    resp.delete_cookie("pw_session")
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    merchant = await _current_merchant(request)
    if not merchant:
        return RedirectResponse("/wallet/login", status_code=303)
    return templates.TemplateResponse("wallet/dashboard.html", {
        "request": request,
        "merchant": merchant,
        "wallet_url": settings.app_base_url,
    })


# ================================================================= API

@router.get("/api/summary")
async def api_summary(request: Request):
    merchant = await _current_merchant(request)
    if not merchant:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db = get_db()

    outstanding = 0
    async for d in db.debts.find({"merchant_id": str(merchant["_id"]),
                                  "status": {"$in": ["PENDING", "PARTIAL"]}}):
        outstanding += d["amount_kobo"] - d.get("paid_kobo", 0)

    paid_count = await db.transactions.count_documents({"merchant_id": str(merchant["_id"]), "status": "SUCCESS"})
    open_debts = await db.debts.count_documents({"merchant_id": str(merchant["_id"]),
                                                 "status": {"$in": ["PENDING", "PARTIAL"]}})

    return {
        "name": merchant.get("name"),
        "business_name": merchant.get("business_name"),
        "balance": fmt_naira(merchant.get("balance_kobo", 0)),
        "balance_kobo": merchant.get("balance_kobo", 0),
        "outstanding": fmt_naira(outstanding),
        "account_number": merchant.get("master_account_number"),
        "paid_count": paid_count,
        "open_debts": open_debts,
    }


@router.get("/api/transactions")
async def api_transactions(request: Request):
    merchant = await _current_merchant(request)
    if not merchant:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db = get_db()
    out = []
    cursor = db.transactions.find({"merchant_id": str(merchant["_id"]),
                                   "status": "SUCCESS"}).sort("created_at", -1).limit(50)
    async for t in cursor:
        debt = await db.debts.find_one({"_id": ObjectId(t["debt_id"])})
        debtor = await db.debtors.find_one({"_id": ObjectId(debt["debtor_id"])}) if debt else None
        out.append({
            "amount": fmt_naira(t["amount_kobo"]),
            "debtor": (debtor or {}).get("name", "—"),
            "goods": (debt or {}).get("goods_description", ""),
            "date": t["created_at"].strftime("%d %b %Y, %I:%M %p"),
            "reference": t["reference"][:8],
        })
    return {"transactions": out}


@router.get("/api/debts")
async def api_debts(request: Request):
    merchant = await _current_merchant(request)
    if not merchant:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db = get_db()
    out = []
    cursor = db.debts.find({"merchant_id": str(merchant["_id"])}).sort("created_at", -1).limit(50)
    async for d in cursor:
        debtor = await db.debtors.find_one({"_id": ObjectId(d["debtor_id"])})
        out.append({
            "reference": d["reference"][:8],
            "debtor": (debtor or {}).get("name", "—"),
            "goods": d.get("goods_description", ""),
            "amount": fmt_naira(d["amount_kobo"]),
            "paid": fmt_naira(d.get("paid_kobo", 0)),
            "balance": fmt_naira(d["amount_kobo"] - d.get("paid_kobo", 0)),
            "status": d.get("status"),
            "due_date": str(d.get("due_date") or ""),
            "date": d["created_at"].strftime("%d %b %Y"),
        })
    return {"debts": out}


@router.post("/api/withdraw")
async def api_withdraw(request: Request,
                       amount_naira: float = Form(...),
                       bank_code: str = Form(...),
                       account_number: str = Form(...),
                       account_name: str = Form(...)):
    merchant = await _current_merchant(request)
    if not merchant:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    db = get_db()
    amount_kobo = naira_to_kobo(amount_naira)
    if amount_kobo <= 0:
        return JSONResponse({"error": "Amount must be greater than zero."}, status_code=400)
    if amount_kobo > merchant.get("balance_kobo", 0):
        return JSONResponse({"error": "Insufficient wallet balance."}, status_code=400)

    # insert a PENDING withdrawal first (idempotency anchor)
    ref = secrets_token()
    insert = await db.withdrawals.insert_one({
        "reference": ref,
        "merchant_id": str(merchant["_id"]),
        "amount_kobo": amount_kobo,
        "destination_bank_code": bank_code,
        "destination_account_number": account_number,
        "destination_account_name": account_name,
        "status": "PENDING",
        "created_at": datetime.now(timezone.utc),
    })

    # call Nomba transfer
    try:
        result = await nomba.transfer(
            bank_code=bank_code,
            account_number=account_number,
            amount_naira=amount_naira,
            reference=ref,
        )
    except Exception as e:
        await db.withdrawals.update_one({"_id": insert.inserted_id},
                                        {"$set": {"status": "FAILED"}})
        return JSONResponse({"error": f"Transfer failed: {e}"}, status_code=502)

    # on success: debit wallet + mark withdrawal SUCCESS
    async with await _client().start_session() as session:
        async with session.start_transaction():
            await db.merchants.update_one(
                {"_id": merchant["_id"]},
                {"$inc": {"balance_kobo": -amount_kobo}},
                session=session,
            )
            await db.withdrawals.update_one(
                {"_id": insert.inserted_id},
                {"$set": {"status": "SUCCESS", "nomba_transfer_id": result.get("nomba_transfer_id")}},
                session=session,
            )
            await session.commit_transaction()

    return {"status": "SUCCESS", "reference": ref,
            "nomba_transfer_id": result.get("nomba_transfer_id")}


# ---- tiny helpers (kept local to avoid circular imports) ----

def secrets_token() -> str:
    import secrets as _s
    return _s.token_urlsafe(12)


def _client():
    from app.db import client
    return client
