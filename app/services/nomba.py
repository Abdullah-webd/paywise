"""Nomba API client.

Three responsibilities, all async via httpx:
  1. OAuth token (client_credentials) with caching + auto-refresh.
  2. Create a temporary virtual account for a single debt collection.
  3. Bank transfer (merchant withdrawal) with an idempotency key.

Plus a standalone webhook-signature verifier that mirrors Nomba's documented
algorithm EXACTLY: HMAC-SHA256 over a colon-joined string of specific payload
fields, base64-encoded, compared against the `nomba-signature` header.

NOTE on money: Nomba speaks NAIRA decimals (e.g. transactionAmount: 4000.00).
We speak integer KOBO internally. Convert at the boundary.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app.config import settings

log = logging.getLogger("paywise.nomba")


class NombaError(Exception):
    """Raised when a Nomba API call fails or returns a non-success envelope."""


class NombaClient:
    def __init__(self) -> None:
        self._base = settings.nomba_base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=30.0)
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()  # serialize token refresh

    # ---------- auth ---------------------------------------------------

    async def _get_token(self) -> str:
        """Cache the OAuth token; refresh ~60s before expiry."""
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        async with self._lock:
            # re-check inside the lock (another coroutine may have refreshed)
            if self._token and time.time() < self._token_expires_at - 60:
                return self._token

            url = f"{self._base}/v1/auth/token/issue"
            # Nomba auth (per official docs):
            #   - accountId goes in the HEADER (parent account identity)
            #   - grant_type, client_id, client_secret go in the BODY
            # All field names are snake_case. Earlier code used camelCase which
            # caused a 400 "grantType: must not be blank" / 403 errors.
            payload = {
                "grant_type": "client_credentials",
                "client_id": settings.nomba_client_id,
                "client_secret": settings.nomba_client_key,
            }
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "accountId": settings.nomba_account_id,
            }
            try:
                resp = await self._http.post(url, json=payload, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                log.error("Nomba token failed %s: %s", resp.status_code, resp.text)
                raise NombaError(
                    f"Nomba token request failed (HTTP {resp.status_code}): {resp.text}"
                ) from e
            except httpx.HTTPError as e:
                raise NombaError(f"Nomba token request failed: {e}") from e

            body = resp.json()
            data = body.get("data", body)
            # Token field is snake_case (access_token) per the working API response.
            self._token = data.get("access_token") or data.get("accessToken")
            # expires_in is in seconds (snake_case)
            self._token_expires_at = time.time() + int(data.get("expires_in", data.get("expiresIn", 3600)))
            log.info("Nomba token refreshed, expires in %ss", data.get("expires_in", data.get("expiresIn")))
            return self._token

    async def _headers(self, idempotency_key: Optional[str] = None) -> dict:
        token = await self._get_token()
        h = {
            "Authorization": f"Bearer {token}",
            "accountId": settings.nomba_account_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key:
            h["X-Idempotent-key"] = idempotency_key
        return h

    # ---------- virtual accounts --------------------------------------

    async def create_virtual_account(
        self,
        account_ref: str,
        amount_naira: float,
        expires_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Create a temporary Nomba virtual account tied to ONE debt.

        Uses the sub-account endpoint so money lands in the correct wallet.
        URL: POST /v1/accounts/virtual/{subAccountId}
        Headers: Authorization + accountId (parent)
        Body: accountRef, accountName, expectedAmount?, expiryDate?

        Args:
            account_ref: our opaque debt reference (16-64 chars) — becomes the
                         account's aliasAccountReference, so the inbound webhook
                         routes back to exactly this debt.
            amount_naira: the expected collection amount in Naira.
            expires_at:  account expiry; defaults to 48h.

        Returns:
            dict with at least: account_number, bank_name, account_ref.
        """
        if expires_at is None:
            expires_at = datetime.now(timezone.utc) + timedelta(
                hours=settings.nomba_virtual_account_ttl_hours
            )

        # Use the sub-account endpoint: the subAccountId goes in the URL path,
        # the parent accountId stays in the header (per Nomba docs).
        sub_id = settings.nomba_sub_account_id
        url = f"{self._base}/v1/accounts/virtual/{sub_id}"

        # Nomba expects: accountRef (16-64 chars), accountName, expectedAmount
        # (as string "200.00"), expiryDate ("2026-01-30 12:15:00" format).
        payload = {
            "accountName": "PayWise Collection",
            "accountRef": account_ref,
            "expectedAmount": f"{float(amount_naira):.2f}",
            "expiryDate": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        headers = await self._headers(idempotency_key=account_ref)
        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("Nomba create VA failed %s: %s", resp.status_code, resp.text)
            raise NombaError(f"create_virtual_account HTTP {resp.status_code}: {resp.text}") from e
        except httpx.HTTPError as e:
            raise NombaError(f"create_virtual_account network error: {e}") from e

        body = resp.json()
        data = body.get("data", body)
        return {
            "account_number": str(
                data.get("bankAccountNumber")
                or data.get("accountNumber")
                or data.get("virtualAccount", {}).get("accountNumber")
            ),
            "bank_name": str(
                data.get("bankName")
                or data.get("virtualAccount", {}).get("bankName")
                or "Nomba"
            ),
            "bank_code": data.get("bankCode"),
            "account_ref": account_ref,
            "expected_amount_naira": float(amount_naira),
            "expires_at": expires_at.isoformat(),
        }

    # ---------- transfers (withdrawals) -------------------------------

    async def get_banks(self) -> list[dict[str, Any]]:
        """Fetch list of supported banks and their codes.

        GET /v1/transfers/banks
        """
        url = f"{self._base}/v1/transfers/banks"
        headers = await self._headers()
        try:
            resp = await self._http.get(url, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("Nomba banks fetch failed %s: %s", resp.status_code, resp.text)
            raise NombaError(f"get_banks HTTP {resp.status_code}: {resp.text}") from e
        except httpx.HTTPError as e:
            raise NombaError(f"get_banks network error: {e}") from e

        body = resp.json()
        data = body.get("data", body)
        banks = data if isinstance(data, list) else data.get("banks", data.get("results", []))
        return [{"code": b.get("code"), "name": b.get("name")} for b in banks]

    async def lookup_bank_account(self, bank_code: str, account_number: str) -> dict[str, Any]:
        """Verify a bank account number and return the account name.

        POST /v1/transfers/bank/lookup
        """
        url = f"{self._base}/v1/transfers/bank/lookup"
        headers = await self._headers()
        payload = {"bankCode": bank_code, "accountNumber": account_number}
        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("Nomba account lookup failed %s: %s", resp.status_code, resp.text)
            raise NombaError(f"lookup_bank_account HTTP {resp.status_code}: {resp.text}") from e
        except httpx.HTTPError as e:
            raise NombaError(f"lookup_bank_account network error: {e}") from e

        body = resp.json()
        data = body.get("data", body)
        return {
            "account_name": data.get("accountName") or data.get("account_name") or "",
            "account_number": data.get("accountNumber") or account_number,
            "bank_code": bank_code,
        }

    async def wallet_transfer(
        self,
        amount_naira: float,
        reference: str,
    ) -> dict[str, Any]:
        """Transfer money from sub-account wallet to the parent account.

        POST /v2/transfers/wallet/{subAccountId}
        Moves money from sub-account wallet into the parent wallet.
        Use this as the first step before bank transfer (wallet → parent → bank).
        """
        sub_id = settings.nomba_sub_account_id
        url = f"{self._base}/v2/transfers/wallet/{sub_id}"
        payload = {
            "amount": round(float(amount_naira), 2),
            "currency": "NGN",
            "reference": reference,
        }
        headers = await self._headers(idempotency_key=reference)
        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("Nomba wallet transfer failed %s: %s", resp.status_code, resp.text)
            raise NombaError(f"wallet_transfer HTTP {resp.status_code}: {resp.text}") from e
        except httpx.HTTPError as e:
            raise NombaError(f"wallet_transfer network error: {e}") from e

        body = resp.json()
        data = body.get("data", body)
        return {
            "nomba_transfer_id": data.get("transactionId") or data.get("id"),
            "status": data.get("status"),
            "amount_naira": float(data.get("amount", amount_naira)),
            "reference": reference,
        }

    async def transfer(
        self,
        bank_code: str,
        account_number: str,
        account_name: str,
        amount_naira: float,
        reference: str,
    ) -> dict[str, Any]:
        """Move money OUT of the SUB-ACCOUNT wallet to a commercial bank account.

        POST /v2/transfers/bank/{subAccountId}
        The subAccountId is in the URL PATH, so Nomba debits ONLY from that
        sub-account's available balance — never from the shared parent pool.

        `reference` is sent as merchantTxRef + X-Idempotent-key so a retried
        withdrawal never pays out twice.
        """
        sub_id = settings.nomba_sub_account_id
        url = f"{self._base}/v2/transfers/bank/{sub_id}"
        payload = {
            "bankCode": bank_code,
            "accountNumber": account_number,
            "accountName": account_name,
            "amount": round(float(amount_naira), 2),
            "currency": "NGN",
            "merchantTxRef": reference,
        }
        headers = await self._headers(idempotency_key=reference)
        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("Nomba transfer failed %s: %s", resp.status_code, resp.text)
            raise NombaError(f"transfer HTTP {resp.status_code}: {resp.text}") from e
        except httpx.HTTPError as e:
            raise NombaError(f"transfer network error: {e}") from e

        body = resp.json()
        data = body.get("data", body)
        return {
            "nomba_transfer_id": data.get("transactionId") or data.get("id"),
            "status": data.get("status"),
            "amount_naira": float(data.get("amount", amount_naira)),
            "reference": reference,
        }

    # ---------- update / reuse virtual account --------------------

    async def update_virtual_account(
        self, account_ref: str, new_account_ref: str, amount_naira: float, account_name: str = "PayWise Collection"
    ) -> dict[str, Any]:
        """Update an existing virtual account with new reference and amount.

        PUT /v1/accounts/virtual/{identifier}
        Reuses an existing VA slot instead of creating new ones (avoids 2-VA sandbox limit).
        """
        headers = await self._headers()
        url = f"{self._base}/v1/accounts/virtual/{account_ref}"
        payload = {
            "newAccountRef": new_account_ref,
            "expectedAmount": f"{float(amount_naira):.2f}",
            "accountName": account_name,
        }
        try:
            resp = await self._http.put(url, json=payload, headers=headers)
            if resp.status_code < 400:
                body = resp.json()
                updated = body.get("data", {}).get("updated", True)
                log.info("Nomba VA %s updated -> %s (amount=%s): %s", account_ref, new_account_ref, amount_naira, updated)
                return {"updated": True, "account_ref": new_account_ref, "old_ref": account_ref}
            log.warning("Nomba update VA %s returned %s: %s", account_ref, resp.status_code, resp.text[:200])
            return {"updated": False, "status": resp.status_code, "detail": resp.text[:200]}
        except httpx.HTTPError as e:
            log.error("Nomba update VA failed: %s", e)
            return {"updated": False, "error": str(e)}

    # ---------- account balance & transactions ---------------------

    async def get_parent_balance(self) -> dict[str, Any]:
        """Fetch the live PARENT account balance (where transfers debit from).

        GET /v1/accounts/balance
        This is the real pool of money — reflects all inflows AND outflows.
        The sub-account balance only tracks virtual account collections.
        """
        url = f"{self._base}/v1/accounts/balance"
        headers = await self._headers()
        try:
            resp = await self._http.get(url, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("Nomba parent balance fetch failed %s: %s", resp.status_code, resp.text)
            raise NombaError(f"get_parent_balance HTTP {resp.status_code}: {resp.text}") from e
        except httpx.HTTPError as e:
            raise NombaError(f"get_parent_balance network error: {e}") from e

        body = resp.json()
        data = body.get("data", body)
        return {
            "balance_naira": float(data.get("amount", 0)),
            "currency": data.get("currency", "NGN"),
            "time_created": data.get("timeCreated"),
        }

    async def get_sub_account_balance(self) -> dict[str, Any]:
        """Fetch the live balance of the configured sub-account.

        GET /v1/accounts/{subAccountId}/balance
        Returns amount (Naira decimal string) and currency.
        """
        sub_id = settings.nomba_sub_account_id
        url = f"{self._base}/v1/accounts/{sub_id}/balance"
        headers = await self._headers()
        try:
            resp = await self._http.get(url, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("Nomba balance fetch failed %s: %s", resp.status_code, resp.text)
            raise NombaError(f"get_sub_account_balance HTTP {resp.status_code}: {resp.text}") from e
        except httpx.HTTPError as e:
            raise NombaError(f"get_sub_account_balance network error: {e}") from e

        body = resp.json()
        data = body.get("data", body)
        return {
            "balance_naira": float(data.get("amount", 0)),
            "currency": data.get("currency", "NGN"),
            "time_created": data.get("timeCreated"),
        }

    async def get_sub_account_transactions(self, page: int = 1, size: int = 10) -> dict[str, Any]:
        """Fetch recent transactions for the configured sub-account.

        GET /v1/transactions/accounts/{subAccountId}?page=1&size=10
        Returns a list of transactions (credits, debits, fees, etc.).
        """
        sub_id = settings.nomba_sub_account_id
        url = f"{self._base}/v1/transactions/accounts/{sub_id}?page={page}&size={size}"
        headers = await self._headers()
        try:
            resp = await self._http.get(url, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("Nomba txn fetch failed %s: %s", resp.status_code, resp.text)
            raise NombaError(f"get_sub_account_transactions HTTP {resp.status_code}: {resp.text}") from e
        except httpx.HTTPError as e:
            raise NombaError(f"get_sub_account_transactions network error: {e}") from e

        body = resp.json()
        data = body.get("data", body)
        results = data.get("results", [])
        txns = []
        for t in results:
            txns.append({
                "id": t.get("id"),
                "status": t.get("status"),
                "type": t.get("type"),
                "amount": float(t.get("amount", 0)),
                "fee": float(t.get("fixedCharge", 0)),
                "entry_type": t.get("entryType"),
                "sender_name": t.get("senderName") or t.get("ktaSenderName"),
                "sender_account": t.get("accountNumber") or t.get("ktaSenderAccountNumber"),
                "sender_bank": t.get("bankName") or t.get("ktaSenderBankCode"),
                "narration": t.get("narration"),
                "time_created": t.get("timeCreated"),
                "wallet_balance": float(t.get("walletBalance", 0)),
                "virtual_account_reference": t.get("virtualAccountReference"),
                "recipient_account_number": t.get("recipientAccountNumber"),
            })
        return {
            "transactions": txns,
            "count": len(txns),
            "cursor": data.get("cursor", ""),
        }

    # ---------- expire / delete virtual accounts -------------------

    async def expire_virtual_account(self, account_ref: str) -> dict[str, Any]:
        """Expire a virtual account via Nomba API.

        DELETE /v1/accounts/virtual/{identifier}
        The {identifier} is the account reference (not sub-account ID, not account number).
        """
        headers = await self._headers()
        url = f"{self._base}/v1/accounts/virtual/{account_ref}"
        try:
            resp = await self._http.delete(url, headers=headers)
            if resp.status_code < 400:
                body = resp.json()
                data = body.get("data", body)
                expired = data.get("expired", True)
                log.info("Nomba VA %s expired: %s", account_ref, expired)
                return {"expired": True, "account_ref": account_ref}
            log.warning("Nomba expire VA %s returned %s: %s", account_ref, resp.status_code, resp.text[:200])
            return {"expired": False, "account_ref": account_ref, "status": resp.status_code, "detail": resp.text[:200]}
        except httpx.HTTPError as e:
            log.error("Nomba expire VA %s failed: %s", account_ref, e)
            return {"expired": False, "account_ref": account_ref, "error": str(e)}

    async def close(self) -> None:
        await self._http.aclose()


# ---------------------------------------------------------------------------
# WEBHOOK SIGNATURE VERIFICATION
# ---------------------------------------------------------------------------

def _stringify(value: Any) -> str:
    """Render a payload value the way Nomba does for signing (deterministic)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def verify_nomba_webhook_signature(
    *,
    signature_header: Optional[str],
    event_type: Optional[str],
    request_id: Optional[str],
    user_id: Optional[str],
    wallet_id: Optional[str],
    transaction_id: Optional[str],
    type_: Optional[str],
    time: Optional[str],
    response_code: Optional[str],
    nomba_timestamp: Optional[str],
) -> bool:
    """Verify a Nomba webhook using the hackathon shared key.

    The signed string is colon-joined in this exact order:
        event_type : requestId : userId : walletId : transactionId
        : type : time : responseCode : nomba-timestamp

    Key: b"NombaHackathon2026" (from hackathon organisers).
    Algorithm: HMAC-SHA256 → base64, compared constant-time.
    """
    if not signature_header:
        return False

    import base64
    import hmac
    import hashlib

    KEY = b"NombaHackathon2026"

    parts = [
        event_type, request_id, user_id, wallet_id, transaction_id,
        type_, time, response_code, nomba_timestamp,
    ]
    signed_string = ":".join(_stringify(p) for p in parts)

    expected = base64.b64encode(
        hmac.new(KEY, signed_string.encode(), hashlib.sha256).digest()
    ).decode()

    return hmac.compare_digest(expected, signature_header.strip())


# shared singleton
nomba = NombaClient()
