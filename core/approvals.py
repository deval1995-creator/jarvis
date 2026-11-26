"""Approval cards: Deval's yes for each exact action outside the PC.

When Claude calls a tool that needs approval, Claude Code asks the Core
(`can_use_tool`). The broker stores the exact arguments with their hash, shows
a card, and waits for Deval. On approve, the tool runs with exactly the stored
arguments (passed back as `updated_input`), so nothing can change between the
card and the action. Each approval covers one call; no timeout means no.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from core.db import utc_now

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10 * 60

OnEvent = Callable[[dict], Awaitable[None]]


def arguments_hash(arguments: dict) -> str:
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalError(ValueError):
    pass


class ApprovalBroker:
    def __init__(self, conn: sqlite3.Connection, on_event: OnEvent | None = None, timeout: float = TIMEOUT_SECONDS):
        self.conn = conn
        self.on_event = on_event
        self.timeout = timeout
        self._waiting: dict[str, asyncio.Future] = {}

    def get(self, approval_id: str) -> dict:
        row = self.conn.execute("select * from approvals where id = ?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalError(f"no approval {approval_id}")
        d = dict(row)
        d["arguments"] = json.loads(d["arguments"])
        return d

    def list(self, status: str = "pending") -> list[dict]:
        rows = self.conn.execute(
            "select id from approvals where status = ? order by created_at", (status,)
        ).fetchall()
        return [self.get(r[0]) for r in rows]

    async def _emit(self, event: dict) -> None:
        if self.on_event is not None:
            await self.on_event(event)

    def _set_status(self, approval_id: str, status: str) -> None:
        self.conn.execute(
            "update approvals set status = ?, decided_at = ? where id = ? and status = 'pending'",
            (status, utc_now(), approval_id),
        )

    async def request(
        self,
        tool: str,
        arguments: dict,
        *,
        conversation_id: str | None,
        untrusted: bool,
        on_created: Callable[[str], None] | None = None,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Show a card and wait for Deval. Returns the permission result for Claude Code."""
        approval_id = uuid.uuid4().hex[:12]
        frozen = json.loads(json.dumps(arguments))  # our own copy of exactly what was asked
        self.conn.execute(
            "insert into approvals (id, conversation_id, tool, arguments, arguments_hash, untrusted, created_at)"
            " values (?, ?, ?, ?, ?, ?, ?)",
            (approval_id, conversation_id, tool, json.dumps(frozen, ensure_ascii=False),
             arguments_hash(frozen), int(untrusted), utc_now()),
        )
        if on_created is not None:
            on_created(approval_id)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting[approval_id] = future
        await self._emit({"type": "approval_request", "approval": self.get(approval_id)})
        try:
            decision = await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            decision = "expired"
            self._set_status(approval_id, "expired")
            await self._emit({"type": "approval_decided", "approval": self.get(approval_id)})
        except asyncio.CancelledError:
            self._set_status(approval_id, "cancelled")
            raise
        finally:
            self._waiting.pop(approval_id, None)

        if decision == "approved":
            stored = self.get(approval_id)
            if arguments_hash(stored["arguments"]) != stored["arguments_hash"]:  # tampered with in the db
                return PermissionResultDeny(message="Approval record did not match; not done.")
            return PermissionResultAllow(updated_input=stored["arguments"])
        reasons = {
            "rejected": "Deval rejected this action. It was not done. Don't retry it unless Deval asks.",
            "expired": "No answer from Deval within 10 minutes, so it was not done.",
            "cancelled": "Cancelled (Deval interrupted or started a new conversation). It was not done.",
        }
        return PermissionResultDeny(message=reasons.get(decision, "Not approved; not done."))

    async def decide(self, approval_id: str, approve: bool) -> dict:
        a = self.get(approval_id)
        if a["status"] != "pending":
            raise ApprovalError(f"approval {approval_id} is already {a['status']}")
        future = self._waiting.get(approval_id)
        status = "approved" if approve else "rejected"
        self._set_status(approval_id, status)
        if future is not None and not future.done():
            future.set_result(status)
        elif future is None and approve:
            # The turn that asked is gone (e.g. the Core restarted); nothing can run.
            self.conn.execute("update approvals set status = 'expired' where id = ?", (approval_id,))
        a = self.get(approval_id)
        await self._emit({"type": "approval_decided", "approval": a})
        return a

    async def cancel_all(self) -> int:
        """Refuse everything still waiting (Deval interrupted, new conversation)."""
        n = 0
        for approval_id, future in list(self._waiting.items()):
            if not future.done():
                self._set_status(approval_id, "cancelled")
                future.set_result("cancelled")
                await self._emit({"type": "approval_decided", "approval": self.get(approval_id)})
                n += 1
        return n

    def expire_orphans(self) -> int:
        """At startup: anything still pending belongs to a turn that no longer exists."""
        return self.conn.execute(
            "update approvals set status = 'expired', decided_at = ? where status = 'pending'", (utc_now(),)
        ).rowcount
