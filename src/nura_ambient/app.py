"""NURA Ambient HTTP API.

Implements the OMI-compatible facade so an OMI client (or our MCP server) can
point `OMI_API_BASE_URL` at us and work unchanged. Mounted under /v1/mcp to
match the routes OMI's backend actually serves.

Two behaviours copied deliberately from OMI's own client, because omitting them
turns a filtered query into an unfiltered one:

1. Categories FAIL CLOSED. An unknown category raises 422 naming the field.
   OMI's comment records the incident: dropping unknown categories left an
   empty list, the dispatcher then omitted the filter, and the caller got
   EVERY record while believing it was filtered.

2. Dates are yyyy-mm-dd. OMI's backend 400s opaquely on anything else, so we
   validate up front and name the bad argument.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import settings
from .medbase import MedBaseClient, MedBaseUnavailable, MedBaseWriteBlocked, suggest_attribution
from .store import Store
from .stt import get_transcriber

# ---------------------------------------------------------------- vocabularies
# NOTE: these are two DIFFERENT vocabularies. Only work/other appear in both.
# Sending a conversation category to a memory endpoint is the exact bug OMI
# hardened against - so it is rejected, not ignored.

MemoryCategory = Literal[
    "core", "hobbies", "lifestyle", "interests", "habits", "work", "skills",
    "learnings", "other",
]

ConversationCategory = Literal[
    "personal", "education", "health", "finance", "legal", "philosophy",
    "spiritual", "science", "entrepreneurship", "parenting", "romance",
    "travel", "inspiration", "technology", "business", "social", "work",
    "sports", "politics", "literature", "history", "architecture", "music",
    "weather", "news", "entertainment", "psychology", "real", "design",
    "family", "economics", "environment", "other",
]


# ---------------------------------------------------------------- auth
def require_key(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token not in settings.api_keys:
        raise HTTPException(status_code=403, detail="invalid API key")
    return token


# ---------------------------------------------------------------- models
class CreateMemoryBody(BaseModel):
    content: str = Field(min_length=1)
    category: MemoryCategory


class EditMemoryBody(BaseModel):
    content: str = Field(min_length=1)


class SegmentBody(BaseModel):
    idx: int | None = None
    text: str = ""
    speaker: str | None = None
    speaker_id: int | None = None
    is_user: bool = False
    start: float | None = None
    end: float | None = None


class AttributionQuery(BaseModel):
    query: str | None = Field(
        default=None,
        description="Text to match against MedBase patients. If omitted, derived from the conversation.",
    )
    conversation_id: str | None = None
    limit: int = Field(default=5, ge=1, le=20)


class CreateConversationBody(BaseModel):
    title: str | None = None
    category: ConversationCategory | None = None
    segments: list[SegmentBody] = Field(default_factory=list)


# ---------------------------------------------------------------- app factory
def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(title="NURA Ambient", version="0.1.0")
    store = Store(db_path or settings.db_path)
    app.state.store = store
    app.state.transcriber = get_transcriber(settings.stt_backend)

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    # ------------------------------------------------------------ health
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "nura-ambient",
            "stt_backend": getattr(app.state.transcriber, "name", "unknown"),
            "store": "sqlite",
        }

    # ------------------------------------------------------------ memories
    @app.get("/v1/mcp/memories")
    def get_memories(
        categories: Annotated[list[MemoryCategory] | None, Query()] = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: str = Depends(require_key),
    ) -> list[dict]:
        return store.list_memories(categories, limit, offset)

    @app.get("/v1/mcp/memories/search")
    def search_memories(
        query: str = Query(min_length=1),
        limit: int = Query(default=10, ge=1, le=100),
        _: str = Depends(require_key),
    ) -> list[dict]:
        return store.search_memories(query, limit)

    @app.post("/v1/mcp/memories", status_code=201)
    def create_memory(body: CreateMemoryBody, _: str = Depends(require_key)) -> dict:
        return store.create_memory(body.content, body.category)

    @app.patch("/v1/mcp/memories/{memory_id}")
    def edit_memory(memory_id: str, body: EditMemoryBody, _: str = Depends(require_key)) -> dict:
        out = store.edit_memory(memory_id, body.content)
        if out is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return out

    @app.delete("/v1/mcp/memories/{memory_id}")
    def delete_memory(memory_id: str, _: str = Depends(require_key)) -> dict:
        if not store.delete_memory(memory_id):
            raise HTTPException(status_code=404, detail="memory not found")
        return {"deleted": True, "id": memory_id}

    # ------------------------------------------------------------ conversations
    @app.get("/v1/mcp/conversations")
    def get_conversations(
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        categories: Annotated[list[ConversationCategory] | None, Query()] = None,
        limit: int = Query(default=20, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: str = Depends(require_key),
    ) -> list[dict]:
        return store.list_conversations(
            start_date.isoformat() if start_date else None,
            end_date.isoformat() if end_date else None,
            categories, limit, offset,
        )

    @app.get("/v1/mcp/conversations/search")
    def search_conversations(
        query: str = Query(min_length=1),
        limit: int = Query(default=10, ge=1, le=100),
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        _: str = Depends(require_key),
    ) -> list[dict]:
        return store.search_conversations(query, limit)

    @app.get("/v1/mcp/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, _: str = Depends(require_key)) -> dict:
        out = store.get_conversation(conversation_id)
        if out is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return out

    @app.post("/v1/mcp/conversations", status_code=201)
    def create_conversation(body: CreateConversationBody, _: str = Depends(require_key)) -> dict:
        """Ingest path. Not part of OMI's 8 MCP tools, but required to capture."""
        return store.create_conversation(
            body.title, body.category, [s.model_dump() for s in body.segments]
        )

    # ------------------------------------------------------------ attribution
    # READ-ONLY. This route exists so a recording can be matched to a patient.
    # It never writes to MedBase: the client refuses any tool outside its
    # read-only allowlist, so the boundary is enforced in code, not in a doc.
    @app.post("/v1/attribution/suggest")
    def attribution_suggest(body: AttributionQuery, _: str = Depends(require_key)) -> dict:
        if not settings.medbase_url or not settings.medbase_token:
            raise HTTPException(
                status_code=503,
                detail="MedBase not configured (set NURA_AMBIENT_MEDBASE_URL and NURA_AMBIENT_MEDBASE_TOKEN)",
            )
        # If a conversation_id is supplied, make sure it exists before spending
        # a call on MedBase - a typo should not read as "no patient found".
        if body.conversation_id and store.get_conversation(body.conversation_id) is None:
            raise HTTPException(status_code=404, detail="conversation not found")

        query = body.query
        if not query and body.conversation_id:
            conv = store.get_conversation(body.conversation_id) or {}
            query = conv.get("title") or ""
            if not query:
                segs = conv.get("segments") or []
                query = " ".join(s.get("text", "") for s in segs[:3]).strip()
        if not query:
            raise HTTPException(status_code=422, detail="provide query or a conversation with text")

        client = MedBaseClient(settings.medbase_url, settings.medbase_token)
        try:
            out = suggest_attribution(client, query, limit=body.limit)
        except MedBaseWriteBlocked as e:
            raise HTTPException(status_code=500, detail=str(e)) from None
        except MedBaseUnavailable as e:
            # Fail loudly. An empty candidate list would read as "no match for
            # this patient", which is a materially different and dangerous claim.
            raise HTTPException(status_code=502, detail=f"MedBase unavailable: {e}") from None

        out["conversation_id"] = body.conversation_id
        return out

    # ------------------------------------------------------------ audio in
    @app.post("/v1/stt/transcribe")
    async def transcribe(request: Request, _: str = Depends(require_key)) -> dict:
        """The ONE audio-in route on OMI's whole backend. Whisper lands here."""
        audio = await request.body()
        if not audio:
            raise HTTPException(status_code=422, detail="empty audio body")
        result = app.state.transcriber.transcribe_bytes(audio)
        return result.as_dict()

    return app


app = create_app()
