# Instructions for Oussama

**To:** Oussama Benakka — oussama@nuratech.ai
**From:** Eddie / NURATECH
**Date:** 2026-10-17
**Repo:** `https://github.com/eddieg73/nura-ambient` (public — no invitation needed to read)
**Instructions:** `https://github.com/eddieg73/nura-ambient/blob/main/docs/FOR-OUSSAMA.md`
**Subject:** ambient capture backend — what it is, what it is not, and what I need from you

---

## 1. Why this exists, in one paragraph

We want to use OMI wearable pendants (an open-source AI recorder, MIT licensed) in our
environment. OMI's own backend requires Google Cloud with Firebase and Firestore, plus
Redis and Pinecone, so every transcript and audio clip lands in Google's infrastructure.
Their `PROVIDER_MODE=offline` flag is not a self-hosting mode — their own feature-flag
registry calls it "a local offline provider harness, not a product flag"; it swaps in
fake providers for tests. For a covered product that is unusable. So we keep OMI's
device and mobile client and replace the server with our own, running on our metal.
This repo is that server.

**This service is capture and memory only. It has no clinical authority.** It does not
write to patient records, does not decide anything, and does not generate clinical
notes. MedBase stays the system of record. That boundary is the most important line in
this document — see section 5.

---

## 2. Status — read this before you plan anything

| Thing | State |
|---|---|
| 8-route OMI MCP facade + ingest + audio-in | **Working**, 13/13 tests pass |
| Storage | SQLite. Postgres comes later behind the same interface |
| Transcription | **Stub.** Returns `[stub transcript of N bytes]`. Whisper is not provisioned |
| Semantic search | Naive SQL `LIKE`. Qdrant not wired |
| LLM lane | Config keys exist, nothing calls them yet |
| Docker | Dockerfile and compose are written but **UNBUILT** — no Docker daemon where they were authored |
| Deployment | **Nothing is deployed anywhere** |

If someone tells you this is running, they are wrong. Check `docker ps` yourself.

---

## 3. Quick start

```bash
git clone git@github.com:eddieg73/nura-ambient.git
cd nura-ambient

uv venv .venv --python /usr/bin/python3
uv pip install --python .venv/bin/python fastapi "pydantic>=2" pytest httpx

# verify before you change anything
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
# expect: 13 passed

# run it
PYTHONPATH=src NURA_AMBIENT_API_KEYS=dev-key \
  .venv/bin/python -m uvicorn nura_ambient.app:app --host 127.0.0.1 --port 8086
```

Then:

```bash
curl -s localhost:8086/health
curl -s -H "Authorization: Bearer dev-key" localhost:8086/v1/mcp/memories
```

---

## 4. How the pieces fit

```
OMI pendant ──BLE──▶ OMI mobile app ──HTTPS──▶ nura-ambient ──▶ Postgres / Qdrant
                                                      │
                                                      └─▶ MCP facade ──▶ NURA agent
                                                              │
                                                              └─▶ MedBase (READ only)
```

```
src/nura_ambient/
  config.py     env-driven settings, nothing hardcoded
  store.py      ALL persistence. SQLite today. This is the only file that changes
                when we move to Postgres.
  stt/whisper.py  transcription backends behind a two-method contract
  app.py        the HTTP routes and the auth dependency
tests/test_api.py  contract tests — see section 8, do not delete these
```

The OMI mobile client only cares about `OMI_API_BASE_URL` and a bearer token. Point it
at us and the client works unchanged. That is the whole design.

---

## 5. The MedBase boundary — read twice

**MedBase owns every clinical write and its `phi_audit` hash chain. This service must
never insert, update or delete a patient record.**

Allowed:
- MedBase calls us and reads a transcript to attach to an encounter the clinician opened
- MedBase stores the `conversation_id` as a foreign reference
- MedBase writes its own audit row for having read it

Not allowed, ever:
- This service writing to `patients`, `encounters`, `clinical` or `phi_audit`
- This service holding PHI in its own tables without an audit trail of who read it
- PHI in logs, in error messages, or in any LLM prompt that leaves our metal

If you think a write is needed, that is a design conversation, not a commit.

---

## 6. The contract

Three things are fixed because an OMI client depends on them.

### 6.1 Routes

```
GET    /health
GET    /v1/mcp/memories              ?categories=&limit=&offset=
GET    /v1/mcp/memories/search       ?query=&limit=
POST   /v1/mcp/memories              {content, category}
PATCH  /v1/mcp/memories/{id}         {content}
DELETE /v1/mcp/memories/{id}
GET    /v1/mcp/conversations         ?start_date=&end_date=&categories=&limit=&offset=
GET    /v1/mcp/conversations/search  ?query=&limit=&start_date=&end_date=
GET    /v1/mcp/conversations/{id}    → includes transcript segments
POST   /v1/mcp/conversations         {title, category, segments[]}   ← ingest
POST   /v1/stt/transcribe            raw audio body → segments       ← Whisper here
```

Everything except `/health` needs `Authorization: Bearer <key>`.

### 6.2 Two separate category vocabularies — do not merge them

```
MemoryCategory (9):       core hobbies lifestyle interests habits work skills learnings other
ConversationCategory (33): personal education health finance legal philosophy spiritual
                           science entrepreneurship parenting romance travel inspiration
                           technology business social work sports politics literature
                           history architecture music weather news entertainment
                           psychology real design family economics environment other
```

Only `work` and `other` appear in both. Sending `finance` to a memory endpoint is a bug.

### 6.3 Categories fail closed

An unknown category returns **422**, it is not ignored. This is deliberate and I want it
kept. OMI's own code comment records the incident that taught them: they used to log and
drop unknown categories, the empty list meant the dispatcher omitted the filter entirely,
and the caller received **every record in the database** while believing the query was
filtered. In a clinical context that is a data-exposure bug, not a UX bug.

`tests/test_api.py` pins both directions. If you find yourself relaxing those two tests
to make something pass, stop and call me.

### 6.4 Dates are `yyyy-mm-dd`

OMI's backend returns an opaque HTTP 400 on anything else, so we validate on entry and
name the bad argument. Keep that behaviour.

---

## 7. What still needs building — pick in this order

**7.1 Whisper (highest value, smallest change).**
`src/nura_ambient/stt/whisper.py` already has a `WhisperTranscriber` class using
`faster-whisper`. It is not installed and not tested. Set `NURA_AMBIENT_STT=whisper` and
provision it.

Two warnings:
- **Whisper does not separate speakers.** The class reports `diarised: false` on purpose.
  Do not flip that flag to make output look nicer — a consumer that assumes diarisation
  will mis-attribute quotes, and in a clinical record that is the worst failure we have.
  If speaker separation is required, run `pyannote.audio` alongside and merge.
- Whisper `large-v3` wants roughly 10 GB VRAM for comfortable real-time. Pick the model
  size against the host we actually allocate.

**7.2 Qdrant.**
`store.py` currently does `LIKE '%query%'` for both search endpoints. Replace with ANN.
Keep the `Store` method signatures identical so nothing above changes.

**7.3 Postgres.**
Rewrite `store.py` only. The API and tests should not need to change. That containment is
the point of the current layout.

**7.4 LLM lane.**
DeepSeek first, then a local fine-tuned medical model. Both are OpenAI-compatible, so one
client. **The medical model does not go live without an eval harness** — "it answered
correctly twice" is not a basis for clinical use. Anything that drafts text for a
clinician needs the escape hatch (refusal) tested before the happy path.

**7.5 Replace Google Cloud Tasks — the invisible one.**
OMI's backend runs audio merge, listen finalisation, account deletion and follow-up jobs
as Google Cloud Tasks workers. Six of their routers gate on
`verify_cloud_tasks_oidc`. This does not appear in any route count and its failure mode
is **transcripts that simply never finalise** — no error, no alert, just silence. We need
our own queue (Postgres-backed or Redis) before this can be trusted with real recordings.

**7.6 The `RUN_SCHEDULER` question.**
When CarePilot moved to Hostinger the deploy config kept `RUN_SCHEDULER=false` because AWS
was responsible for scheduled jobs. If production traffic is now served from a box with
that still false, the EMR/Solis/GHL/reporting automation may be running nowhere. That is
your box and your call, but check it — it is a silent failure.

---

## 8. Testing rules

- **Do not delete or weaken the category tests.** They encode a real incident.
- **Do not replace the stub transcriber with something that fabricates plausible text.**
  The stub returns `[stub transcript of N bytes]` so a pipeline test proves audio arrived
  without pretending transcription happened. If a test needs "realistic" output, pass it
  explicitly rather than making the stub lie.
- Run `PYTHONPATH=src .venv/bin/python -m pytest tests/ -q` before every commit.
- If you add a route, add a test that asserts the **failure** path too, not just 200.

---

## 9. Security

- API keys live in a 0600 file, never in the repo, never in a compose file that gets
  committed. `NURA_AMBIENT_API_KEYS` accepts a comma-separated list so we can rotate
  without downtime.
- No PHI in logs. Note that `app.py` deliberately does not log request bodies.
- The service binds `127.0.0.1` in the example. Do not publish it directly; it goes
  behind the existing reverse proxy.
- Do not put PHI in an LLM prompt that reaches an external provider.

---

## 10. What I need from you

1. **Which host and which port?** It wants its own container, not sharing CarePilot's.
2. **Postgres or stay on SQLite for the first deployment?** My view: Postgres from the
   start, because migrating later moves data rather than config.
3. **How does MedBase call this?** Direct HTTP from PHP, or through the `flexmcp` MCP
   module you already built? I would prefer MCP — one pattern, already audited.
4. **Who reads the transcript, and is that read audited?** If MedBase attaches a
   transcript to an encounter, `phi_audit` should record the read. Confirm you agree
   before I build the client side.
5. **Recording policy.** This is the real gate. An always-on ambient recorder captures
   PHI indiscriminately, including people who did not consent — patients in the next
   room, staff, family. That is a policy decision that has to exist before any clinic
   deployment, and it is not something either of us can decide in code.

---

## 11. Reference

- `README.md` — what the service is
- `docs/ROUTE-SURFACE.md` — OMI's full 712-route surface, the 34 agent routes, the 8 tools,
  and the two hard Google couplings (Firebase identity, Cloud Tasks async)
- `docs/OMI-INTEGRATION-PLAN.md` — the full analysis and phased plan
- `contracts/omi-routes.json` — the extracted route table, for reference only

Upstream: `github.com/BasedHardware/omi` (MIT). We take the device, the client and the
contract; we do not take their backend. No OMI source is copied into this repo — see
`NOTICE`.

---

## 12. Access and attribution

The repository is **public**, so the link above opens for you with no invitation and no
GitHub account:

    https://github.com/eddieg73/nura-ambient

Read access needs nothing. **Push access needs your GitHub username** — GitHub's API
cannot invite a collaborator by email address, so send the username to eg@nuratech.ai and
it will be added. Do not assume the email alone grants write access; it does not.

IP owner: Eddie Garrido / Garrido Group LLC.
Implementation and integration: Oussama Benakka — oussama@nuratech.ai.

Questions to Eddie directly. If something in this document is wrong, tell me and I will
correct it rather than have you work around it.
