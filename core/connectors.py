"""Which claude.ai connectors Jarvis may use, and how.

Claude Code loads every claude.ai connector the account has. Jarvis uses three:
Gmail and Calendar (reads free, actions with approval) and Interactive Brokers
(read-only; trading tools are blocked). Three layers keep it that way:

1. Selection: at startup the Core switches off every other MCP server for
   Jarvis's home folder (Claude Code remembers this per folder), so they stop
   loading into the context.
2. Permissions: the read tools below run freely; APPROVAL_TOOLS run only after
   Deval approves the exact call (an approval card); BLOCKED_TOOLS are denied by
   name and never reach Claude; anything else is refused by `deny_unlisted`.
3. Taint: once a session reads email or calendar content, it is marked
   untrusted, and memory changes wait for Deval's approval.
"""

from __future__ import annotations

import asyncio
import logging
import re

from claude_agent_sdk import PermissionResultDeny

log = logging.getLogger(__name__)

GMAIL = "claude.ai Gmail"
CALENDAR = "claude.ai Google Calendar"
IBKR = "claude.ai Interactive Brokers (IBKR)"
ALLOWED_SERVERS = {GMAIL, CALENDAR, IBKR}
OWN_SERVERS = {"memory", "display", "tools"}  # Jarvis's in-process servers: never switched off


def _prefix(server_name: str) -> str:
    """'claude.ai Interactive Brokers (IBKR)' -> 'mcp__claude_ai_Interactive_Brokers_IBKR__'

    Claude Code turns each run of other characters into one underscore."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", server_name).strip("_")
    return f"mcp__{slug}__"


READ_TOOLS = {
    GMAIL: ["search_threads", "get_thread", "get_message"],
    CALENDAR: ["list_calendars", "list_events", "get_event", "search_events"],
    # Read-only account and market data. Never orders: Jarvis doesn't trade.
    IBKR: [
        "get_account_summary", "get_account_balances", "get_account_positions", "get_account_orders",
        "get_account_trades", "get_pa_performance_all_periods", "get_pa_allocation",
        "get_price_snapshot", "get_price_history", "search_contracts", "get_watchlists", "get_watchlist",
    ],
}
# Actions outside the PC that Jarvis may take, one approved call at a time.
APPROVAL_TOOLS = {
    GMAIL: ["send_message", "reply", "create_draft"],
    CALENDAR: ["create_event", "update_event", "respond_to_event"],
}
# Never: destructive, or easy to misuse. Denied by name so they can't be enabled by accident.
BLOCKED_TOOLS = {
    GMAIL: [
        "forward", "update_draft", "delete_draft",
        "trash_thread", "untrash_thread", "trash_message", "untrash_message",
        "mark_thread_spam", "unmark_thread_spam", "mark_message_spam", "unmark_message_spam",
        "label_thread", "unlabel_thread", "label_message", "unlabel_message", "update_message_labels",
        "apply_sensitive_thread_label", "apply_sensitive_message_label",
        "create_label", "update_label", "delete_label", "download_gmail_attachment",
    ],
    CALENDAR: ["delete_event"],
    IBKR: [
        "create_order_instruction", "delete_order_instruction",
        "create_alert", "update_alert", "delete_alert", "set_alert_status",
        "create_watchlist", "edit_watchlist", "delete_watchlist",
        "create_saved_screener", "update_saved_screener", "delete_saved_screener",
        "provide_customer_feedback",
    ],
}

# Not needed, so not loaded: every tool definition is sent to Claude on every turn
# (IBKR alone has 49), which costs Pro usage and response time.
UNUSED_TOOLS = {
    GMAIL: ["list_drafts", "get_draft", "list_labels"],  # ~3,400 tokens, rarely useful by voice
    CALENDAR: ["suggest_time"],
    IBKR: [
        "get_alert", "get_alerts", "get_combo_identifier", "get_option_data", "get_option_parameters",
        "search_futures", "get_earnings_insight", "search_earnings_insights", "get_flow_type",
        "get_company_connections", "get_company_themes", "get_theme_details", "search_investment_topics",
        "get_order_instructions", "get_saved_screener", "get_screener_asset_classes",
        "get_screener_capabilities", "get_screener_industries", "get_screener_templates",
        "get_screener_themes", "list_saved_screeners", "preview_screener", "run_saved_screener", "whats_new",
    ],
}

ALLOWED_TOOLS = [_prefix(s) + t for s, tools in READ_TOOLS.items() for t in tools]
# Reads whose results hold text other people wrote (emails, invites). Broker data isn't that.
_READ_SET = frozenset(_prefix(s) + t for s in (GMAIL, CALENDAR) for t in READ_TOOLS[s])
NEEDS_APPROVAL = {_prefix(s) + t for s, tools in APPROVAL_TOOLS.items() for t in tools}
DISALLOWED_TOOLS = [_prefix(s) + t for group in (BLOCKED_TOOLS, UNUSED_TOOLS)
                    for s, tools in group.items() for t in tools]
# Claude Code's built-in web tools (part of the Pro plan): search, and read a page.
WEB_TOOLS = ["WebSearch", "WebFetch"]
# Jarvis's own web tools (Tavily): also text written by strangers.
OWN_WEB_TOOLS = ["mcp__tools__search", "mcp__tools__read_page"]


def reads_outside_content(tool_name: str) -> bool:
    """Tools whose results contain text other people wrote (emails, invites, web pages)."""
    return tool_name in _READ_SET or tool_name in WEB_TOOLS or tool_name in OWN_WEB_TOOLS


async def deny_unlisted(tool_name: str, tool_input: dict, context) -> PermissionResultDeny:
    """Called for any tool that is neither allowed nor explicitly denied."""
    log.warning("refused unlisted tool %s", tool_name)
    return PermissionResultDeny(message=f"{tool_name} is not enabled for Jarvis.")


async def wait_for_servers(client, timeout: float = 45.0) -> list[dict]:
    """Connectors load in the background; wait until the ones Jarvis uses are
    listed and none is pending.

    Jarvis's own in-process servers are listed at once, but the claude.ai
    connectors join the list later. Waiting only for "nothing pending" could
    return before they appear, leaving a session without Gmail, Calendar or IBKR.
    """
    servers: list[dict] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        servers = (await client.get_mcp_status()).get("mcpServers", [])
        names = {s.get("name") for s in servers}
        settled = servers and not any(s.get("status") == "pending" for s in servers)
        if settled and ALLOWED_SERVERS <= names:
            break
        await asyncio.sleep(0.5)
    else:
        missing = ALLOWED_SERVERS - {s.get("name") for s in servers}
        log.warning("connectors still missing after %.0f s: %s", timeout, sorted(missing))
    return servers


async def select_servers(client) -> dict[str, str]:
    """Switch off every server Jarvis doesn't use, switch on the ones it does.

    Claude Code saves this for Jarvis's home folder, so later sessions start
    without the other connectors. Returns {server: status} after the change.
    """
    servers = await wait_for_servers(client)
    for s in servers:
        name, status = s["name"], s.get("status")
        wanted = name in ALLOWED_SERVERS or name in OWN_SERVERS
        if not wanted and status not in ("disabled", "needs-auth", "failed"):
            log.info("switching off connector %s for Jarvis", name)
            await client.toggle_mcp_server(name, False)
        elif wanted and status == "disabled":
            await client.toggle_mcp_server(name, True)
    return {s["name"]: s.get("status") for s in (await client.get_mcp_status()).get("mcpServers", [])}


# Statuses Jarvis can repair by itself; "needs-auth" needs Deval to sign in again.
REPAIRABLE = {"disabled", "failed", "disconnected", "error"}


async def ensure_connected(client, settle: float = 15.0) -> dict[str, str]:
    """Make sure every connector Jarvis uses is connected in this session.

    Missing or still loading: wait for it. Switched off: switch it on. Failed or
    dropped: reconnect it. Returns {server: status} afterwards; anything still
    not "connected" (e.g. needs-auth) is logged.
    """
    status = {s["name"]: s.get("status") for s in (await client.get_mcp_status()).get("mcpServers", [])}
    if all(status.get(n) == "connected" for n in ALLOWED_SERVERS):
        return status  # the usual case: one quick local call
    for name in sorted(ALLOWED_SERVERS):
        st = status.get(name)
        try:
            if st == "disabled":
                log.info("connector %s was switched off; switching it on", name)
                await client.toggle_mcp_server(name, True)
            elif st in REPAIRABLE:
                log.info("connector %s is %s; reconnecting", name, st)
                await client.reconnect_mcp_server(name)
        except Exception:
            log.exception("could not repair connector %s", name)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settle
    while loop.time() < deadline:  # missing ones may still be loading
        status = {s["name"]: s.get("status") for s in (await client.get_mcp_status()).get("mcpServers", [])}
        if all(status.get(n) not in (None, "pending") for n in ALLOWED_SERVERS):
            break
        await asyncio.sleep(0.5)
    bad = {n: status.get(n, "missing") for n in ALLOWED_SERVERS if status.get(n) != "connected"}
    if bad:
        log.warning("connectors not available: %s", bad)
    return status
