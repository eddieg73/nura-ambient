# nura-ambient

Ambient capture and memory backend for NURA wearables. A self-hosted replacement
for the OMI cloud pipeline: the device and client stay OMI's (MIT); the server is
ours, running on our own metal.

**Status: working contract surface, plus a live MedBase read-only integration.**
28/28 tests pass. The 8-route OMI-compatible facade is implemented over SQLite with
a stub transcriber. MedBase attribution is wired and verified against the live MCP
(endpoint, 46 tools). Whisper and Qdrant are wired but not yet provisioned. Nothing
here is deployed.

## Why this exists

OMI is an open-source AI wearable ecosystem, but "self-hosted OMI" is not
sovereign. Its `backend/.env.offline.template` sets `PROVIDER_MODE=offline`,
which its own feature-flag registry describes as a **local offline provider
harness, not a product flag** - it swaps in fake providers for tests. The real
backend requires Google Cloud with Firebase and Firestore, Redis and Pinecone,
so transcripts and audio still land in Google's infrastructure.

For a covered healthcare product that is not usable. This repository replaces
the server so PHI never leaves our metal.

## The contract

OMI's own MCP server (`mcp/src/mcp_server_omi/server.py`) is a thin HTTP client:

```python
base_url = os.getenv("OMI_API_BASE_URL", "https://api.omi.me/v1/mcp/")
```

It holds no state and no schema. So the backend surface we must match is HTTP
routes, not an internals port. See `docs/ROUTE-SURFACE.md` for the full analysis:
OMI's backend is **712 routes across 117 domains**, of which **~34** are the
agent-facing `/v1/mcp` facade and **8** are what their packaged MCP server
exposes as tools.

This service currently implements the 8-tool facade plus the ingest path and the
single audio-in route.

## Routes implemented

```
GET    /health
GET    /v1/mcp/memories              categories[] limit offset
GET    /v1/mcp/memories/search       query limit
POST   /v1/mcp/memories              content category
PATCH  /v1/mcp/memories/{id}         content
DELETE /v1/mcp/memories/{id}
GET    /v1/mcp/conversations         start_date end_date categories[] limit offset
GET    /v1/mcp/conversations/search  query limit start_date end_date
GET    /v1/mcp/conversations/{id}    (returns transcript segments)
POST   /v1/mcp/conversations         title category segments[]   <- ingest
POST   /v1/stt/transcribe            raw audio body             <- Whisper lands here
POST   /v1/attribution/suggest       query | conversation_id    <- MedBase, read-only
```

All routes except `/health` require `Authorization: Bearer <key>`.

## Two behaviours copied on purpose

**Categories fail closed.** `MemoryCategory` (9 values) and `ConversationCategory`
(33 values) are *different vocabularies*. Sending `finance` to a memory endpoint
returns 422 rather than ignoring it. OMI's code documents the incident that
taught them this: an unknown category was logged and dropped, the empty list made
the dispatcher omit the filter entirely, and the caller received **every record**
while believing it was filtered. `tests/test_api.py` pins both directions.

**Dates are `yyyy-mm-dd`.** OMI's backend 400s opaquely on anything else, so the
client validates first. We validate up front too and name the bad argument.

## Deliberate honesty points

- The stub transcriber returns `[stub transcript of N bytes]`. It does **not**
  invent speech, so a pipeline test proves audio arrived without pretending
  transcription happened.
- `diarised` is reported explicitly, and Whisper always reports `false`. Whisper
  does not separate speakers; claiming otherwise would cause mis-attributed quotes
  downstream.
- `test_transcribe_stub_is_honest` asserts the `false`.

## Run it

```bash
uv venv .venv && uv pip install --python .venv/bin/python fastapi "pydantic>=2" pytest httpx
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q        # 13 passed
PYTHONPATH=src NURA_AMBIENT_API_KEYS=dev-key \
  .venv/bin/python -m uvicorn nura_ambient.app:app --port 8086
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NURA_AMBIENT_DB` | `nura_ambient.db` | SQLite path (Postgres later, same interface) |
| `NURA_AMBIENT_API_KEYS` | `dev-key` | Comma-separated, so rotation needs no downtime |
| `NURA_AMBIENT_STT` | `stub` | `stub` or `whisper` |
| `NURA_AMBIENT_LLM_BASE_URL` | unset | OpenAI-compatible endpoint (DeepSeek or local medical model) |
| `NURA_AMBIENT_LLM_MODEL` | unset | Model id for that endpoint |
| `NURA_AMBIENT_MEDBASE_URL` | unset | MedBase MCP base (feature off when empty) |
| `NURA_AMBIENT_MEDBASE_TOKEN` | unset | `fmp_` key — read-only routes only |

## MedBase integration — read only, enforced in code

`src/nura_ambient/medbase.py` is a client for Oussama's `flexmcp` MCP inside MedBase
(`medbase-crm` v1.0.1, protocol `2024-11-05`, 46 tools).

It exists for **attribution**: a recording is worthless until someone can say which
patient it belongs to.

```
POST /v1/attribution/suggest      {query?, conversation_id?, limit}
```

**The write boundary is mechanical, not documented.** `MedBaseClient.call()` refuses
any tool outside `READ_ONLY_TOOLS` *before it builds a request*, so this service
cannot write to MedBase even if misconfigured:

```
READ-ONLY (8)   search_patients  list_patients  get_patient  get_patient_by_mrn
                get_patient_summary  list_patient_records
                get_patient_portal_access  search_crm

REFUSED (38)    create_patient  update_patient  add_note  create_reminder
                create_payment  reply_ticket  ... and everything else
```

Two behaviours that matter:

- Attribution always returns `requires_confirmation: true` and never associates a
  recording with a patient. MedBase makes that call.
- A transport failure returns **502**, never an empty candidate list. "No match
  found" and "MedBase is down" are different claims and must not look the same.

Candidates are reduced to `patient_id, mrn, display_name, email` — no date of birth,
address or phone, because attribution does not need them and the response may be
logged.

Verified live: write tools blocked, `search_patients` returns candidates, and the
reduced field set holds.

## Roadmap

1. **Whisper** - `faster-whisper`, local, CPU-capable. Slots into `/v1/stt/transcribe`.
2. **Qdrant** - replace the naive `LIKE` search with ANN over the same routes.
3. **Postgres** - swap `store.py`; the API never learns which backend it talks to.
4. **LLM lane** - DeepSeek first, then a local fine-tuned medical model, behind an eval gate.
5. **Async workers** - OMI uses Google Cloud Tasks for audio merge, listen
   finalisation and follow-up. That dependency must be replaced with our own queue
   or transcripts silently never finalise.
6. **MedBase seam** - read/capture only. MedBase owns every clinical write and its
   `phi_audit` chain.

## Scope boundary

This service is **capture and memory**, not clinical authority. It does not decide
anything, does not write to patient records, and does not synthesise clinical
notes. Any clinical use requires a recording policy first: an always-on ambient
recorder captures PHI indiscriminately, including people who did not consent.

## Licence

Apache-2.0. See `NOTICE` for attribution: the device protocol, client behaviour
and MCP contract derive from `BasedHardware/omi` (MIT, Copyright (c) 2024 Based
Hardware Contributors). No OMI source is copied into this repository.
