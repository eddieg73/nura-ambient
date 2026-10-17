"""Tests for the MedBase MCP client and the attribution route.

The load-bearing test here is `test_write_tools_are_refused_before_dispatch`.
NURA Ambient is the capture layer; MedBase owns every clinical write and its
phi_audit chain. If that ever stops being true, this test fails loudly instead of
the boundary quietly eroding.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from nura_ambient.medbase import (
    KNOWN_WRITE_TOOLS,
    READ_ONLY_TOOLS,
    MedBaseClient,
    MedBaseUnavailable,
    MedBaseWriteBlocked,
    suggest_attribution,
)

KEY = "dev-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


# --------------------------------------------------------------------------
# Fake transport: records every request so we can assert what was NOT sent.
# --------------------------------------------------------------------------
class FakeTransport:
    def __init__(self, tools_response=None, call_response=None, sse=False):
        self.sent: list[dict] = []
        self.tools_response = tools_response or {"tools": []}
        self.call_response = call_response or {"content": []}
        self.sse = sse

    def __call__(self, method, params):
        self.sent.append({"method": method, "params": params})
        if method == "tools/list":
            return self.tools_response
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "serverInfo": {"name": "medbase-crm"}}
        return self.call_response


def client_with(transport) -> MedBaseClient:
    c = MedBaseClient("https://example.invalid", "fmp_test")
    c._rpc = transport  # type: ignore[method-assign]
    return c


# --------------------------------------------------------------------------
# THE BOUNDARY
# --------------------------------------------------------------------------
def test_write_tools_are_refused_before_dispatch():
    """Every known write tool must raise, and nothing may reach the transport."""
    t = FakeTransport()
    c = client_with(t)

    for tool in sorted(KNOWN_WRITE_TOOLS):
        with pytest.raises(MedBaseWriteBlocked):
            c.call(tool, {"anything": 1})

    assert t.sent == [], f"a write tool reached the transport: {t.sent}"


def test_read_tools_are_allowed():
    t = FakeTransport(call_response={"content": [{"type": "text", "text": '{"patients": []}'}]})
    c = client_with(t)
    c.search_patients("smith")
    assert len(t.sent) == 1
    assert t.sent[0]["method"] == "tools/call"
    assert t.sent[0]["params"]["name"] == "search_patients"


def test_allowlist_does_not_contain_any_known_write_tool():
    overlap = READ_ONLY_TOOLS & KNOWN_WRITE_TOOLS
    assert overlap == frozenset(), f"a write tool leaked into the allowlist: {overlap}"


def test_unknown_tool_is_refused():
    """Default-deny: a tool we have never heard of is not allowed either."""
    t = FakeTransport()
    c = client_with(t)
    with pytest.raises(MedBaseWriteBlocked):
        c.call("delete_everything", {})
    assert t.sent == []


# --------------------------------------------------------------------------
# Transport behaviour
# --------------------------------------------------------------------------
def test_unwraps_structured_content():
    t = FakeTransport(call_response={"structuredContent": {"patients": [{"id": 1}]}})
    c = client_with(t)
    assert c.call("list_patients", {}) == {"patients": [{"id": 1}]}


def test_unwraps_text_content_as_json():
    t = FakeTransport(call_response={"content": [{"type": "text", "text": '{"id": 7, "mrn": "M1"}'}]})
    c = client_with(t)
    assert c.call("get_patient", {"id": 7}) == {"id": 7, "mrn": "M1"}


def test_sse_framing_is_stripped():
    """MCP streamable HTTP may answer with `data:` frames."""
    from nura_ambient.medbase import _strip_sse
    raw = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    assert json.loads(_strip_sse(raw))["result"]["ok"] is True


def test_bare_json_passes_through():
    from nura_ambient.medbase import _strip_sse
    assert json.loads(_strip_sse('{"a":1}'))["a"] == 1


# --------------------------------------------------------------------------
# Attribution semantics
# --------------------------------------------------------------------------
def test_attribution_never_claims_confirmation():
    t = FakeTransport(call_response={"content": [{"type": "text", "text": json.dumps(
        {"patients": [{"id": 12, "mrn": "M-12", "firstname": "Ana", "lastname": "Lopez",
                       "email": "a@example.invalid", "date_of_birth": "1970-01-01"}]})}]})
    c = client_with(t)
    out = suggest_attribution(c, "Ana Lopez", limit=5)

    assert out["requires_confirmation"] is True
    assert out["candidate_count"] == 1
    assert out["candidates"][0]["display_name"] == "Ana Lopez"
    # Attribution must not forward DOB / identifiers it does not need.
    assert "date_of_birth" not in out["candidates"][0]


def test_attribution_calls_only_read_tool():
    t = FakeTransport(call_response={"content": [{"type": "text", "text": '{"patients": []}'}]})
    c = client_with(t)
    suggest_attribution(c, "anyone")
    assert all(r["params"]["name"] in READ_ONLY_TOOLS for r in t.sent if r["method"] == "tools/call")


def test_transport_failure_is_not_an_empty_result():
    """A transport error must raise, never return 'no candidates found'."""
    def boom(method, params):
        raise MedBaseUnavailable("MedBase HTTP 502")
    c = client_with(type("T", (), {"__call__": staticmethod(boom)})())
    with pytest.raises(MedBaseUnavailable):
        suggest_attribution(c, "smith")


# --------------------------------------------------------------------------
# Route behaviour
# --------------------------------------------------------------------------
def _configure_medbase(monkeypatch, url: str, token: str) -> None:
    """Settings is a frozen dataclass, so replace the binding rather than mutate.

    app.py does `from .config import settings`, which binds the object at import
    time - patching config.settings would not reach it. Patch app.settings.
    """
    import dataclasses

    import nura_ambient.app as app_mod
    from nura_ambient.config import settings as base

    monkeypatch.setattr(
        app_mod, "settings", dataclasses.replace(base, medbase_url=url, medbase_token=token)
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from nura_ambient.app import create_app

    # default: MedBase unconfigured, so the 503 path is exercised honestly
    _configure_medbase(monkeypatch, "", "")
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as c:
        yield c


def test_attribution_503_when_unconfigured(client):
    r = client.post("/v1/attribution/suggest", json={"query": "smith"}, headers=AUTH)
    assert r.status_code == 503
    assert "MedBase not configured" in r.text


def test_attribution_404_for_unknown_conversation(client, monkeypatch):
    _configure_medbase(monkeypatch, "https://example.invalid", "fmp_x")
    r = client.post("/v1/attribution/suggest",
                    json={"conversation_id": "does-not-exist"}, headers=AUTH)
    assert r.status_code == 404


def test_attribution_422_without_any_text(client, monkeypatch):
    _configure_medbase(monkeypatch, "https://example.invalid", "fmp_x")
    r = client.post("/v1/attribution/suggest", json={}, headers=AUTH)
    assert r.status_code == 422


def test_attribution_unreachable_medbase_is_502_not_empty(client, monkeypatch):
    _configure_medbase(monkeypatch, "https://example.invalid", "fmp_x")

    def boom(self, tool, arguments=None):
        raise MedBaseUnavailable("MedBase HTTP 502")

    monkeypatch.setattr(MedBaseClient, "call", boom)
    r = client.post("/v1/attribution/suggest", json={"query": "smith"}, headers=AUTH)
    assert r.status_code == 502
    assert "unavailable" in r.text.lower()
