"""Transcription backends.

Mirrors the shape of OMI's `PrerecordedSTTProvider` two-method contract so the
same call sites work, but this is an independent implementation.

    transcribe_bytes(audio, ...) -> TranscribeResult
    transcribe_url(url, ...)     -> TranscribeResult

`StubTranscriber` is the default: it returns a deterministic marker rather than
inventing speech, so a pipeline test can prove audio reached the transcriber
without pretending transcription happened.

`WhisperTranscriber` is a thin wrapper over faster-whisper and is only
constructed when the dependency is installed AND the backend is selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TranscribeResult:
    segments: list[dict[str, Any]] = field(default_factory=list)
    language: str | None = None
    backend: str = "unknown"
    diarised: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "segments": self.segments,
            "language": self.language,
            "backend": self.backend,
            "diarised": self.diarised,
        }


class StubTranscriber:
    """Deterministic. Never fabricates speech."""

    name = "stub"

    def transcribe_bytes(self, audio_bytes: bytes, **_: Any) -> TranscribeResult:
        return TranscribeResult(
            segments=[{"idx": 0, "text": f"[stub transcript of {len(audio_bytes)} bytes]",
                       "speaker": "SPEAKER_00", "is_user": False}],
            language=None,
            backend=self.name,
            diarised=False,
        )

    def transcribe_url(self, audio_url: str, **_: Any) -> TranscribeResult:
        return TranscribeResult(
            segments=[{"idx": 0, "text": f"[stub transcript of {audio_url}]",
                       "speaker": "SPEAKER_00", "is_user": False}],
            language=None,
            backend=self.name,
            diarised=False,
        )


class WhisperTranscriber:
    """faster-whisper. Imported lazily so the package installs without it.

    NOTE ON DIARISATION: Whisper does not separate speakers. `diarised` is
    reported False rather than silently claiming speaker labels, because a
    consumer that assumes diarisation happened will mis-attribute quotes.
    """

    name = "whisper"

    def __init__(self, model_size: str = "base", compute_type: str = "int8") -> None:
        from faster_whisper import WhisperModel  # noqa: F401  (raises if absent)

        self._model = WhisperModel(model_size, compute_type=compute_type)

    def _run(self, gen) -> TranscribeResult:  # type: ignore[no-untyped-def]
        segs = []
        for i, s in enumerate(gen):
            segs.append({"idx": i, "text": s.text.strip(), "speaker": None,
                         "start": s.start, "end": s.end, "is_user": False})
        lang = getattr(gen, "language", None)
        return TranscribeResult(segments=segs, language=lang, backend=self.name, diarised=False)

    def transcribe_bytes(self, audio_bytes: bytes, **kw: Any) -> TranscribeResult:
        import io

        buf = io.BytesIO(audio_bytes)
        segs, info = self._model.transcribe(buf, **{k: v for k, v in kw.items() if k in
                                                    ("language", "beam_size", "vad_filter")})
        result = self._run(segs)
        result.language = getattr(info, "language", result.language)
        return result

    def transcribe_url(self, audio_url: str, **kw: Any) -> TranscribeResult:
        segs, info = self._model.transcribe(audio_url, **{k: v for k, v in kw.items() if k in
                                                          ("language", "beam_size", "vad_filter")})
        result = self._run(segs)
        result.language = getattr(info, "language", result.language)
        return result


def get_transcriber(backend: str) -> Any:
    if backend == "whisper":
        return WhisperTranscriber()
    return StubTranscriber()
