"""Local speech services for the HUD.

STT runs through faster-whisper on the CPU. TTS uses Microsoft Edge's neural
voices over the network; when it is unreachable the renderer falls back to the
installed Windows/browser voice. Wake-word inference uses openWakeWord's local
``hey_jarvis`` model and never sends raw microphone audio off the machine.

Models are loaded lazily so the API starts quickly and remains usable when an
optional dependency or model download is unavailable.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import threading
import time
from typing import Any

from app.core.config import settings

logger = logging.getLogger("jarvis.voice")

VOICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,79}$")
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,8})?$")

_stt_model: Any | None = None
_stt_lock = threading.Lock()
_wake_model: Any | None = None
_wake_lock = threading.Lock()
_wake_predict_lock = threading.Lock()
_wake_model_key: str | None = None

WAKE_SAMPLE_RATE = 16_000
WAKE_FRAME_SAMPLES = 1_280  # 80 ms, the model's native step
WAKE_FRAME_BYTES = WAKE_FRAME_SAMPLES * 2


class VoiceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "voice_error") -> None:
        super().__init__(message)
        self.code = code


def _import_available(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def status() -> dict[str, Any]:
    """Report component availability without loading or downloading models."""
    return {
        "stt": {
            "available": _import_available("faster_whisper"),
            "engine": "faster-whisper",
            "model": settings.whisper_model,
            "device": settings.whisper_device,
            "language": settings.whisper_language,
            "loaded": _stt_model is not None,
        },
        "tts": {
            "available": _import_available("edge_tts"),
            "engine": "edge-tts",
            "voice": settings.tts_voice,
            "language": settings.tts_language,
            "fallback": "browser",
        },
        "wake": {
            "available": _import_available("openwakeword") and _import_available("numpy"),
            "engine": "openWakeWord",
            "model": settings.wake_model,
            "threshold": settings.wake_threshold,
            "sample_rate": WAKE_SAMPLE_RATE,
            "frame_samples": WAKE_FRAME_SAMPLES,
            "loaded": _wake_model is not None,
        },
        "audio": {
            "sample_rate": WAKE_SAMPLE_RATE,
            "channels": 1,
            "format": "pcm_s16le",
        },
        "privacy": (
            "Microphone audio is processed locally. Only the final transcript is sent to the "
            "configured AI provider; wake-word audio never leaves this machine."
        ),
    }


def _get_stt_model() -> Any:
    global _stt_model
    if _stt_model is not None:
        return _stt_model
    with _stt_lock:
        if _stt_model is not None:
            return _stt_model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceError(
                "Speech recognition is not installed. Install faster-whisper on the backend.",
                code="stt_unavailable",
            ) from exc
        logger.info(
            "Loading faster-whisper model %s (%s/%s)",
            settings.whisper_model,
            settings.whisper_device,
            settings.whisper_compute_type,
        )
        started = time.perf_counter()
        try:
            _stt_model = WhisperModel(
                settings.whisper_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )
        except Exception as exc:  # noqa: BLE001
            raise VoiceError(
                f"Could not load the speech model: {exc}",
                code="stt_unavailable",
            ) from exc
        logger.info("Speech model ready in %.1fs", time.perf_counter() - started)
        return _stt_model


def transcribe(data: bytes, *, language: str | None = None) -> dict[str, Any]:
    if not data:
        raise VoiceError("No audio was received.", code="empty_audio")
    if len(data) > settings.max_audio_bytes:
        raise VoiceError("The recording is too large.", code="audio_too_large")
    model = _get_stt_model()
    requested = (language or settings.whisper_language or "en").strip()
    if not LANGUAGE_RE.match(requested):
        requested = settings.whisper_language or "en"
    # Whisper expects a primary language code ("en"), not a UI locale
    # ("en-GB"); passing the locale logs a warning and forces a fallback.
    requested = requested.split("-")[0].lower()
    started = time.perf_counter()
    try:
        segments, info = model.transcribe(
            io.BytesIO(data),
            language=requested,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
            no_speech_threshold=0.55,
            without_timestamps=True,
        )
        collected = list(segments)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Transcription failed: %s", exc)
        raise VoiceError(
            "The recording could not be transcribed. Check the microphone format and try again.",
            code="transcription_failed",
        ) from exc
    text = " ".join(segment.text.strip() for segment in collected if segment.text.strip()).strip()
    duration = float(getattr(info, "duration", 0) or 0)
    return {
        "text": text,
        "empty": not bool(text),
        "language": getattr(info, "language", requested),
        "language_probability": float(getattr(info, "language_probability", 0) or 0),
        "duration": round(duration, 2),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "model": settings.whisper_model,
    }


def _percent(value: int, *, low: int = -100, high: int = 200) -> str:
    clamped = max(low, min(high, int(value)))
    return f"{clamped:+d}%"


async def synthesize(
    text: str,
    *,
    voice: str | None = None,
    language: str | None = None,
    rate: int = 0,
    volume: int = 0,
    timeout: float = 30.0,
) -> tuple[bytes, str]:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        raise VoiceError("There is nothing to speak.", code="empty_text")
    if len(clean) > 2_000:
        clean = clean[:2_000]
    selected_voice = (voice or settings.tts_voice).strip()
    if not VOICE_RE.match(selected_voice):
        selected_voice = settings.tts_voice
    if language and not LANGUAGE_RE.match(language.strip()):
        language = settings.tts_language
    try:
        import edge_tts
    except ImportError as exc:
        raise VoiceError(
            "Neural speech is not installed. Install edge-tts on the backend.",
            code="tts_unavailable",
        ) from exc

    async def run() -> bytes:
        communicate = edge_tts.Communicate(
            clean,
            voice=selected_voice,
            rate=_percent(rate, low=-50, high=100),
            volume=_percent(volume, low=-100, high=100),
        )
        audio = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                audio.extend(chunk["data"])
        return bytes(audio)

    try:
        audio = await asyncio.wait_for(run(), timeout=timeout)
    except TimeoutError as exc:
        raise VoiceError("Speech synthesis timed out.", code="tts_timeout") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("Edge TTS failed: %s", exc)
        raise VoiceError(
            "Neural speech is temporarily unavailable; using the local Windows voice instead.",
            code="tts_unavailable",
        ) from exc
    if not audio:
        raise VoiceError("Speech synthesis returned no audio.", code="tts_unavailable")
    return audio, selected_voice


def _get_wake_model() -> Any:
    global _wake_model, _wake_model_key
    if _wake_model is not None:
        return _wake_model
    with _wake_lock:
        if _wake_model is not None:
            return _wake_model
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise VoiceError(
                "Wake-word detection is not installed. Install openwakeword on the backend.",
                code="wake_unavailable",
            ) from exc
        started = time.perf_counter()
        try:
            _wake_model = Model(
                wakeword_models=[settings.wake_model],
                inference_framework="onnx",
                vad_threshold=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            raise VoiceError(
                f"Could not load the “{settings.wake_model}” wake model: {exc}",
                code="wake_unavailable",
            ) from exc
        _wake_model_key = next(iter(_wake_model.models.keys()), settings.wake_model)
        logger.info("Wake model ready in %.1fs", time.perf_counter() - started)
        return _wake_model


def warm_wake_model() -> None:
    """Load the wake model ahead of the first audio frame."""
    _get_wake_model()


def wake_ready_message() -> dict[str, Any]:
    return {
        "type": "ready",
        "engine": "openWakeWord",
        "model": settings.wake_model,
        "threshold": settings.wake_threshold,
        "sample_rate": WAKE_SAMPLE_RATE,
        "frame_samples": WAKE_FRAME_SAMPLES,
        "format": "pcm_s16le",
    }


def feed_wake_frame(frame: bytes) -> float:
    """Score one exact 80 ms PCM frame. Returns the selected model's score."""
    if len(frame) != WAKE_FRAME_BYTES:
        raise VoiceError("Wake-word frames must be 1280 int16 samples.", code="invalid_frame")
    import numpy as np

    model = _get_wake_model()
    samples = np.frombuffer(frame, dtype=np.int16).copy()
    with _wake_predict_lock:
        scores = model.predict(samples)
    key = _wake_model_key or next(iter(scores.keys()), settings.wake_model)
    return float(scores.get(key, 0.0))


def reset_wake_model() -> None:
    global _wake_model, _wake_model_key
    with _wake_predict_lock:
        if _wake_model is not None and hasattr(_wake_model, "reset"):
            try:
                _wake_model.reset()
            except Exception:  # noqa: BLE001
                _wake_model = None
                _wake_model_key = None


def voice_catalog() -> list[dict[str, str]]:
    """Curated edge-tts names that exist for common HUD languages."""
    return [
        {"id": "en-GB-RyanNeural", "label": "Ryan — British English", "language": "en-GB"},
        {"id": "en-GB-SoniaNeural", "label": "Sonia — British English", "language": "en-GB"},
        {"id": "en-US-GuyNeural", "label": "Guy — American English", "language": "en-US"},
        {"id": "en-US-AriaNeural", "label": "Aria — American English", "language": "en-US"},
        {"id": "en-AU-WilliamNeural", "label": "William — Australian English", "language": "en-AU"},
        {"id": "en-IN-PrabhatNeural", "label": "Prabhat — Indian English", "language": "en-IN"},
    ]
