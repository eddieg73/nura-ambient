"""Configuration. Everything overridable by environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Where the SQLite store lives. Postgres comes later behind the same interface.
    db_path: str = os.getenv("NURA_AMBIENT_DB", "nura_ambient.db")

    # Auth. The OMI client sends `Authorization: Bearer <key>`; we accept a set.
    # Comma-separated so rotation is possible without downtime.
    api_keys: tuple[str, ...] = tuple(
        k.strip() for k in os.getenv("NURA_AMBIENT_API_KEYS", "dev-key").split(",") if k.strip()
    )

    # STT backend selection. Mirrors OMI's STT_PRERECORDED_MODEL idea.
    stt_backend: str = os.getenv("NURA_AMBIENT_STT", "stub")

    # LLM lane. Both DeepSeek and a local medical model are OpenAI-compatible.
    llm_base_url: str = os.getenv("NURA_AMBIENT_LLM_BASE_URL", "")
    llm_model: str = os.getenv("NURA_AMBIENT_LLM_MODEL", "")


settings = Settings()
