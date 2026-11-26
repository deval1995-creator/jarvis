"""Speech models, loaded once per process and shared by every connection.

- Whisper large-v3-turbo on the GPU (float16): ~0.2 s per sentence on an RTX 3070.
- Kokoro (ONNX) on the GPU: ~0.2 s for a short sentence, ~0.5 s for a long one
  (the CPU took ~1.8 s for a long one, too slow for the 2.5 s first-audio target).
"""

from __future__ import annotations

import logging
import os
import threading

from voice import cuda

cuda.add_dll_directories()

from pipecat.services.kokoro import tts as kokoro_tts  # noqa: E402
from pipecat.services.whisper.stt import Model, WhisperSTTService  # noqa: E402
from pipecat.transcriptions.language import Language  # noqa: E402

log = logging.getLogger(__name__)

WHISPER_MODEL = os.environ.get("JARVIS_WHISPER_MODEL", Model.LARGE_V3_TURBO.value)
WHISPER_DEVICE = os.environ.get("JARVIS_WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE = os.environ.get("JARVIS_WHISPER_COMPUTE", "float16" if WHISPER_DEVICE == "cuda" else "int8")
VOICE = os.environ.get("JARVIS_VOICE", "bm_george")
TTS_DEVICE = os.environ.get("JARVIS_TTS_DEVICE", "cuda")

_lock = threading.Lock()
_whisper: dict[tuple, object] = {}
_kokoro: dict[tuple, object] = {}
_KokoroClass = kokoro_tts.Kokoro


def _shared_kokoro(model_path: str, voices_path: str):
    with _lock:
        key = (model_path, voices_path)
        if key not in _kokoro:
            import onnxruntime as rt

            rt.set_default_logger_severity(3)  # its CUDA notes are noise
            providers = ["CPUExecutionProvider"]
            if TTS_DEVICE == "cuda":
                providers.insert(0, "CUDAExecutionProvider")
            session = rt.InferenceSession(model_path, providers=providers)
            if TTS_DEVICE == "cuda" and "CUDAExecutionProvider" not in session.get_providers():
                log.warning("Kokoro could not use the GPU; speaking from the CPU (slower)")
            _kokoro[key] = _KokoroClass.from_session(session, voices_path)
        return _kokoro[key]


kokoro_tts.Kokoro = _shared_kokoro  # KokoroTTSService builds one per connection otherwise


class SharedWhisperSTTService(WhisperSTTService):
    def _load(self):
        key = (self._settings.model, self._device, self._compute_type)
        with _lock:
            if key not in _whisper:
                super()._load()
                _whisper[key] = self._model
            self._model = _whisper[key]


def make_stt() -> SharedWhisperSTTService:
    return SharedWhisperSTTService(
        device=WHISPER_DEVICE,
        compute_type=WHISPER_COMPUTE,
        settings=SharedWhisperSTTService.Settings(
            model=WHISPER_MODEL, language=Language.EN
        ),
    )


def make_tts(**kwargs) -> kokoro_tts.KokoroTTSService:
    return kokoro_tts.KokoroTTSService(settings=kokoro_tts.KokoroTTSService.Settings(voice=VOICE), **kwargs)


def preload() -> None:
    """Load (and download, the first time) both models before anyone talks."""
    log.info("loading Whisper %s on %s and Kokoro voice %s", WHISPER_MODEL, WHISPER_DEVICE, VOICE)
    make_stt()
    make_tts()
    # The first run of each model is slow (GPU kernels load); do it now, not on the first question.
    import numpy as np

    for whisper in _whisper.values():
        list(whisper.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
    for kokoro in _kokoro.values():
        kokoro.create("Ready.", voice=VOICE, lang="en-us")
    log.info("speech models ready")
