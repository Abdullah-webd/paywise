"""Binds Python functions to OpenAI tool-call JSON schemas, and executes them.

The LLM only ever sees:
  - 5 read tools (execute immediately)
  - 4 propose_* tools (return a summary + stash a pending_action)

It NEVER sees commit_*. That's the whole safety model.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.agent import tools_read, tools_write
from app.agent.state import AgentState

log = logging.getLogger("paywise.agent.tools")


# Each entry:  (async fn, OpenAI tool schema dict)
TOOL_REGISTRY: dict[str, tuple[Callable, dict]] = {}


def _register(name: str, schema: dict):
    def deco(fn):
        TOOL_REGISTRY[name] = (fn, {"type": "function", "function": schema})
        return fn
    return deco


# ---------------------------------------------------- READ tools (immediate)

_register(
    "lookup_merchant_by_phone",
    {
        "name": "lookup_merchant_by_phone",
        "description": (
            "Check if a phone number belongs to a registered PayWise merchant. "
            "Call this FIRST on every inbound message to decide onboarding vs returning user."
        ),
        "parameters": {
            "type": "object",
            "properties": {"phone": {"type": "string", "description": "The sender's phone number"}},
            "required": ["phone"],
        },
    },
)(tools_read.lookup_merchant_by_phone)

_register(
    "find_debtors_by_name",
    {
        "name": "find_debtors_by_name",
        "description": (
            "Search this merchant's debtors by name (case-insensitive, partial match). "
            "Use to disambiguate when multiple debtors share a name like 'Alhaji'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "name_query": {"type": "string", "description": "Partial or full debtor name"},
            },
            "required": ["merchant_id", "name_query"],
        },
    },
)(tools_read.find_debtors_by_name)

_register(
    "get_debtor_outstanding",
    {
        "name": "get_debtor_outstanding",
        "description": "Get the running balance owed by one debtor across all their open debts.",
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "debtor_id": {"type": "string"},
            },
            "required": ["merchant_id", "debtor_id"],
        },
    },
)(tools_read.get_debtor_outstanding)

_register(
    "list_recent_debts",
    {
        "name": "list_recent_debts",
        "description": "List a merchant's most recent debts (any status), newest first. Returns debt_id (the DB _id) and reference for each debt. ALWAYS use debt_id (NOT reference) when calling propose_create_collection_account, propose_edit_debt, propose_delete_debt, or propose_mark_paid.",
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["merchant_id"],
        },
    },
)(tools_read.list_recent_debts)

_register(
    "who_owes_me",
    {
        "name": "who_owes_me",
        "description": (
            "Get a per-debtor breakdown of WHO owes the merchant money and HOW "
            "MUCH each person owes (already summed across multiple debts). "
            "Use this when the merchant asks 'how much is everyone owing me?', "
            "'who dey owe me?', 'list my debtors', or anything asking about "
            "outstanding balances across ALL debtors. DO NOT try to sum "
            "list_recent_debts yourself — call this instead, it does the math."
        ),
        "parameters": {
            "type": "object",
            "properties": {"merchant_id": {"type": "string"}},
            "required": ["merchant_id"],
        },
    },
)(tools_read.who_owes_me)

_register(
    "list_drafts",
    {
        "name": "list_drafts",
        "description": (
            "List all open DRAFT debts (incomplete sales waiting for info like a "
            "phone number). Use when the merchant asks 'what drafts do I have?', "
            "'any unfinished records?', or to find a draft to complete when a "
            "phone number arrives. Also use at the start of a conversation to "
            "remind the merchant about pending drafts."
        ),
        "parameters": {
            "type": "object",
            "properties": {"merchant_id": {"type": "string"}},
            "required": ["merchant_id"],
        },
    },
)(tools_read.list_drafts)

_register(
    "get_wallet_summary",
    {
        "name": "get_wallet_summary",
        "description": "Get the merchant's wallet balance and total outstanding owed to them.",
        "parameters": {
            "type": "object",
            "properties": {"merchant_id": {"type": "string"}},
            "required": ["merchant_id"],
        },
    },
)(tools_read.get_wallet_summary)

_register(
    "get_merchant_login_details",
    {
        "name": "get_merchant_login_details",
        "description": (
            "Retrieve the merchant's web-wallet login URL and password. "
            "Use when they ask 'how do I log in?', 'what's my password?', "
            "'where's my wallet?', or want to see their dashboard."
        ),
        "parameters": {
            "type": "object",
            "properties": {"phone": {"type": "string", "description": "The merchant's phone number"}},
            "required": ["phone"],
        },
    },
)(tools_read.get_merchant_login_details)


# ----------------------------------------------- WRITE tools (propose only)

_register(
    "propose_onboard_merchant",
    {
        "name": "propose_onboard_merchant",
        "description": (
            "Propose creating a new PayWise merchant account. Use ONLY when "
            "lookup_merchant_by_phone returns exists=false. Returns a summary "
            "to confirm with the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "name": {"type": "string"},
                "business_name": {"type": "string"},
                "preferred_lang": {
                    "type": "string",
                    "enum": ["pidgin", "yoruba", "igbo", "hausa", "english"],
                    "default": "pidgin",
                },
            },
            "required": ["phone", "name", "business_name"],
        },
    },
)(tools_write.propose_onboard_merchant)

_register(
    "propose_record_debt",
    {
        "name": "propose_record_debt",
        "description": (
            "Propose recording a NEW credit sale on the ledger. Call this as "
            "soon as you have a sale — even if the debtor's PHONE is missing. "
            "If the phone is missing the sale is AUTO-SAVED as a DRAFT (no "
            "confirmation needed) and you then ask the merchant for the phone. "
            "NEVER discard a sale or refuse it because a field is missing. "
            "If name/phone are ambiguous among existing debtors, ask first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "debtor_name": {"type": "string", "description": "Best-known name; may be omitted on a draft"},
                "debtor_phone": {"type": "string", "description": "Debtor's unique id — if omitted/invalid, a DRAFT is saved"},
                "goods_description": {"type": "string"},
                "amount_naira": {"type": "number"},
                "due_date": {"type": "string", "description": "ISO date the merchant says the debtor will pay by, optional"},
            },
            "required": ["merchant_id", "goods_description", "amount_naira"],
        },
    },
)(tools_write.propose_record_debt)

_register(
    "propose_create_collection_account",
    {
        "name": "propose_create_collection_account",
        "description": (
            "Propose generating a temporary Nomba account number for a debtor "
            "to pay a specific debt, and notifying them on WhatsApp. Use after "
            "a debt exists and the merchant wants to collect. "
            "CRITICAL: debt_id must be the debt_id field from list_recent_debts or list_drafts "
            "(a 24-char hex string), NOT the reference field (which is a 32-char UUID). "
            "If you pass the wrong ID, the request will fail with debt_not_found."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "debtor_id": {"type": "string"},
                "debt_id": {"type": "string"},
                "amount_naira": {"type": "number"},
            },
            "required": ["merchant_id", "debtor_id", "debt_id", "amount_naira"],
        },
    },
)(tools_write.propose_create_collection_account)

_register(
    "propose_mark_paid",
    {
        "name": "propose_mark_paid",
        "description": (
            "Propose marking a debt partially or fully paid (MANUAL ledger entry, "
            "e.g. customer paid cash). Does NOT move wallet money. Always confirm "
            "with the merchant before proposing. Use debt_id from list_recent_debts "
            "(NOT the reference field)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "debt_id": {"type": "string"},
                "amount_naira": {"type": "number"},
                "source": {"type": "string", "default": "manual"},
            },
            "required": ["merchant_id", "debt_id", "amount_naira"],
        },
    },
)(tools_write.propose_mark_paid)

_register(
    "propose_complete_pending_debt",
    {
        "name": "propose_complete_pending_debt",
        "description": (
            "Complete a saved DRAFT debt by supplying the missing phone number. "
            "Call this when the merchant provides the phone for a sale you "
            "previously saved as a draft. AUTO-COMMITTED (no confirmation). "
            "If the merchant mentions several customers, pass debtor_name to "
            "narrow to the right draft; else the most recent open draft is used."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "debtor_phone": {"type": "string", "description": "The phone just provided by the merchant"},
                "debtor_name": {"type": "string", "description": "Optional — narrows which draft to complete"},
                "draft_id": {"type": "string", "description": "Optional — exact draft to complete"},
            },
            "required": ["merchant_id", "debtor_phone"],
        },
    },
)(tools_write.propose_complete_pending_debt)

_register(
    "propose_edit_debt",
    {
        "name": "propose_edit_debt",
        "description": (
            "Propose changing one or more fields of an existing debt (amount, "
            "goods, due date, or debtor name). MAJOR change — always confirm "
            "with the merchant first, and resolve the exact debt_id via "
            "list_recent_debts / find_debtors_by_name before calling if unsure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "debt_id": {"type": "string"},
                "amount_naira": {"type": "number"},
                "goods_description": {"type": "string"},
                "due_date": {"type": "string", "description": "ISO date, optional"},
                "debtor_name": {"type": "string", "description": "Renames the debtor on file"},
            },
            "required": ["merchant_id", "debt_id"],
        },
    },
)(tools_write.propose_edit_debt)

_register(
    "propose_delete_debt",
    {
        "name": "propose_delete_debt",
        "description": (
            "Propose cancelling (soft-deleting) a debt the merchant wants "
            "removed. MAJOR change — always confirm with the merchant first. "
            "Resolve the exact debt_id before calling; never guess which debt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "debt_id": {"type": "string"},
            },
            "required": ["merchant_id", "debt_id"],
        },
    },
)(tools_write.propose_delete_debt)

_register(
    "propose_set_reminder_preference",
    {
        "name": "propose_set_reminder_preference",
        "description": (
            "Turn payment reminders ON or OFF for this merchant. Reminders are "
            "ON by default. Use when the merchant says 'don't remind me', "
            "'stop reminders', 'turn reminders back on', etc. AUTO-COMMITTED."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "reminders_enabled": {"type": "boolean", "description": "true=on, false=off"},
            },
            "required": ["merchant_id", "reminders_enabled"],
        },
    },
)(tools_write.propose_set_reminder_preference)

_register(
    "propose_send_reminder",
    {
        "name": "propose_send_reminder",
        "description": (
            "Propose sending a payment reminder SMS to a debtor. "
            "Use when the merchant says 'send am message', 'remind am to pay', "
            "'tell am make e pay', 'send reminder', etc. "
            "The merchant sees the message and confirms before it sends."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string"},
                "debtor_id": {"type": "string"},
                "debt_id": {"type": "string"},
            },
            "required": ["merchant_id", "debtor_id", "debt_id"],
        },
    },
)(tools_write.propose_send_reminder)


# ---------------------------------------------------- execution dispatcher

async def execute_tool(name: str, args: dict, state: AgentState) -> str:
    """Run a tool by name and return its result as a JSON string for the LLM.

    Three outcomes for a propose_* result:
      1. error      → returned as-is (nothing stashed).
      2. auto_commit → the change is non-destructive (save a DRAFT, flip a
         preference), so we commit it IMMEDIATELY. No human checkpoint.
      3. otherwise  → stash into state.pending_action and gate behind the
         merchant's yes. The model never reaches commit_* on its own.
    """
    entry = TOOL_REGISTRY.get(name)
    if not entry:
        return json.dumps({"error": f"unknown_tool: {name}"})
    fn, _ = entry
    try:
        # The LLM doesn't know the sender's real WhatsApp number or merchant
        # ObjectId, so inject them from state for the tools that need them.
        if name in ("propose_onboard_merchant", "lookup_merchant_by_phone",
                     "get_merchant_login_details"):
            real_phone = state.get("merchant_phone", "")
            if real_phone:
                args["phone"] = real_phone
        # Any tool that takes merchant_id gets the REAL one from state, never
        # whatever the LLM guessed (it often passes the name instead).
        if "merchant_id" in args:
            real_mid = state.get("merchant_id")
            if real_mid:
                args["merchant_id"] = real_mid
        result = await fn(**args)
        if name.startswith("propose_") and isinstance(result, dict) and result.get("token"):
            if result.get("auto_commit"):
                # non-destructive write — execute now, surface the committed result
                from app.agent.tools_write import commit_pending_action
                committed = await commit_pending_action(result)
                log.info("auto-committed %s: %s", result["action_type"], committed)
                result = {**result, "committed": committed}
            else:
                # major mutation — gate behind the confirmation loop
                state["pending_action"] = result
                state["awaiting_confirmation"] = True
        return json.dumps(result, default=str)
    except Exception as e:
        log.exception("tool %s failed", name)
        return json.dumps({"error": str(e)})


def schemas_for_llm() -> list[dict]:
    """The list of tool schemas we pass to OpenAI on every call."""
    return [schema for _, schema in TOOL_REGISTRY.values()]
