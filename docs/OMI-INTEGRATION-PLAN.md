# OMI → NURA integration plan
**Target stack:** NURA ERP (MedBase) · Whisper STT · DeepSeek LLM · own fine-tuned medical LLM · self-hosted on bare metal
**Repo:** `BasedHardware/omi` · HEAD `2391c059d90cf9dbd6bb63e6d4f2f17fc23c6c20` · MIT · 18,453 files / 1.5 GB
**Cloned:** `/opt/data/vendor/omi`
**Written:** 2026-10-17

---

## 0. The headline, before any code

Two of your four requirements are **cheap** and one is **expensive**, and the split is sharp:

| Requirement | Difficulty | Why |
|---|---|---|
| Whisper STT | **Easy** | The provider ABC is 2 methods; Parakeet is a working open-model template |
| DeepSeek LLM | **Easy** | `OpenAICompatibleProviderConfig` exists; OpenRouter is already wired the same way |
| Fine-tuned medical LLM | **Easy** | Same shape as DeepSeek — it is another OpenAI-compatible endpoint |
| **Firestore → our Postgres** | **Expensive** | **1,164 Python files** touch firestore/firebase |

So the model and speech layers are a wiring job. **The storage layer is a re-platform.** That asymmetry should drive the plan, not the marketing.

---

## 1. What the repo actually contains

```
app/          Flutter mobile app           mcp/       first-party MCP server (4 files)
backend/      Python FastAPI               sdks/      device, python, python-cli,
desktop/      desktop client                          react-native, rust, swift
web/          web client                   plugins/   chatgpt, composio, hume-ai, multion
omi/          core + firmware              omiGlass/  glasses firmware
infrastructure/ + helm charts              contracts/ + contract_tests
```

Firmware is real and included (`omi/firmware/`, devkit scripts). This is a full device ecosystem, not a wrapper.

---

## 2. STT — Whisper drops in cleanly

**The contract** (`backend/utils/stt/pre_recorded.py:83`):

```python
class PrerecordedSTTProvider(ABC):
    @abstractmethod
    def transcribe_url(self, audio_url, speakers_count=None, attempts=0,
                       return_language=False, diarize=True, language=None,
                       keywords=None) -> ... : ...

    @abstractmethod
    def transcribe_bytes(self, audio_bytes, sample_rate=16000, diarize=True,
                         attempts=0, encoding=None, channels=1, language=None,
                         return_language=False, keywords=None) -> ... : ...
```

**Two methods. That is the whole surface.**

**The selector** (`backend/utils/stt/streaming.py:90`):

```python
class STTService(str, Enum):
    deepgram = "deepgram"
    modulate = "modulate"
    parakeet = "parakeet"
    soniox  = "soniox"
```

**The open-model precedent** — `ParakeetPrerecordedProvider` (line 1027) is a 48-line class that does nothing but forward the two calls to `parakeet_prerecorded()` / `parakeet_prerecorded_from_bytes()`. **Whisper is exactly this shape.**

### Work items

1. New file `backend/utils/stt/whisper.py`:
   - `whisper_transcribe_url(...)` / `whisper_transcribe_bytes(...)`
   - Backend target: `faster-whisper` locally (CTranslate2, no PyTorch dependency, CPU is fine) on our own metal, or the Whisper API during transition
   - **Diarisation**: Whisper does not diarise. OMI's contract takes `diarize: bool` and expects speaker-labelled output. Either (a) run a local diariser (`pyannote.audio`) alongside and merge, or (b) return single-speaker segments and let OMI's existing `speaker_clustering.py` work on them. **Option (b) is honest and cheap; say so in the code comment so nobody assumes diarisation happened.**
2. `class WhisperPrerecordedProvider(PrerecordedSTTProvider)` forwarding to those two.
3. Add `whisper = "whisper"` to `STTService`, plus `get_model_name` → `'whisper_streaming'` (or `_prerecorded`).
4. Register the provider token wherever `provider_is_enabled(PARAKEET_PROVIDER, ...)` is defined and gate it the same way.
5. Select it: `STT_PRERECORDED_MODEL=whisper` (67 call sites read this).

**Env:** `STT_PRERECORDED_MODEL=whisper` · optional `WHISPER_BASE_URL`, `WHISPER_MODEL=large-v3`, `WHISPER_COMPUTE=int8`

**Note:** `DEEPGRAM_SELF_HOSTED_PROVIDER` already exists in the codebase (15 refs) — they anticipated self-hosted ASR, so this path is not foreign to the architecture.

**Streaming vs prerecorded:** `PrerecordedSTTProvider` handles uploaded audio. Live streaming is a separate `STTSocket(ABC)` (`socket.py:39`) with per-vendor implementations (`SafeDeepgramSocket`, `SafeSonioxSocket`, `ParakeetWebSocketSocket`). **Do Whisper prerecorded first.** A live Whisper socket needs chunked inference and is a materially harder job — don't let it block phase 1.

---

## 3. LLM — DeepSeek and our medical model are the same swap

`backend/utils/llm/providers.py` already has:

```python
class OpenAICompatibleProviderConfig:      # line 34
    base_url: Optional[str] = None
    ...
    # OpenRouter already configured:
    base_url="https://openrouter.ai/api/v1"  # line 49
```

And `backend/utils/llm/model_config.py` is documented as **"the source of truth for feature → (model, provider) routing"** with `get_llm(feature)`, `get_model(feature)`, `get_provider(feature)`.

### Work items

1. Add a provider entry:
   - DeepSeek → `base_url="https://api.deepseek.com/v1"`, key `DEEPSEEK_API_KEY`
   - Medical model → `base_url="http://127.0.0.1:<port>/v1"` on our bare metal (vLLM or llama.cpp server), no external key
2. Map features to them in `model_config.py`. **Do not map everything at once** — route the highest-volume, lowest-risk features first (titles, topic labels, action-item normalisation) and keep the careful ones (medical note synthesis) on a model we have evaluated.
3. Keep the gateway path intact: `gateway_client.py` / `gateway_byok.py` (BYOK) already exists, so a provider-scoped key flows through without touching routing for other features.

**⚠ The one real trap here:** `model_config.py` carries a `validate_no_prohibited_company_paid_models` guard and "pinned features" that ignore env overrides. If a feature is pinned, setting an env var will silently do nothing. **Check `pinned` for each feature before assuming a model swap took effect** — verify by reading the response's `model` field, not by trusting the config edit.

---

## 4. Medical LLM — the same lane, plus the part that is actually hard

Wiring it is the same as DeepSeek. What is *not* the same:

- **A fine-tuned model is not automatically a safe clinical model.** It needs an eval harness before it touches note synthesis. We already have the pieces: the clinical regression-suite skill and `MedAgentBench` (★339, MIT) as an external reference.
- **Keep the `jev_client.py` pattern in mind** — OMI already has a cheap-judge escalation module. For clinical work the useful shape is: cheap model drafts, a stronger/validated model checks anything it is unsure about. That maps directly onto our approval-gated doctrine.
- **Output guard must be mechanical, not prompt-based.** Same rule as the field agent: a regex/refusal layer, not "please be careful" in the system prompt.

---

## 5. NURA ERP (MedBase) — integrate through MCP, not through the DB

**Do not couple OMI's schema to MedBase's.** The clean seam already exists.

`mcp/src/mcp_server_omi/server.py` exposes 8 tools (`OmiTools`, line 310):

```
GET_MEMORIES            SEARCH_MEMORIES        CREATE_MEMORY
DELETE_MEMORY           EDIT_MEMORY
GET_CONVERSATIONS       GET_CONVERSATION_BY_ID SEARCH_CONVERSATIONS
```

That is a **memory and conversation interface** — exactly the right boundary. MedBase stays the system of record; OMI is a capture-and-recall surface in front of it.

### Recommended seam

```
Omi device ──BLE──▶ OMI backend ──▶ MCP (8 memory/conversation tools) ──▶ NURA agent
                                                                          │
                                                             MedBase (Perfex fork)
                                                             · patients / encounters
                                                             · phi_audit hash-chain
                                                             · login_guard
```

- The NURA agent (Hermes) calls the OMI MCP server for `SEARCH_CONVERSATIONS` / `GET_MEMORIES`.
- Anything written to a **patient record** goes through MedBase's own clinical modules and its `phi_audit` chain — never straight from OMI.
- This preserves the rule already in force: **MedBase is the system of record; nothing else writes clinical data.**

---

## 6. The storage problem — and the two honest paths

**1,164 Python files touch firestore/firebase.** Memory retrieval also uses Pinecone. That is the reason "self-hosted OMI" is not actually self-hosted.

### Path A — Fork and migrate (thorough, slow)
Keep OMI's backend, swap Firestore for a Firestore-compatible shim against Postgres, and Pinecone for our Qdrant.
- **Cost:** large. 1,164 files is not a find-and-replace; the Firestore API is a document store with its own query semantics.
- **Benefit:** stays on their release train.
- **Realistic timing:** months, not weeks.

### Path B — Replace the backend, keep the device (recommended)
Use OMI as the **capture layer only**: firmware, Flutter app, device SDKs (all MIT), and its **MCP contract** as the interface shape. Replace the Python backend with our own services on bare metal.

```
Omi pendant (firmware, MIT) ──▶ our ingest service (FastAPI)
                                   ├─ Whisper (faster-whisper, local)
                                   ├─ DeepSeek → then medical LLM (local vLLM)
                                   ├─ Postgres (transcripts, audio refs)
                                   ├─ Qdrant (semantic memory)
                                   └─ MCP server exposing the same 8 tools
                                          └─▶ Hermes → MedBase
```

- **We already own every box in that diagram** except the ingest and MCP services.
- **Cost:** days for the ingest + MCP server; we are not porting 1,164 files, we are writing a small service.
- **Benefit:** genuine sovereignty, PHI never leaves our metal, and we control the model lane outright.
- **Cost of Path B:** we are off OMI's release train for the backend and must follow their *device/protocol* changes ourselves.

**Recommendation: Path B.** The thing that makes OMI valuable to us is the **hardware and the client** — a real pendant, a real Flutter app, real firmware, all MIT. The Python backend is the part that fights us, and it is the part we can most cheaply replace with our own.

---

## 7. Phased plan

**Phase 0 — prove the seam (1–2 days)**
Read `mcp/src/mcp_server_omi/server.py` end to end; run it against a stub; confirm the 8 tools' exact request/response shapes. Everything downstream depends on this contract.

**Phase 1 — Whisper in (3–5 days)**
`whisper.py` + provider class + enum + `STT_PRERECORDED_MODEL=whisper`. Prove it on a recorded sample against the existing pipeline. **Accept single-speaker output initially; document that diarisation is not happening.**

**Phase 2 — DeepSeek in (2–3 days)**
Provider config + per-feature routing in `model_config.py`, starting with low-risk features. **Verify by reading `model` in the response, not the config.**

**Phase 3 — decide the backend (1 week of analysis)**
Prototype the Path B ingest service for one device. Measure what we would actually lose versus Path A. Decide with numbers, not preference.

**Phase 4 — medical LLM (after 1–3, gated)**
Only after the eval harness exists. This is the one step where "it works" is not the bar — "it fails safely and provably" is.

---

## 8. Open questions that need your call

1. **Which OMI hardware?** The CV1 pendant buffers audio offline (per the omi-collector project), which is what makes local capture possible. Confirm we are buying pendant, glasses, or both.
2. **Where does capture physically happen?** Any OMI deployment in a clinic records PHI indiscriminately — including people who did not consent. That is a policy decision, not an engineering one, and it gates whether this is a clinical tool or a founder tool.
3. **Bare metal spec.** Whisper `large-v3` wants ~10 GB VRAM for comfortable real-time; a medical LLM wants a GPU of its own. Which host carries this?
4. **MedBase write authority.** I would keep OMI strictly read/capture and let MedBase own all writes. Confirm you agree before I build against it.

---

### Local clone
`/opt/data/vendor/omi` — HEAD `2391c059d90cf9dbd6bb63e6d4f2f17fc23c6c20`, 18,453 files, 1.5 GB, shallow (`--depth 1`)


---

## 9. PHASE 0 RESULT — the MCP server is a thin HTTP client (this changes the plan)

Read `mcp/src/mcp_server_omi/server.py` in full. Line 68:

```python
base_url = os.getenv("OMI_API_BASE_URL", "https://api.omi.me/v1/mcp/")
```

The entire MCP server does nothing but issue HTTP calls against that base URL with
`Authorization: Bearer <OMI_API_KEY>`. It holds no state, no schema, no database
knowledge. **It is an adapter, not a backend.**

### Therefore the backend contract is 8 endpoints, not 1,164 files

| Method | Path | Params |
|---|---|---|
| GET | `{base}memories` | `limit`, `offset` |
| GET | `{base}memories/search` | `query`, `limit` |
| POST | `{base}memories` | `content`, `category` |
| DELETE | `{base}memories/{id}` | — |
| PUT | `{base}memories/{id}` | `content` |
| GET | `{base}conversations` | `limit`, `offset`, `start_date`, `end_date`, `categories` |
| GET | `{base}conversations/{id}` | — (returns transcript segments) |
| GET | `{base}conversations/search` | `query`, `limit`, `start_date`, `end_date` |

**Date-format trap, documented in their own code:** `/conversations/search` parses
`YYYY-MM-DD` and returns an opaque HTTP 400 on anything else. The MCP client validates
before sending so the error names the bad argument. Our reimplementation must accept
that same format or we silently break the client.

### Why this collapses the estimate

Path B no longer means "replace a Firestore-backed monolith." It means
**implement 8 REST endpoints over Postgres + Qdrant**, then point `OMI_API_BASE_URL`
at it. The 1,164 Firestore files never enter the picture because we are not running
that backend at all.

Revised effort for Path B's server: **days.** The catalogue is:

```
GET    /memories            -> Postgres SELECT (limit/offset)
GET    /memories/search     -> Qdrant ANN over memory embeddings
POST   /memories            -> Postgres INSERT + Qdrant upsert
DELETE /memories/{id}       -> soft delete both stores
PUT    /memories/{id}       -> Postgres UPDATE + re-embed
GET    /conversations       -> Postgres, filtered by date + categories
GET    /conversations/{id}  -> Postgres + transcript segments
GET    /conversations/search-> Qdrant ANN over transcript/chunk embeddings
```

### Revised recommendation

**Path B is now clearly correct, and cheap.** OMI supplies the device, the Flutter
client, the firmware, and a clean MCP adapter — all MIT. We supply the 8 endpoints
against infrastructure we already run. Nothing about PHI leaves our metal, and the
STT/LLM swaps (Whisper, DeepSeek, medical model) happen in *our* ingest service where
we control them completely rather than fighting `model_config.py` pinning.

The one thing we inherit as an obligation: **we must track OMI's client-side and
protocol changes ourselves**, because we are off their backend release train.
