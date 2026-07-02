"""Graph state + the tool catalogue exposed to the LLM.

The catalogue has two tiers:
  - READ tools  → exposed directly. The model can call them whenever.
  - WRITE tools → exposed ONLY as propose_* (returns a summary + token). The
                  model literally cannot commit; the graph does that after
                  the merchant says yes. This is the structural confirmation gate.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """The state that flows between nodes.

    `messages` is the OpenAI-style conversation (system, user, assistant, tool).
    Everything else is bookkeeping for the confirmation loop.
    """
    # core conversation
    messages: Annotated[list, add_messages]

    # identity resolved at the top of every run
    merchant_id: Optional[str]
    merchant_phone: str
    merchant_lang: str               # detected per message from what the user said
    source: str                       # "voice" or "text" — drives voice-out choice

    # confirmation gate state
    pending_action: Optional[dict]   # a proposed write awaiting "yes"
    awaiting_confirmation: bool      # is the agent waiting on a yes/no?

    # delivery hints for the outbound layer
    reply_text: Optional[str]        # final text to send (short replies)
    reply_is_long: bool              # if True, send as voice note instead


# ---------------------------------------------------------------- tool registry
# Maps the tool NAME the LLM sees -> (async callable, json schema for the LLM).

READ_TOOLS: dict[str, tuple] = {
    "lookup_merchant_by_phone": None,   # filled in tool_defs.py
    "find_debtors_by_name": None,
    "get_debtor_outstanding": None,
    "list_recent_debts": None,
    "get_wallet_summary": None,
}

PROPOSE_TOOLS: dict[str, tuple] = {
    "propose_onboard_merchant": None,
    "propose_record_debt": None,
    "propose_create_collection_account": None,
    "propose_mark_paid": None,
}
