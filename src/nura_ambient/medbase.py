"""MedBase MCP client — READ ONLY, enforced mechanically.

NURA Ambient is the capture layer. MedBase owns every clinical write and its
`phi_audit` chain. That boundary is not a convention in a document here: this
module physically cannot call a write tool, because `call()` refuses any tool
not in READ_ONLY_TOOLS before it builds a request.

If a future change needs a write, it has to edit READ_ONLY_TOOLS in this file,
which is a one-line diff that shows up in review. That is the point.

Transport: MedBase exposes a Streamable-HTTP JSON-RPC MCP endpoint at
/flexmcp/api/mcp (serverInfo `medbase-crm`, protocol 2024-11-05). Responses can
come back as bare JSON or as SSE-framed `data:` lines, and both are handled.

Why a client here at all: an ambient recording is useless until someone can say
which patient it belongs to. Attribution answers that question. It does not
answer it by guessing — it returns ranked candidates and
`requires_confirmation: true`, and MedBase makes the actual association.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

# --------------------------------------------------------------------------
# THE WRITE BOUNDARY.
#
# 46 tools exist on medbase-crm v1.0.1. These are the only ones this service
# may call. Every other tool - including create_patient, update_patient,
# add_note, create_reminder, create_payment, reply_ticket - is refused here.
#
# Adding a name to this set is a governance change, not a refactor.
# --------------------------------------------------------------------------
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "search_patients",
    "list_patients",
    "get_patient",
    "get_patient_by_mrn",
    "get_patient_summary",
    "list_patient_records",
    "get_patient_portal_access",
    "search_crm",
})

# Named explicitly so a test can assert these are refused. Not strictly needed
# for the check, but it documents the intent where a reviewer will look.
KNOWN_WRITE_TOOLS: frozenset[str] = frozenset({
    "create_patient", "update_patient", "update_patient_emergency_contact",
    "create_lead", "update_lead",
    "add_note", "create_reminder", "create_task",
    "create_ticket", "reply_ticket",
    "create_estimate", "create_credit_note", "create_subscription",
    "create_proposal", "create_project", "create_contract",
    "create_expense", "create_payment",
})


class MedBaseWriteBlocked(PermissionError):
    """Raised when a caller attempts a tool outside READ_ONLY_TOOLS."""


class MedBaseUnavailable(RuntimeError):
    """Raised when the transport fails. Never swallowed into an empty result."""


def _strip_sse(raw: str) -> str:
    """MCP streamable HTTP may wrap the JSON in SSE `data:` frames."""
    if raw.lstrip().startswith("{"):
        return raw
    payload = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            payload.append(line[5:].lstrip())
    return "\n".join(payload) if payload else raw


class MedBaseClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30) -> None:
        if not base_url:
            raise ValueError("MedBase base_url is required")
        if not token:
            raise ValueError("MedBase token is required")
        # Accept either the bare host or the full MCP path.
        url = base_url.rstrip("/")
        if not url.endswith("/mcp"):
            url = url + "/flexmcp/api/mcp"
        self.url = url
        self._token = token
        self.timeout = timeout
        self._id = 0

    # ---------------------------------------------------------------- plumbing
    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params or {},
        }).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            # Do not leak the token or the body into the message.
            raise MedBaseUnavailable(f"MedBase HTTP {e.code}") from None
        except Exception as e:  # noqa: BLE001 - transport failures are all one class here
            raise MedBaseUnavailable(f"MedBase transport failed: {type(e).__name__}") from None

        try:
            data = json.loads(_strip_sse(raw))
        except json.JSONDecodeError:
            raise MedBaseUnavailable("MedBase returned a non-JSON payload") from None

        if "error" in data:
            raise MedBaseUnavailable(f"MedBase RPC error: {data['error'].get('message', 'unknown')}")
        return data.get("result", {})

    def initialize(self) -> dict:
        return self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "nura-ambient", "version": "0.1.0"},
        })

    def list_tools(self) -> list[dict]:
        return self._rpc("tools/list", {}).get("tools", [])

    # ---------------------------------------------------------------- the guard
    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call a READ-ONLY tool. Refuses anything else before sending."""
        if tool not in READ_ONLY_TOOLS:
            raise MedBaseWriteBlocked(
                f"tool {tool!r} is not in READ_ONLY_TOOLS; "
                "nura-ambient is capture-only and MedBase owns every clinical write"
            )
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments or {}})
        return self._unwrap(result)

    @staticmethod
    def _unwrap(result: dict) -> Any:
        """MCP returns content blocks. Prefer structured, fall back to text."""
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        parts = []
        for block in result.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    parts.append(text)
        return parts[0] if len(parts) == 1 else parts

    # ---------------------------------------------------------------- domain
    def search_patients(self, query: str, limit: int = 10) -> Any:
        return self.call("search_patients", {"q": query, "limit": limit})

    def get_patient(self, patient_id: int) -> Any:
        return self.call("get_patient", {"id": patient_id})


# --------------------------------------------------------------------------
# Attribution: turn a transcript into ranked candidate patients.
# --------------------------------------------------------------------------

def _candidates(payload: Any) -> list[dict]:
    """Normalise the shape MedBase returns into a flat list."""
    if isinstance(payload, dict):
        for key in ("patients", "data", "results", "items", "records"):
            if isinstance(payload.get(key), list):
                return [c for c in payload[key] if isinstance(c, dict)]
        # a single patient object
        if any(k in payload for k in ("id", "patient_id", "mrn")):
            return [payload]
    if isinstance(payload, list):
        return [c for c in payload if isinstance(c, dict)]
    return []


def suggest_attribution(client: MedBaseClient, query: str, limit: int = 5) -> dict:
    """Return ranked candidates. Never confirms, never writes.

    `requires_confirmation` is always true: this function's output is evidence
    for a human or for MedBase to act on, not an association.
    """
    raw = client.search_patients(query, limit=limit)
    cands = _candidates(raw)

    out = []
    for c in cands[:limit]:
        pid = c.get("id") or c.get("patient_id")
        out.append({
            "patient_id": pid,
            "mrn": c.get("mrn") or c.get("medical_record_number"),
            # Only the fields needed to recognise a match. No DOB, no SSN,
            # no address - attribution does not need them and the response may
            # be displayed or logged.
            "display_name": c.get("full_name") or c.get("name") or _name(c),
            "email": c.get("email"),
        })

    return {
        "query": query,
        "candidates": out,
        "candidate_count": len(out),
        "requires_confirmation": True,
        "source": "medbase.search_patients",
        # Stated so a consumer cannot mistake this for an association.
        "note": "read-only suggestion from MedBase; no record was modified",
    }


def _name(c: dict) -> str | None:
    first = c.get("firstname") or c.get("first_name") or ""
    last = c.get("lastname") or c.get("last_name") or ""
    joined = f"{first} {last}".strip()
    return joined or None
