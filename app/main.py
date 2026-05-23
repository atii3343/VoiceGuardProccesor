"""
VoiceGuard Processor - Main entry point.
FastAPI + WebSocket server for real-time voice chat moderation.
"""
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException

from .config import load_config, Config
from .transcriber import Transcriber
from .moderator import Moderator
from .session import SessionManager

# ---------- logging ----------
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "processor.log"),
    ],
)
log = logging.getLogger("voiceguard")

# ---------- globals (set in lifespan) ----------
CONFIG: Config | None = None
TRANSCRIBER: Transcriber | None = None
MODERATOR: Moderator | None = None
SESSIONS: SessionManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CONFIG, TRANSCRIBER, MODERATOR, SESSIONS

    CONFIG = load_config()
    log.info("Loaded config (port=%d, model=%s, device=%s)",
             CONFIG.server.port, CONFIG.transcription.model,
             CONFIG.transcription.device)

    MODERATOR = Moderator(CONFIG)
    log.info("Moderator ready (wordlists: %d profanity / %d mute / %d critical)",
             len(MODERATOR.profanity), len(MODERATOR.mute), len(MODERATOR.critical))

    TRANSCRIBER = Transcriber(CONFIG)
    log.info("Loading Whisper model '%s'... (this can take a while on first run)",
             CONFIG.transcription.model)
    await asyncio.to_thread(TRANSCRIBER.load)
    log.info("Whisper model loaded.")

    SESSIONS = SessionManager()

    yield

    log.info("Shutting down...")


app = FastAPI(title="VoiceGuard Processor", lifespan=lifespan)


# ---------- HTTP endpoints ----------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model_loaded": TRANSCRIBER is not None and TRANSCRIBER.is_loaded(),
        "active_sessions": SESSIONS.count() if SESSIONS else 0,
    }


@app.get("/stats")
async def stats(x_server_key: str | None = Header(None)):
    if not CONFIG or x_server_key != CONFIG.server.server_key:
        raise HTTPException(status_code=401, detail="Invalid server key")
    return {
        "active_sessions": SESSIONS.count(),
        "total_transcribed": TRANSCRIBER.stats_total if TRANSCRIBER else 0,
        "total_flagged": MODERATOR.stats_flagged if MODERATOR else 0,
        "model": CONFIG.transcription.model,
        "device": CONFIG.transcription.device,
    }


# ---------- WebSocket endpoint ----------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = None

    try:
        # ----- handshake / auth -----
        auth_raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        auth_msg = json.loads(auth_raw)

        if auth_msg.get("type") != "auth":
            await websocket.send_json({"type": "error", "error": "expected_auth"})
            await websocket.close(code=4001)
            return

        if auth_msg.get("server_key") != CONFIG.server.server_key:
            log.warning("Auth failed from %s", websocket.client)
            await websocket.send_json({"type": "error", "error": "invalid_key"})
            await websocket.close(code=4003)
            return

        session_id = SESSIONS.add(websocket)
        await websocket.send_json({
            "type": "auth_ok",
            "session_id": session_id,
            "config": {
                "min_audio_seconds": CONFIG.transcription.min_audio_seconds,
                "language": CONFIG.transcription.language,
            },
        })
        log.info("Session %s authenticated (%s)", session_id, websocket.client)

        # ----- message loop -----
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "audio":
                await handle_audio(websocket, msg)
            elif msg_type == "wordlist_update":
                MODERATOR.update_wordlists(
                    profanity=msg.get("profanity", []),
                    mute=msg.get("mute", []),
                    critical=msg.get("critical", []),
                )
                await websocket.send_json({"type": "wordlist_updated"})
                log.info("Wordlists updated from session %s", session_id)
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                log.warning("Unknown message type: %s", msg_type)

    except WebSocketDisconnect:
        log.info("Session %s disconnected", session_id)
    except asyncio.TimeoutError:
        log.warning("Auth timeout from %s", websocket.client)
        await websocket.close(code=4002)
    except Exception as e:
        log.exception("WebSocket error in session %s: %s", session_id, e)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        if session_id is not None:
            SESSIONS.remove(session_id)


async def handle_audio(websocket: WebSocket, msg: dict):
    """Process an audio chunk from the plugin."""
    player_uuid = msg.get("player_uuid")
    player_name = msg.get("player_name", "?")
    audio_b64 = msg.get("audio")
    sample_rate = msg.get("sample_rate", 48000)
    request_id = msg.get("request_id")

    if not player_uuid or not audio_b64:
        await websocket.send_json({
            "type": "error",
            "error": "missing_fields",
            "request_id": request_id,
        })
        return

    # Run heavy work in thread pool
    try:
        transcript, segments, language = await asyncio.to_thread(
            TRANSCRIBER.transcribe_b64, audio_b64, sample_rate,
        )
    except Exception as e:
        log.exception("Transcription failed for %s: %s", player_name, e)
        await websocket.send_json({
            "type": "error",
            "error": "transcription_failed",
            "request_id": request_id,
        })
        return

    if not transcript.strip():
        # silence or hallucination filtered out
        await websocket.send_json({
            "type": "transcription",
            "request_id": request_id,
            "player_uuid": player_uuid,
            "transcript": "",
            "language": language,
            "flagged": False,
            "severity": "none",
            "matched_words": [],
        })
        return

    verdict = MODERATOR.evaluate(transcript)

    response = {
        "type": "transcription",
        "request_id": request_id,
        "player_uuid": player_uuid,
        "player_name": player_name,
        "transcript": transcript,
        "language": language,
        "flagged": verdict.flagged,
        "severity": verdict.severity,
        "matched_words": verdict.matched,
    }

    await websocket.send_json(response)

    if verdict.flagged:
        log.info("FLAG [%s] %s: '%s' -> %s",
                 verdict.severity, player_name, transcript, verdict.matched)


def main():
    cfg = load_config()
    # Pterodactyl egg passes SERVER_PORT env var; respect it if present.
    port = int(os.environ.get("SERVER_PORT", cfg.server.port))
    host = os.environ.get("SERVER_IP", cfg.server.host)
    log.info("Starting VoiceGuard processor on %s:%d", host, port)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level="info",
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )


if __name__ == "__main__":
    main()
