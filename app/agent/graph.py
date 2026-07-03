"""The LangGraph agent — a confirmation-guarded ReAct loop.

Topology:
    START
      ↓
    resolve_identity  ── decides onboarding vs returning, sets language
      ↓
  ┌─► agent           ── calls OpenAI with tools
  │   ↓
  │   router
  │   ├── (called tools) ─► tools ─► agent  (loop)
  │   ├── (final msg)   ─► deliver ─► END
  │   └── (proposed action) ─► ask_confirm ─► END  (waits for merchant)
  │
  │   (merchant replies to a pending action)
  │   ↓
  └─ confirm_router
      ├── "yes" ─► commit ─► deliver ─► END
      └── "no"/other ─► agent (re-enters loop with the no)

The key insight: the model can ONLY propose. The commit path is entered from
confirm_router, which only fires when a human said yes. The model cannot reach
commit_* on its own.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
# MemorySaver kept as fallback; MongoCheckpointSaver is the default.
from langgraph.checkpoint.memory import MemorySaver
from app.agent.mongo_checkpointer import MongoCheckpointSaver

from app.agent.state import AgentState
from app.agent.tool_catalog import execute_tool, schemas_for_llm
from app.agent.tools_read import lookup_merchant_by_phone
from app.agent.tools_write import commit_pending_action
from app.agent.prompt import SYSTEM_PROMPT
from app.config import settings

log = logging.getLogger("paywise.agent")


# ---- the model ---------------------------------------------------------

_llm = ChatOpenAI(
    model=settings.openai_model,
    # GPT-5 (reasoning models) ONLY support temperature=1.
    # Langchain's default is 0.7, which GPT-5 rejects with a 400 error,
    # so we MUST explicitly set temperature=1.
    temperature=1.0,
    api_key=settings.openai_api_key,
)
_llm_with_tools = _llm.bind_tools(schemas_for_llm())


# ---- helpers -----------------------------------------------------------

def _is_yes(text: str) -> bool:
    t = (text or "").strip().lower()
    # covers pidgin + english + yoruba affirmatives
    affirmatives = {"yes", "y", "ye", "yeah", "ok", "okay", "go ahead",
                    "correct", "sure", "e hen", "ehen", "na so", "i concur",
                    "confirm", "do am", "abeg do am", "yes sir", "yes ma"}
    return t in affirmatives or any(t.startswith(w) for w in ("yes",))


def _is_no(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"no", "n", "nope", "cancel", "stop", "no sir", "abeg no"}


# ---- language detection (best-effort, per message) --------------------
# Lets the agent reply in the SAME language the merchant is using right now,
# instead of a fixed "pidgin". Falls back to "" if nothing matches.

_PIDGIN_MARKERS = ["abeg", "wetin", "how far", "i dey", "you dey", "sabi",
                   "no fit", "make i", "i go ", "e go", "don pay", "bros",
                   "na im", "you wan", "go do am", "wetin you", "abeg try"]
_HAUSA_MARKERS = ["sannu", "ina kwana", "na gode", "yaya", "lafiya",
                  "muna", "za ku", "ba shi", "ina son", "ba a", "gaskiya"]
_YORUBA_MARKERS = ["bawo", "e kaaro", "e kaasan", "daadaa", "o dab",
                   "e jo", "o seun", "kíni", "e le"]
_IGBO_MARKERS = ["kedu", "ndewo", "ụtụtụ", "ihe", "ọ dị", "bịa",
                 "unu", "kedu ka", "ọ dị mma", "ndi"]


def _detect_lang(text: str) -> str:
    t = (text or "").lower()
    if not t.strip():
        return ""

    def count(markers: list[str]) -> int:
        return sum(1 for m in markers if m in t)

    scores = {
        "pidgin": count(_PIDGIN_MARKERS),
        "hausa": count(_HAUSA_MARKERS),
        "yoruba": count(_YORUBA_MARKERS),
        "igbo": count(_IGBO_MARKERS),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else ""


# ============================================================ NODE: resolve_identity

async def resolve_identity(state: AgentState) -> AgentState:
    """First node: figure out who's talking and what language.

    We inspect the inbound message (which arrives as the last HumanMessage),
    pull the sender's phone, and look them up. For returning merchants we
    inject a context message into the conversation so the LLM knows who
    it's talking to — this is critical for the LLM to behave like it
    remembers the merchant.
    """
    phone = state.get("merchant_phone", "")
    merchant = await lookup_merchant_by_phone(phone)

    # Detect the language from the merchant's latest message so we reply in
    # the SAME language they're speaking right now (Hausa, Yoruba, Igbo, Pidgin).
    last_msg = ""
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            last_msg = str(m.content)
            break
    detected = _detect_lang(last_msg)

    if not merchant.get("exists"):
        state["merchant_id"] = None
        state["merchant_lang"] = detected or "pidgin"  # default until detected
        return state

    state["merchant_id"] = merchant["merchant_id"]
    state["merchant_lang"] = detected or merchant.get("preferred_lang", "pidgin")

    # Inject a context note so the LLM knows this is a returning merchant.
    # This is injected as a SystemMessage at the start of conversation history
    # (right after the user's message) so the LLM has the merchant's identity.
    context_note = (
        f"[SYSTEM CONTEXT: This is a RETURNING merchant. "
        f"Name: {merchant.get('name', 'unknown')}, "
        f"Business: {merchant.get('business_name', 'unknown')}, "
        f"Phone: {phone}, "
        f"Merchant ID: {merchant['merchant_id']}, "
        f"Wallet balance: {merchant.get('wallet_balance', '₦0')}. "
        f"Greet them by name and continue the conversation naturally. "
        f"Do NOT re-onboard them or ask for their name/business again.]"
    )
    # Insert the context note right before the latest HumanMessage
    # so the LLM sees it every time.
    state["messages"].insert(0, SystemMessage(content=context_note))

    return state


# ============================================================ NODE: agent

MAX_CONTEXT_MESSAGES = 24  # ~12 exchanges (Human+AI pairs). Keeps token growth bounded.


def _trim_messages(messages: list, max_count: int = MAX_CONTEXT_MESSAGES) -> list:
    """Drop oldest messages once the conversation exceeds max_count.

    We always keep:
      - The very first HumanMessage (gives the agent the original context).
      - The last max_count messages (recent context).
    Everything in between gets dropped. The DB remembers the facts; the agent
    only needs recent conversational flow to be coherent.

    Also handles corrupted checkpointer state: removes orphaned AIMessages
    with tool_calls that have no matching ToolMessage responses, and removes
    orphaned ToolMessages whose parent AIMessage was removed.
    """
    from langchain_core.messages import ToolMessage as _TM

    # ---- Step 1: scan for orphaned tool_calls globally ----
    # Collect all valid tool_call_ids that have a matching ToolMessage
    valid_tool_call_ids: set = set()
    for m in messages:
        if isinstance(m, _TM) and getattr(m, "tool_call_id", None):
            valid_tool_call_ids.add(m.tool_call_id)

    # Remove any AIMessage whose tool_calls ALL lack responses (orphaned)
    cleaned: list = []
    for m in messages:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            orphaned = all(
                tc.get("id") not in valid_tool_call_ids
                for tc in m.tool_calls
            )
            if orphaned:
                continue  # skip this orphaned AI message
        # Also skip orphaned ToolMessages (parent AIMessage was removed)
        if isinstance(m, _TM) and getattr(m, "tool_call_id", None):
            if m.tool_call_id not in valid_tool_call_ids:
                continue
        cleaned.append(m)

    # ---- Step 2: standard trimming ----
    if len(cleaned) <= max_count:
        return cleaned

    trimmed = [cleaned[0]] + cleaned[-max_count:]
    # If the first kept recent message is a ToolMessage, walk back to include
    # its parent AIMessage(tool_calls).
    while len(trimmed) > 1 and isinstance(trimmed[1], _TM):
        tool_id = trimmed[1].tool_call_id
        idx = None
        for i in range(len(cleaned) - 1, -1, -1):
            m = cleaned[i]
            if (not isinstance(m, _TM)
                    and getattr(m, "tool_calls", None)
                    and any(tc.get("id") == tool_id for tc in m.tool_calls)):
                idx = i
                break
        if idx is None:
            break
        trimmed = [cleaned[0]] + cleaned[idx:]
    return trimmed


async def agent_node(state: AgentState) -> AgentState:
    """Call OpenAI with the conversation + tools. Returns its message."""
    sys = SystemMessage(content=SYSTEM_PROMPT.format(
        preferred_lang=state.get("merchant_lang", "pidgin"),
        today=date.today().isoformat(),
    ))
    trimmed = _trim_messages(state["messages"])
    messages = [sys] + trimmed
    response: AIMessage = await _llm_with_tools.ainvoke(messages)
    state["messages"].append(response)
    return state


# ============================================================ NODE: tools

async def tools_node(state: AgentState) -> AgentState:
    """Execute every tool call the model made, append ToolMessages."""
    last: AIMessage = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return state

    for call in last.tool_calls:
        name = call["name"]
        args = call.get("args", {}) or {}
        log.info("tool call: %s(%s)", name, args)
        result = await execute_tool(name, args, state)
        state["messages"].append(ToolMessage(content=result, tool_call_id=call["id"]))
    return state


# ============================================================ NODE: ask_confirm

async def ask_confirm_node(state: AgentState) -> AgentState:
    """A propose_* tool left a pending_action. Surface the summary to the user."""
    pending = state.get("pending_action")
    if not pending:
        return state
    summary = pending.get("summary", "Abeg confirm: reply YES or NO.")
    # gentle reminder in-language tone — keep short, text mode
    prompt = f"{summary}\n\nReply YES to confirm or NO to cancel."
    state["messages"].append(AIMessage(content=prompt))
    state["reply_text"] = prompt
    state["reply_is_long"] = False
    state["awaiting_confirmation"] = True
    return state


# ============================================================ NODE: commit

async def commit_node(state: AgentState) -> AgentState:
    """Run the confirmed pending_action and report the result."""
    pending = state.get("pending_action")
    if not pending:
        return state
    try:
        result = await commit_pending_action(pending)
        log.info("committed %s: %s", pending["action_type"], result)
        ack = _ack_for(pending["action_type"], result)
    except Exception as e:
        log.exception("commit failed")
        err_detail = str(e)
        if "sandbox_limit" in err_detail:
            ack = (
                "Ah, Nomba sandbox don reach di limit of 2 virtual accounts. "
                "No wahala — tell di debtor to pay into your main Nomba account instead. "
                "I go still dey track di payment. Or you fit delete old accounts from Nomba dashboard."
            )
        else:
            ack = "Sorry, something no go through. Try again, or check your connection."

    state["messages"].append(AIMessage(content=ack))
    state["reply_text"] = ack
    state["reply_is_long"] = False
    state["pending_action"] = None
    state["awaiting_confirmation"] = False
    return state


def _ack_for(action_type: str, result: dict) -> str:
    """Short in-language acknowledgement per action type.

    Only used for GATED actions that flow through commit_node (after a yes).
    Auto-committed actions (draft, complete_pending_debt, reminders) carry their
    own `auto_reply` / `summary` which the LLM relays — but we keep entries here
    too as a safety net if they ever reach this node.
    """
    committed = result.get("committed", result) if isinstance(result, dict) else {}
    if result.get("error"):
        return f"E no go through. {result.get('detail') or result.get('error')}."
    if action_type == "onboard_merchant":
        from app.config import settings
        wallet_url = settings.base_url.rstrip("/") + "/wallet/login"
        password = result.get("password", "")
        return (
            f"You welcome, {result.get('name')}! PayWise don set. 🙌\n\n"
            f"Your wallet link na: {wallet_url}\n"
            f"Your password na: {password}\n"
            f"Save am well!\n\n"
            f"Anytime you ask me 'what's my password?' I go remind you.\n"
            f"Just send me voice note of anybody wey buy on credit — "
            f"name, phone, wetin dem buy, and how much. I go record am."
        )
    if action_type == "record_debt":
        return (f"Don write am ✅ {result.get('amount')} for "
                f"{result.get('debtor_name')}. "
                f"You wan make I send am account number make e pay?")
    if action_type == "record_debt_draft":
        return committed.get("auto_reply") or (
            f"Saved as draft ✍️ {result.get('amount')}. "
            f"Send the phone number make I complete am.")
    if action_type == "complete_pending_debt":
        return committed.get("auto_reply") or (
            f"Don complete am ✅ {result.get('amount')} for "
            f"{result.get('debtor_name')}.")
    if action_type == "create_collection_account":
        return (f"Done! I don send {result.get('bank_name', 'Nomba')} account "
                f"{result.get('account_number')} to the debtor. I go tell you "
                f"when e pay.")
    if action_type == "mark_paid":
        return f"Updated ✅ Balance now {result.get('balance')} ({result.get('status')})."
    if action_type == "edit_debt":
        return "Don change am ✅ " + ", ".join(result.get("applied", [])) + "."
    if action_type == "delete_debt":
        return "Okay, I don cancel that debt. ✅"
    if action_type == "set_reminder_preference":
        state = "ON" if result.get("reminders_enabled") else "OFF"
        return f"Reminders don turn {state}. ✅"
    if action_type == "send_reminder":
        if result.get("sent"):
            link = result.get("whatsapp_link", "")
            return (
                f"I don prepare the message for {result.get('debtor_name', 'the debtor')}. ✅\n\n"
                f"Click dis link to send am:\n{link}\n\n"
                f"E go open WhatsApp with the message already typed. Just tap Send."
            )
        return "E no go through. " + str(result.get("error", "unknown error")) + "."
    return "Done ✅"


# ============================================================ NODE: deliver

async def deliver_node(state: AgentState) -> AgentState:
    """Capture the agent's final text reply for the delivery layer.

    Voice-note decision:
      - ALWAYS text if the reply contains passwords, URLs, or account numbers.
      - Voice for replies >100 chars OR if merchant explicitly asked for voice.
    """
    _FORCE_TEXT_MARKERS = [
        "password", "your password", "your wallet link",
        "http://", "https://",
    ]

    # Detect if merchant explicitly asked for a voice note in their last message
    merchant_asked_voice = False
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_merchant = str(msg.content).lower()
            merchant_asked_voice = any(phrase in last_merchant for phrase in [
                "voice note", "send am as voice", "voice message",
                "send voice", "audio", "talk am", "speak am",
            ])
            log.info("deliver_node: merchant_asked_voice=%s (msg=%.60s)", merchant_asked_voice, last_merchant)
            break

    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content and not getattr(msg, "tool_calls", None):
            text = str(msg.content)
            state["reply_text"] = text

            lower = text.lower()
            force_text = any(m.lower() in lower for m in _FORCE_TEXT_MARKERS)
            if merchant_asked_voice:
                # Merchant explicitly asked -- always voice, even if text has URLs/passwords
                state["reply_is_long"] = True
            elif force_text:
                state["reply_is_long"] = False
            else:
                # Lower threshold: >100 chars triggers voice
                word_count = len(text.split())
                state["reply_is_long"] = (len(text) > 100)
            log.info("deliver_node: force_text=%s merchant_voice=%s reply_is_long=%s len=%d words=%d",
                     force_text, merchant_asked_voice, state["reply_is_long"], len(text), len(text.split()))
            break
    return state


# ============================================================ ROUTERS

def after_agent(state: AgentState) -> str:
    """Decide where to go after the agent replies.

    Priority:
      1) If a pending_action was registered → ask for confirmation.
      2) If the model made tool calls → execute them.
      3) Otherwise → it's a final message → deliver.
    """
    if state.get("pending_action") and state.get("awaiting_confirmation"):
        return "ask_confirm"
    last: AIMessage = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    return "deliver"


def confirm_router(state: AgentState) -> str:
    """Inspect the merchant's reply to a pending confirmation."""
    # the latest HumanMessage is the merchant's yes/no
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            text = str(msg.content)
            if _is_yes(text):
                return "commit"
            if _is_no(text):
                # clear the pending action, let the agent re-engage
                state["pending_action"] = None
                state["awaiting_confirmation"] = False
                state["messages"].append(AIMessage(
                    content="Okay, I don cancel am. Wetin you wan do next?"
                ))
                return "deliver"
            # ambiguous reply — treat as a new instruction, re-enter agent
            state["awaiting_confirmation"] = False
            return "agent"
    return "deliver"


# ============================================================ BUILD

def build_graph():
    g = StateGraph(AgentState)

    g.add_node("resolve_identity", resolve_identity)
    g.add_node("agent",            agent_node)
    g.add_node("tools",            tools_node)
    g.add_node("ask_confirm",      ask_confirm_node)
    g.add_node("commit",           commit_node)
    g.add_node("deliver",          deliver_node)

    g.set_entry_point("resolve_identity")
    # If awaiting a yes/no, confirm_router inspects the reply and decides:
    # commit / deliver / agent. Otherwise go straight to the agent loop.
    def _after_resolve(s):
        if s.get("awaiting_confirmation"):
            return confirm_router(s)
        return "agent"
    g.add_conditional_edges("resolve_identity", _after_resolve, {
        "commit": "commit", "deliver": "deliver", "agent": "agent",
    })
    g.add_conditional_edges("agent", after_agent, {
        "tools": "tools", "ask_confirm": "ask_confirm", "deliver": "deliver",
    })
    g.add_edge("tools", "agent")
    g.add_edge("ask_confirm", END)
    g.add_edge("commit", "deliver")
    g.add_edge("deliver", END)

    # Use MongoDB for persistent checkpoints (survives restarts).
    # Falls back to in-memory if MongoDB is not connected yet.
    checkpointer = MongoCheckpointSaver()
    log.info("Using MongoDB checkpointer for persistent conversation memory")
    return g.compile(checkpointer=checkpointer)


# Singleton compiled graph, built once at startup.
_app_graph = None


def get_graph():
    global _app_graph
    if _app_graph is None:
        _app_graph = build_graph()
    return _app_graph
