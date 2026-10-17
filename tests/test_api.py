"""Contract tests for NURA Ambient.

These assert the behaviours we deliberately copied from OMI's client, plus the
ones we deliberately tightened. The category tests are the important ones:
in OMI's history a dropped category filter silently returned EVERY record.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from nura_ambient.app import create_app

KEY = "dev-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------ basics
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "nura-ambient"
    assert body["status"] == "ok"


def test_auth_required(client):
    assert client.get("/v1/mcp/memories").status_code == 401
    assert client.get("/v1/mcp/memories", headers={"Authorization": "Bearer wrong"}).status_code == 403


# ------------------------------------------------------------------ memories
def test_memory_roundtrip(client):
    created = client.post("/v1/mcp/memories",
                          json={"content": "prefers morning clinic slots", "category": "work"},
                          headers=AUTH)
    assert created.status_code == 201
    mid = created.json()["id"]

    listed = client.get("/v1/mcp/memories", headers=AUTH).json()
    assert [m["id"] for m in listed] == [mid]

    edited = client.patch(f"/v1/mcp/memories/{mid}",
                          json={"content": "prefers afternoon clinic slots"}, headers=AUTH)
    assert edited.status_code == 200
    assert "afternoon" in edited.json()["content"]

    assert client.delete(f"/v1/mcp/memories/{mid}", headers=AUTH).status_code == 200
    assert client.get("/v1/mcp/memories", headers=AUTH).json() == []
    # deleting twice is a 404, not a silent success
    assert client.delete(f"/v1/mcp/memories/{mid}", headers=AUTH).status_code == 404


def test_memory_category_filter_actually_filters(client):
    client.post("/v1/mcp/memories", json={"content": "a", "category": "work"}, headers=AUTH)
    client.post("/v1/mcp/memories", json={"content": "b", "category": "hobbies"}, headers=AUTH)
    only_work = client.get("/v1/mcp/memories?categories=work", headers=AUTH).json()
    assert len(only_work) == 1
    assert only_work[0]["category"] == "work"


# ------------------------------------------- the silent-widening regression
def test_memory_category_rejects_conversation_vocabulary(client):
    """`finance` is a CONVERSATION category. Sending it to memories must 422.

    If this ever returns 200 with an unfiltered list, we have reintroduced the
    exact bug OMI's comment describes.
    """
    r = client.get("/v1/mcp/memories?categories=finance", headers=AUTH)
    assert r.status_code == 422
    assert "categor" in r.text.lower()


def test_conversation_category_rejects_memory_vocabulary(client):
    """`learnings` is a MEMORY category only."""
    r = client.get("/v1/mcp/conversations?categories=learnings", headers=AUTH)
    assert r.status_code == 422


def test_bad_memory_category_on_create_is_rejected(client):
    r = client.post("/v1/mcp/memories",
                    json={"content": "x", "category": "finance"}, headers=AUTH)
    assert r.status_code == 422


# ------------------------------------------------------------- conversations
def test_conversation_with_segments(client):
    r = client.post("/v1/mcp/conversations", headers=AUTH, json={
        "title": "intake call",
        "category": "health",
        "segments": [
            {"text": "how long has the cough persisted", "speaker": "SPEAKER_00", "start": 0.0, "end": 2.5},
            {"text": "about three weeks", "speaker": "SPEAKER_01", "start": 2.5, "end": 4.0},
        ],
    })
    assert r.status_code == 201
    cid = r.json()["id"]

    got = client.get(f"/v1/mcp/conversations/{cid}", headers=AUTH).json()
    assert len(got["segments"]) == 2
    assert got["segments"][1]["text"] == "about three weeks"
    # segment order must be stable, not insertion-order dependent
    assert [s["idx"] for s in got["segments"]] == [0, 1]


def test_conversation_missing_is_404(client):
    assert client.get("/v1/mcp/conversations/nope", headers=AUTH).status_code == 404


def test_conversation_date_filter(client):
    client.post("/v1/mcp/conversations", headers=AUTH, json={"title": "today", "category": "work"})
    from datetime import date
    today = date.today().isoformat()
    assert len(client.get(f"/v1/mcp/conversations?start_date={today}", headers=AUTH).json()) == 1
    assert len(client.get("/v1/mcp/conversations?start_date=1999-01-01", headers=AUTH).json()) == 1
    assert client.get("/v1/mcp/conversations?start_date=not-a-date", headers=AUTH).status_code == 422


def test_search(client):
    client.post("/v1/mcp/conversations", headers=AUTH, json={
        "title": "c", "segments": [{"text": "discussed metformin titration"}]})
    hits = client.get("/v1/mcp/conversations/search?query=metformin", headers=AUTH).json()
    assert len(hits) == 1
    assert client.get("/v1/mcp/conversations/search?query=zzzz", headers=AUTH).json() == []


# ------------------------------------------------------------------ transcribe
def test_transcribe_stub_is_honest(client):
    r = client.post("/v1/stt/transcribe", content=b"\x00" * 100, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "stub"
    # must NOT claim diarisation it did not perform
    assert body["diarised"] is False
    assert "stub transcript of 100 bytes" in body["segments"][0]["text"]


def test_transcribe_rejects_empty(client):
    assert client.post("/v1/stt/transcribe", content=b"", headers=AUTH).status_code == 422
