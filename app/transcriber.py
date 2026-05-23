"""faster-whisper transcription with VAD and anti-hallucination."""
import base64
import io
import logging
from typing import Optional

import numpy as np

from .config import Config

log = logging.getLogger("voiceguard.transcriber")


class Transcriber:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None
        self.stats_total = 0

    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self):
        """Load the Whisper model. Blocking - call from a thread."""
        # Import here so module import doesn't drag faster-whisper in until needed.
        from faster_whisper import WhisperModel

        t = self.cfg.transcription
        self._model = WhisperModel(
            t.model,
            device=t.device,
            compute_type=t.compute_type,
            cpu_threads=t.cpu_threads,
            num_workers=t.num_workers,
            download_root="models",
        )

    def transcribe_b64(self, audio_b64: str, sample_rate: int) -> tuple[str, list, str]:
        """
        Decode base64 PCM int16 audio and transcribe.
        Returns (text, segments, language).
        """
        raw = base64.b64decode(audio_b64)
        # PCM signed 16-bit little-endian -> float32 [-1, 1]
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

        duration = len(pcm) / sample_rate
        if duration < self.cfg.transcription.min_audio_seconds:
            return "", [], self.cfg.transcription.language

        # faster-whisper expects 16kHz mono float32; resample if needed.
        if sample_rate != 16000:
            pcm = self._resample(pcm, sample_rate, 16000)

        return self._transcribe_pcm(pcm)

    def _transcribe_pcm(self, pcm_16k_mono: np.ndarray) -> tuple[str, list, str]:
        t = self.cfg.transcription

        segments, info = self._model.transcribe(
            pcm_16k_mono,
            language=t.language if t.language != "auto" else None,
            vad_filter=t.vad_filter,
            vad_parameters={"min_silence_duration_ms": t.vad_min_silence_ms},
            no_speech_threshold=t.no_speech_threshold,
            beam_size=1,  # fast; bump to 5 for accuracy at cost of speed
            condition_on_previous_text=False,  # avoid cascading hallucinations
        )

        kept_segments = []
        text_parts = []
        for seg in segments:
            # Confidence filter
            if t.min_confidence > -1.0 and seg.avg_logprob < t.min_confidence:
                log.debug("Dropping low-confidence segment: '%s' (logprob=%.2f)",
                          seg.text, seg.avg_logprob)
                continue

            text = seg.text.strip()
            if self._is_hallucination(text):
                log.debug("Dropping hallucinated segment: '%s'", text)
                continue

            kept_segments.append({
                "start": seg.start,
                "end": seg.end,
                "text": text,
                "avg_logprob": seg.avg_logprob,
            })
            text_parts.append(text)

        self.stats_total += 1
        return " ".join(text_parts).strip(), kept_segments, info.language

    def _is_hallucination(self, text: str) -> bool:
        if not text:
            return True
        lower = text.lower().strip()
        for needle in self.cfg.transcription.hallucination_blacklist:
            if needle.lower() == lower or needle.lower() == lower.rstrip(".!?,"):
                return True
        return False

    @staticmethod
    def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        """Simple linear resample. For high quality use scipy or librosa."""
        if src_rate == dst_rate:
            return audio
        duration = len(audio) / src_rate
        new_len = int(duration * dst_rate)
        # numpy interp - good enough for speech
        old_indices = np.linspace(0, len(audio) - 1, new_len)
        return np.interp(old_indices, np.arange(len(audio)), audio).astype(np.float32)
