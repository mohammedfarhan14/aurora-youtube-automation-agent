"""Small OpenAI-compatible HTTP bridge for a separately installed Qwen3-TTS runtime.

Install qwen-tts in its own Python 3.12 environment. This bridge deliberately
does not add Torch/Qwen dependencies to Aurora's API or worker images.
"""

from __future__ import annotations

import hmac
import io
import json
import logging
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

logger = logging.getLogger("aurora.qwen_tts_bridge")
MAX_REQUEST_BYTES = 100_000
MAX_AUDIO_TOKENS = 480
MODEL_ID = os.getenv(
    "QWEN_TTS_MODEL",
    "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
).strip()
LANGUAGE = os.getenv("QWEN_TTS_LANGUAGE", "English").strip()
DEVICE = os.getenv("QWEN_TTS_DEVICE", "auto").strip().lower()
SUBTALKER_DOSAMPLE = (
    os.getenv("QWEN_TTS_SUBTALKER_DOSAMPLE", "false").strip().lower() == "true"
)
API_KEY = os.getenv("QWEN_TTS_API_KEY", "").strip()
HOST = os.getenv("QWEN_TTS_HOST", "127.0.0.1").strip()
PORT = int(os.getenv("QWEN_TTS_PORT", "8880"))

_model: Any = None
_model_lock = threading.Lock()


def _load_model() -> Any:
    """Load one small CustomVoice checkpoint, preferring CUDA when requested."""
    import torch
    from qwen_tts import Qwen3TTSModel

    candidates = [DEVICE]
    if DEVICE == "auto":
        candidates = ["cuda:0", "cpu"] if torch.cuda.is_available() else ["cpu"]
    elif DEVICE.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable to this Python runtime.")

    last_error: Exception | None = None
    for device in candidates:
        dtype = torch.float16 if device.startswith("cuda") else torch.float32
        try:
            return Qwen3TTSModel.from_pretrained(
                MODEL_ID,
                device_map=device,
                dtype=dtype,
            )
        except RuntimeError as exc:
            last_error = exc
            if not device.startswith("cuda") or "out of memory" not in str(exc).lower():
                raise
            logger.warning("Qwen-TTS CUDA memory was insufficient; retrying on CPU.")
            torch.cuda.empty_cache()
    raise RuntimeError("Qwen-TTS model could not be loaded.") from last_error


def _authorize(handler: BaseHTTPRequestHandler) -> bool:
    if not API_KEY:
        return True
    supplied = handler.headers.get("Authorization", "")
    expected = f"Bearer {API_KEY}"
    return hmac.compare_digest(supplied.encode(), expected.encode())


class Handler(BaseHTTPRequestHandler):
    server_version = "AuroraQwenTTS/1.0"

    def log_message(self, format: str, *args: object) -> None:
        logger.info("%s - %s", self.client_address[0], format % args)

    def _json(self, status: int, payload: dict[str, object]) -> None:
        content = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ready", "model_loaded": _model is not None})
            return
        if self.path == "/v1/models":
            if not _authorize(self):
                self._json(401, {"error": {"message": "Unauthorized"}})
                return
            self._json(
                200,
                {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]},
            )
            return
        self._json(404, {"error": {"message": "Not found"}})

    def do_POST(self) -> None:
        if self.path != "/v1/audio/speech":
            self._json(404, {"error": {"message": "Not found"}})
            return
        if not _authorize(self):
            self._json(401, {"error": {"message": "Unauthorized"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": {"message": "Invalid content length"}})
            return
        if not 1 <= length <= MAX_REQUEST_BYTES:
            self._json(413, {"error": {"message": "Request size is outside limits"}})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": {"message": "Request must contain JSON"}})
            return
        text = body.get("input") if isinstance(body, dict) else None
        voice = body.get("voice", "Ryan") if isinstance(body, dict) else "Ryan"
        model_id = body.get("model", MODEL_ID) if isinstance(body, dict) else MODEL_ID
        response_format = body.get("response_format", "wav") if isinstance(body, dict) else "wav"
        speed = body.get("speed", 1.0) if isinstance(body, dict) else 1.0
        if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
            self._json(422, {"error": {"message": "input must contain 1 to 10000 characters"}})
            return
        if not isinstance(voice, str) or not voice.strip() or len(voice) > 80:
            self._json(422, {"error": {"message": "voice is invalid"}})
            return
        if model_id != MODEL_ID or response_format != "wav" or speed != 1.0:
            self._json(
                422,
                {"error": {"message": "Only the configured model, WAV, and speed 1.0 are supported"}},
            )
            return

        try:
            wav_content = self._synthesize(text=text, voice=voice)
        except Exception:
            logger.exception("Qwen-TTS synthesis failed.")
            self._json(503, {"error": {"message": "Qwen-TTS synthesis failed"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav_content)))
        self.end_headers()
        self.wfile.write(wav_content)

    @staticmethod
    def _synthesize(*, text: str, voice: str) -> bytes:
        import soundfile as sf

        model = _model
        if model is None:
            raise RuntimeError("Qwen-TTS model has not loaded.")
        with _model_lock:
            max_new_tokens = min(
                MAX_AUDIO_TOKENS,
                max(64, math.ceil(len(text.strip()) * 2.6)),
            )
            waveforms, sample_rate = model.generate_custom_voice(
                text=text,
                language=LANGUAGE,
                speaker=voice,
                do_sample=True,
                top_k=50,
                top_p=0.9,
                temperature=0.7,
                max_new_tokens=max_new_tokens,
                subtalker_dosample=SUBTALKER_DOSAMPLE,
            )
        waveform = np.asarray(waveforms[0], dtype=np.float32).reshape(-1)
        if waveform.size == 0 or not np.isfinite(waveform).all():
            raise RuntimeError("Qwen-TTS returned an empty or non-finite waveform.")
        peak = float(np.max(np.abs(waveform)))
        if peak <= 1e-8:
            raise RuntimeError("Qwen-TTS returned a silent waveform.")
        # The decoder may return samples outside SoundFile's normalized float
        # range. SoundFile otherwise clips those samples to full scale, creating
        # harsh audio and causing the downstream loudness/true-peak gate to fail.
        # Preserve the waveform dynamics while setting a safe -1 dBFS peak.
        waveform = waveform * (0.89125094 / peak)
        logger.info(
            "Qwen waveform normalized: samples=%s source_peak=%.5f",
            waveform.size,
            peak,
        )
        output = io.BytesIO()
        sf.write(output, waveform, sample_rate, format="WAV", subtype="PCM_16")
        return output.getvalue()


def main() -> None:
    global _model
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    if not MODEL_ID or not LANGUAGE or not 1 <= PORT <= 65535:
        raise SystemExit("Qwen-TTS model, language, or port configuration is invalid.")
    if HOST not in {"127.0.0.1", "localhost", "::1"} and len(API_KEY) < 32:
        raise SystemExit("Set QWEN_TTS_API_KEY (at least 32 characters) before network binding.")
    if DEVICE not in {"auto", "cpu", "cuda:0"}:
        raise SystemExit("QWEN_TTS_DEVICE must be auto, cpu, or cuda:0.")

    _model = _load_model()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    logger.info("Qwen-TTS bridge listening on %s:%s", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
