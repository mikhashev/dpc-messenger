"""The Telegram footer for an agent reply: the same four context-window rows
and the same balance line the DPC UI shows beside a chat, ported to Python so
a reply sent to Telegram carries the numbers a person reading it in the app
would see (card TELEGRAM-AGENT-REPLIES-CARRY-NO-BALANCE-OR-CONTEXT-STATS,
Mike's decision 2026-09-28).

Every number here is read from a backend source that already exists — this
module adds no second computation of a figure the backend measures. The one
exception is the dialogue estimator: the UI's number there
(`estimateTokens`/`nonDialogTokens` in
dpc-client/ui/src/lib/tokenEstimator.ts and
dpc-client/ui/src/lib/utils/nonDialogTokens.ts) is computed only in
TypeScript, so it is ported here verbatim, function for function, with each
one naming its TS original.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# --- dpc-client/ui/src/lib/tokenEstimator.ts:estimateTokens ---------------

DEFAULT_TOKEN_LIMIT = 16384  # SessionControls.svelte's DEFAULT_TOKEN_LIMIT


def estimate_tokens(text: str) -> int:
    """Port of tokenEstimator.ts:estimateTokens — 4 chars ≈ 1 token, floored."""
    if not text:
        return 0
    return len(text) // 4


# --- dpc-client/ui/src/lib/utils/nonDialogTokens.ts -----------------------


def non_dialog_tokens(measured_total: float, estimated_dialogue: float) -> int:
    """Port of nonDialogTokens.ts:nonDialogTokens — the remainder, floored at
    zero: `max(0, measuredTotal - min(estimatedDialogue, measuredTotal))`."""
    if not (measured_total and measured_total > 0):
        return 0
    return int(max(0, measured_total - min(estimated_dialogue, measured_total)))


def has_component_breakdown(breakdown: Optional[List[Dict[str, Any]]]) -> bool:
    """Port of nonDialogTokens.ts:hasComponentBreakdown."""
    return bool(breakdown)


def non_dialog_label(
    context_agent: Optional[str],
    breakdown: Optional[List[Dict[str, Any]]],
) -> str:
    """Port of nonDialogTokens.ts:nonDialogLabel."""
    if context_agent:
        return "Agent ctx"
    return "Static" if has_component_breakdown(breakdown) else "Non-dialog"


# --- the four rows (SessionControls.svelte's $derived block) -------------


def _fmt_int(n: float) -> str:
    return f"{int(round(n)):,}"


def build_stats_footer(
    session_state: Dict[str, Any],
    context_agent: str = "",
) -> Optional[str]:
    """The DIALOG / TOTAL / <label> / MESSAGES block, computed the same way
    SessionControls.svelte derives it for the same fields
    (DpcAgentManager.get_session_state's history_tokens,
    tokens_after_last_response, tokens_limit, messages_count,
    context_breakdown).

    Returns None when there is nothing measured yet (`tokens_after_last_
    response` not yet set) — the UI's fallback for that case is the plain
    single-line counter, which carries nothing this footer would add.
    """
    tokens_after = session_state.get("tokens_after_last_response")
    if not (isinstance(tokens_after, (int, float)) and tokens_after > 0):
        return None

    history_tokens = float(session_state.get("history_tokens") or 0)
    tokens_limit = float(session_state.get("tokens_limit") or 0)
    breakdown = session_state.get("context_breakdown")
    message_count = int(
        session_state.get("messages_count")
        if session_state.get("messages_count") is not None
        else session_state.get("message_count") or 0
    )

    effective_limit = tokens_limit if tokens_limit > 0 else DEFAULT_TOKEN_LIMIT
    # Clamp so the chars/4 estimate never exceeds the measured total (same
    # comment as SessionControls.svelte's effectiveHistoryTokens).
    effective_history = min(history_tokens, tokens_after)
    static_memory = non_dialog_tokens(tokens_after, history_tokens)
    dialog_available = effective_limit - static_memory
    dialog_pct = (effective_history / dialog_available * 100) if dialog_available > 0 else 0.0
    total_pct = (tokens_after / effective_limit * 100) if effective_limit > 0 else 0.0
    label = non_dialog_label(context_agent, breakdown).upper()

    lines = [
        f"DIALOG      {_fmt_int(effective_history)} / {_fmt_int(dialog_available)} ({round(dialog_pct)}%)",
        f"TOTAL       {_fmt_int(tokens_after)} / {_fmt_int(effective_limit)} ({round(total_pct)}%)",
        f"{label:<10} ≈{_fmt_int(static_memory)}",
        f"MESSAGES    {message_count}",
    ]
    return "\n".join(lines)


# --- balance line: inferenceSharing.ts:formatQuotaLine (compact subset) ---

_WINDOW_LABELS = {"3h": "3h", "minute": "min", "week": "this week", "iso-week": "this week"}


def _window_label(name: Optional[str]) -> str:
    return _WINDOW_LABELS.get(name or "", name or "")


def _quota_window_percent(win: Dict[str, Any]) -> Optional[int]:
    """Port of inferenceSharing.ts:quotaWindowPercent."""
    used, limit = win.get("used"), win.get("limit")
    if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)) or limit <= 0:
        return None
    pct = used / limit * 100
    return round(min(max(pct, 0), 100))


def _format_quota_window(win: Dict[str, Any]) -> str:
    """Port of inferenceSharing.ts:formatQuotaWindow, without the reset-time
    suffix (a footer line has no room for a per-window timestamp)."""
    if win.get("name") == "minute" and isinstance(win.get("used"), (int, float)) and isinstance(win.get("limit"), (int, float)):
        return f"rpm {int(win['used'])}/{int(win['limit'])}"
    pct = _quota_window_percent(win)
    if pct is None:
        return ""
    return f"{_window_label(win.get('name'))} {pct}%"


def format_quota_line(quota: Dict[str, Any]) -> str:
    """Compact port of inferenceSharing.ts:formatQuotaLine's quota half
    (tier/windows/daily-capacity/night — the wallet half is handled
    separately by `format_balance_line`, since a subscription key's wallet
    is a reference price, never a debit).

    One deviation from the TS original, per the card's own example (Mike,
    2026-09-28): the tier reads as the bare tier name ("free"), not
    "<tier> tier" — a footer line has less room than the balance card this
    was ported from.
    """
    if not quota:
        return ""
    parts: List[str] = []
    tier = quota.get("tier")
    if tier:
        parts.append(str(tier))
    for win in quota.get("windows") or []:
        line = _format_quota_window(win)
        if line:
            parts.append(line)
    if isinstance(quota.get("parallel_limit"), (int, float)):
        parts.append(f"{int(quota['parallel_limit'])} parallel")
    cap = quota.get("daily_capacity") or {}
    if isinstance(cap.get("pct_used"), (int, float)):
        parts.append(f"day {cap['pct_used']}%")
    if (quota.get("night") or {}).get("active"):
        parts.append("night hours: limits doubled, already counted")
    return " · ".join(parts)


def format_balance_line(label: str, balance: Optional[Dict[str, Any]]) -> Optional[str]:
    """One footer line for the provider the agent answered with:

        DeepSeek USD 10.42
        NeuralDeep free · 3h 4% · day 0.1%

    None when the balance could not be read (`is_available: False` or no
    usable field at all) — the caller omits the line entirely rather than
    printing an error into the chat."""
    if not balance or balance.get("is_available") is False:
        return None
    quota = balance.get("quota") or {}
    billing_mode = quota.get("billing_mode") or balance.get("billing_mode")
    if billing_mode == "subscription":
        quota_text = format_quota_line(quota)
        return f"{label} {quota_text}".strip() if quota_text else None
    infos = balance.get("balance_infos") or []
    if not infos:
        return None
    info = infos[0]
    total = info.get("total_balance")
    if total is None:
        return None
    currency = info.get("currency") or ""
    return f"{label} {currency} {total}".strip()


def select_balance_for_alias(
    balances_payload: Optional[Dict[str, Any]],
    provider_alias: str,
) -> Optional[Any]:
    """From `service.get_provider_balances()`'s result, the (label, balance)
    pair for the account `provider_alias` belongs to — or None when the
    alias is absent, its account failed, or the provider carries no balance
    at all (a local model: never in `accounts`, since `get_provider_
    balances` only lists `supports_balance()` providers)."""
    if not balances_payload or not provider_alias:
        return None
    for account in balances_payload.get("accounts") or []:
        if provider_alias in (account.get("aliases") or []):
            result = account.get("result") or {}
            if result.get("status") != "success":
                return None
            return account.get("label") or account.get("provider_type") or provider_alias, result.get("balance")
    return None


def build_footer(
    session_state: Dict[str, Any],
    *,
    context_agent: str = "",
    balance_label: Optional[str] = None,
    balance: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """The whole footer block: the four stats rows, plus one balance line
    when a label/balance pair is given. Returns None when there is nothing
    to show at all (no measured total yet)."""
    stats = build_stats_footer(session_state, context_agent=context_agent)
    if stats is None:
        return None
    if balance_label:
        balance_line = format_balance_line(balance_label, balance)
        if balance_line:
            return f"{stats}\n{balance_line}"
    return stats
