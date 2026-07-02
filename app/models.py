"""Pydantic models = the canonical document shapes for MongoDB.

These are NOT an ORM (Mongo has no schema), so they serve three jobs:
  1. Document for the team what each collection holds.
  2. Validation at the boundaries (we only persist dicts that match).
  3. Type hints for the agent tools.

MONEY RULE: every naira amount is stored as integer KOBO (naira * 100) so we
never do float arithmetic on money. We convert to/from Naira only at the Nomba
boundary, which speaks Naira decimals.
"""
from __future__ import annotations

import uuid
from datetime import datetime, date
from enum import Enum
from typing import Optional, Literal

from pydantic import BaseModel, Field


def gen_ref() -> str:
    """Opaque, URL-safe reference used as the Nomba accountRef / money join key."""
    return uuid.uuid4().hex


# ---- enums -------------------------------------------------------------

class SupportedLang(str, Enum):
    PIDGIN = "pidgin"
    YORUBA = "yoruba"
    IGBO = "igbo"
    HAUSA = "hausu"
    ENGLISH = "english"


class DebtStatus(str, Enum):
    DRAFT = "DRAFT"          # incomplete (e.g. missing debtor phone) — saved, not yet collectable
    PENDING = "PENDING"      # recorded, awaiting payment
    PARTIAL = "PARTIAL"      # some money in, balance remains
    PAID = "PAID"
    EXPIRED = "EXPIRED"      # collection account expired unpaid
    CANCELLED = "CANCELLED"  # merchant voided it


class TxnStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


# ---- embedded sub-docs -------------------------------------------------

class CollectionAccount(BaseModel):
    """A temporary Nomba virtual account, unique to ONE debt collection.

    We generate one of these per debt so that when money lands, the webhook's
    aliasAccountReference maps back to exactly one debt — no guessing.
    """
    account_ref: str                 # == debt.reference (the join key)
    account_number: str              # NUBAN, e.g. 0123456789
    bank_name: str
    bank_code: Optional[str] = None
    expected_amount_kobo: int
    currency: str = "NGN"
    is_active: bool = True
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---- top-level collections --------------------------------------------

class Merchant(BaseModel):
    """A shop owner who chats with PayWise on WhatsApp."""
    # We use string _id everywhere (not ObjectId) for clean JSON/agent handling.
    id_: str = Field(alias="_id")  # maps Mongo's "_id" → Python id_
    public_id: str = Field(default_factory=gen_ref)
    phone: str                       # E.164, e.g. +2348012345678 — UNIQUE
    name: str
    business_name: str
    preferred_lang: SupportedLang = SupportedLang.PIDGIN

    # The merchant's "wallet" — running balance of settled debt money.
    balance_kobo: int = 0
    # The master virtual account we create at onboarding (where temp VAs settle into).
    master_account_number: Optional[str] = None
    master_account_ref: Optional[str] = None

    # Reminders (payment due nudges, draft follow-ups) are ON by default. The
    # merchant can turn them off conversationally ("don't remind me").
    reminders_enabled: bool = True

    onboarded: bool = False          # flips True once onboarding completes
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Debtor(BaseModel):
    """A customer who buys on credit from a specific merchant."""
    id_: str = Field(alias="_id")  # maps Mongo's "_id" → Python id_
    merchant_id: str
    name: str
    phone: str                       # raw as spoken
    phone_normalized: str            # E.164 — UNIQUE per (merchant_id, phone_normalized)
    is_on_whatsapp: Optional[bool] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Debt(BaseModel):
    """A single credit sale: goods taken now, money owed later."""
    id_: str = Field(alias="_id")  # maps Mongo's "_id" → Python id_
    reference: str = Field(default_factory=gen_ref)   # UNIQUE — the money join key
    merchant_id: str
    debtor_id: Optional[str] = None        # None while DRAFT (no debtor yet)
    amount_kobo: int
    paid_kobo: int = 0
    goods_description: str
    due_date: Optional[date] = None
    status: DebtStatus = DebtStatus.PENDING
    collection_account: Optional[CollectionAccount] = None
    raw_voice_transcript: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    settled_at: Optional[datetime] = None

    # ---- DRAFT support ----
    # While status == DRAFT these capture everything we DO know, so the sale is
    # never lost even if the merchant walks away mid-sentence. `missing_fields`
    # lists the keys still required to promote to PENDING.
    draft_name: Optional[str] = None
    draft_phone_raw: Optional[str] = None
    missing_fields: Optional[list[str]] = None
    completed_at: Optional[datetime] = None   # set when promoted DRAFT → PENDING


class Transaction(BaseModel):
    """An inbound payment on a collection account (idempotency on `reference`)."""
    id_: str = Field(alias="_id")  # maps Mongo's "_id" → Python id_
    reference: str                   # Nomba transactionId — UNIQUE
    debt_id: str
    merchant_id: str
    amount_kobo: int
    currency: str = "NGN"
    status: TxnStatus = TxnStatus.PENDING
    raw_payload: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Withdrawal(BaseModel):
    """Merchant cashing out wallet balance to a commercial bank."""
    id_: str = Field(alias="_id")  # maps Mongo's "_id" → Python id_
    reference: str = Field(default_factory=gen_ref)
    merchant_id: str
    amount_kobo: int
    destination_bank_code: str
    destination_account_number: str
    destination_account_name: str
    nomba_transfer_id: Optional[str] = None   # UNIQUE (sparse) — idempotency
    status: TxnStatus = TxnStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
