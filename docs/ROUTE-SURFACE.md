# Route surface of BasedHardware/omi

Extracted 2026-10-17 from HEAD `2391c059d90cf9dbd6bb63e6d4f2f17fc23c6c20` by parsing
the FastAPI decorators in `backend/routers/`. Full table in
`contracts/omi-routes.json`.

## Headline numbers

```
712 routes · 117 top-level domains · 113 router modules
```

| Method | Count |
|---|---|
| GET | 302 |
| POST | 271 |
| PATCH | 63 |
| DELETE | 56 |
| PUT | 14 |
| WEBSOCKET | 5 |
| api_route | 1 |

## Three nested surfaces

This distinction matters more than the total, because "how big is the API" has
three different answers depending on what you are building.

| Surface | Size | What it is |
|---|---|---|
| Full backend | **712** | Everything the mobile app, desktop client, web app, admin console and billing touch |
| Agent facade | **~34** | Routes under `/v1/mcp/` |
| Packaged MCP tools | **8** | What `mcp/src/mcp_server_omi/server.py` actually exposes |

The backend already serves **34** MCP routes. Their MCP server packages **8** of
them. So the agent-facing surface is 4x what the server exposes, and the full API
is 90x.

## Weight is concentrated

```
 87  /users              18  /integrations     9  /calendar
 51  /conversations      17  /action-items     7  /stripe
 35  /apps               12  /task-integrations 7  /speaker-tag-prompts
 34  /dev                12  /admin            7  /import
 34  /mcp                10  /payments         7  /frame-requests
 26  /desktop            10  /messages         6  /phone
 24  /v3                 10  /candidates       6  /telemetry
 20  /goals              10  /announcements    6  /.well-known
                          9  /tools            6  /task-intelligence
```

Top five domains = 241 of 712, about a third of the API.

## The endpoints that matter for our build

**Audio in - exactly one route on the whole backend:**

```
POST /v1/stt/transcribe
```

Whisper goes here. No fan-out, no ambiguity.

**Capture path - three of the 51 conversation routes:**

```
POST /v1/conversations
POST /v1/conversations/from-segments
POST /v1/conversations/{id}/finalize
```

**Device boot - one route:**

```
GET /updates/latest        (POST /updates/releases + PATCH .../promote are admin)
```

**Device-relevant `/users` routes are only six** of that domain's 87:

```
POST   /v1/users/fcm-token
GET    /v1/users/onboarding
PATCH  /v1/users/onboarding
POST   /v1/users/store-recording-permission
GET    /v1/users/store-recording-permission
DELETE /v1/users/store-recording-permission
```

## Two hard couplings to Google

### 1. Identity is Firebase

```
POST /v1/auth/token              POST /v1/auth/callback/apple
GET  /v1/auth/authorize          GET  /v1/auth/callback/google
POST /v1/auth/local-dev/custom-token
```

The identity primitive is `get_current_user_uid`, used by **23 routers**, against
`get_current_user_id` in 8. A Firebase UID is the user key throughout, so
replacing auth means minting our own UIDs while keeping the contract shape.

### 2. Async work is Google Cloud Tasks

Six routers gate on OIDC verification of Cloud Tasks:

```
verify_cloud_tasks_oidc
verify_listen_finalization_cloud_tasks_oidc
verify_audio_merge_cloud_tasks_oidc
verify_account_deletion_cloud_tasks_oidc
verify_followup_task
verify_worker
```

That is the audio-merge pipeline, listen finalisation, account deletion and
follow-up jobs running as GCP workers. Replacing it needs our own queue.
**This is the invisible one** - it does not appear in any route count, and its
failure mode is transcripts that simply never finalise.

## Two different date behaviours

Not an inconsistency we introduced - it exists upstream:

- `/conversations/search` sends `%Y-%m-%d` and the backend 400s opaquely on
  anything else. The client validates first to name the bad argument.
- `/v1/mcp/conversations` sends full ISO timestamps, and pushes `end_date` to
  `23:59:59` so the whole final day is included.

Our implementation validates to `date` on entry and stores ISO internally,
which satisfies both callers.

## What full parity would cost

| Scope | Routes | Realistic |
|---|---|---|
| Device records, transcribes, syncs | ~8 | days |
| Agent reads conversations, memories, action items | ~34 | 1-2 weeks |
| Flutter app fully functional | ~150-200 | months |
| Full parity including plugins, billing, admin, desktop | 712 | not a target |

Full parity is not a goal. The device and the agent are what we need; the plugin
marketplace, Stripe billing and the desktop client are things we would rather
replace with our own.
