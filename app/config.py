"""Configuration loader."""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("voiceguard.config")

CONFIG_PATH = Path("config.json")
EXAMPLE_PATH = Path("config.example.json")


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 28472
    server_key: str = "CHANGE-ME-AT-LEAST-16-CHARS-LONG"


@dataclass
class TranscriptionConfig:
    model: str = "Systran/faster-whisper-base"  # tiny/base/small/medium/large-v3
    language: str = "en"  # locked to English
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = 4
    num_workers: int = 1
    # VAD filter cuts out non-speech
    vad_filter: bool = True
    vad_min_silence_ms: int = 500
    # Anti-hallucination
    min_audio_seconds: float = 0.4
    min_confidence: float = -1.0  # avg_logprob >= this; -1.0 disables
    no_speech_threshold: float = 0.6
    # Hallucinated phrases to drop (case-insensitive substring match)
    hallucination_blacklist: list = field(default_factory=lambda: [
        "thanks for watching",
        "thank you for watching",
        "subscribe",
        "please subscribe",
        "thanks for listening",
        ".",
        "you",
        "bye",
        "[music]",
        "[silence]",
    ])


@dataclass
class ModerationConfig:
    case_sensitive: bool = False
    # Whole-word match (regex \b) instead of substring. Recommended.
    whole_word: bool = True


@dataclass
class Config:
    server: ServerConfig
    transcription: TranscriptionConfig
    moderation: ModerationConfig


def _from_dict(d: dict) -> Config:
    srv = ServerConfig(**(d.get("server") or {}))
    trn = TranscriptionConfig(**(d.get("transcription") or {}))
    mod = ModerationConfig(**(d.get("moderation") or {}))
    return Config(server=srv, transcription=trn, moderation=mod)


def load_config() -> Config:
    """Load config.json, creating it from defaults if missing."""
    if not CONFIG_PATH.exists():
        log.warning("config.json not found, writing defaults")
        default = Config(
            server=ServerConfig(),
            transcription=TranscriptionConfig(),
            moderation=ModerationConfig(),
        )
        save_default(default)
        return default

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return _from_dict(raw)


def save_default(cfg: Config) -> None:
    out = {
        "server": cfg.server.__dict__,
        "transcription": cfg.transcription.__dict__,
        "moderation": cfg.moderation.__dict__,
    }
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
