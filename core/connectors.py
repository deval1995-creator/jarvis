"""Which claude.ai connectors Jarvis may use, and how.

Claude Code loads every claude.ai connector the account has. Jarvis needs two,
read-only. Three layers keep it that way:

1. Selection: at startup the Core switches off every other MCP server for
   Jarvis's home folder (Claude Code remembers this per folder), so they stop
   loading into the context.
2. Permissions: only the read tools below are allowed; the write tools are
   explicitly denied; anything else hits `deny_unlisted` and is refused.
3. Taint: once a session reads email or calendar content, it is marked
   untrusted, and memory changes wait for Deval's approval.
"""

from __future__ import annotations

import asyncio
import logging

from claude_agent_sdk import PermissionResultDeny

log = logging.getLogger(__name__)

GMAIL = "claude.ai Gmail"
CALENDAR = "claude.ai Google Calendar"
ALLOWED_SERVERS = {GMAIL, CALENDAR}
OWN_SERVERS = {"memory"}  # Jarvis's in-process servers: never switched off


def _prefix(server_name: str) -> str:
    """'claude.ai Google Calendar' -> 'mcp__claude_ai_Google_Calendar__'"""
    slug = "".join(ch if ch.isalnum() else "_" for ch in server_name)
    return f"mcp__{slug}__"


READ_TOOLS = {
    GMAIL: ["search_threads", "get_thread", "get_message", "list_labels", "list_drafts", "get_draft"],
    CALENDAR: ["list_calendars", "list_events", "get_event", "search_events", "suggest_time"],
}
# Listed so they are refused by name even if a later setting would allow them.
WRITE_TOOLS = {
    GMAIL: [
        "send_message", "reply", "forward", "create_draft", "update_draft", "delete_draft",
        "trash_thread", "untrash_thread", "trash_message", "untrash_message",
        "mark_thread_spam", "unmark_thread_spam", "mark_message_spam", "unmark_message_spam",
        "label_thread", "unlabel_thread", "label_message", "unlabel_message", "update_message_labels",
        "apply_sensitive_thread_label", "apply_sensitive_message_label",
        "create_label", "update_label", "delete_label", "download_gmail_attachment",
    ],
    CALENDAR: ["create_event", "update_event", "delete_event", "respond_to_event"],
}

ALLOWED_TOOLS = [_prefix(s) + t for s, tools in READ_TOOLS.items() for t in tools]
DISALLOWED_TOOLS = [_prefix(s) + t for s, tools in WRITE_TOOLS.items() for t in tools]
UNTRUSTED_PREFIXES = tuple(_prefix(s) for s in ALLOWED_SERVERS)


def reads_outside_content(tool_name: str) -> bool:
    return tool_name.startswith(UNTRUSTED_PREFIXES)


async def deny_unlisted(tool_name: str, tool_input: dict, context) -> PermissionResultDeny:
    """Called for any tool that is neither allowed nor explicitly denied."""
    log.warning("refused unlisted tool %s", tool_name)
    return PermissionResultDeny(message=f"{tool_name} is not enabled for Jarvis.")


async def wait_for_servers(client, timeout: float = 30.0) -> list[dict]:
    """Connectors load in the background; wait until none is pending."""
    servers: list[dict] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        servers = (await client.get_mcp_status()).get("mcpServers", [])
        if servers and not any(s.get("status") == "pending" for s in servers):
            break
        await asyncio.sleep(0.5)
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
