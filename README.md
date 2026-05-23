# VoiceGuard Processor

Python WebSocket processor for the VoiceGuard Minecraft voice moderation plugin.
Uses `faster-whisper` for speech-to-text, plus wordlist matching with severity tiers.

## Architecture

The plugin sends audio chunks (PCM 16-bit base64) over WebSocket. The processor:

1. Decodes and resamples to 16kHz mono float32.
2. Runs faster-whisper STT with Silero VAD to drop non-speech.
3. Filters out common hallucinations and low-confidence segments.
4. Matches against wordlists at 4 severity tiers: `low`, `medium`, `high`, `critical`.
5. Sends back JSON `{transcript, flagged, severity, matched_words}`.

The plugin holds the 15-second audio buffer and posts to Discord itself when flagged.
The processor never sees more audio than the chunk you send it - small and fast.

## Pterodactyl install (recommended)

1. Push this folder to a private git repo (or use the public one).
2. In Pterodactyl panel: **Nests > Eggs > Import**, upload `egg-voiceguard-processor.json`.
3. Create a server using that egg with the **Python 3.11 yolks** image.
4. Set the `REPO_URL` variable to your git URL.
5. Run **Install** once.
6. Edit `config.json` in the server root - **set `server_key`** to a long random string.
7. Start the server. First start downloads the Whisper model (~150MB for base.en).

The egg installs ffmpeg + clones the repo + sets up a venv + installs CPU PyTorch and `faster-whisper`. Restart re-pulls the latest `app/` so you get updates without reinstalling.

## Manual install (without Pterodactyl)

```bash
git clone <repo> voiceguard-processor
cd voiceguard-processor
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -r requirements.txt
cp config.example.json config.json
# edit config.json, set server_key
python -m app.main
```

## Configuration (`config.json`)

| Section | Key | Notes |
|---|---|---|
| `server.host` | listen IP | `0.0.0.0` to accept from outside |
| `server.port` | port | Pterodactyl overrides with `SERVER_PORT` env var |
| `server.server_key` | shared secret | 16+ chars, MUST match plugin `config.yml` |
| `transcription.model` | Whisper model | `Systran/faster-whisper-base.en` recommended for English-only, ~1GB RAM |
| `transcription.compute_type` | quantization | `int8` for CPU = fastest |
| `transcription.cpu_threads` | threads | match your VPS cores |
| `transcription.vad_filter` | true | drops non-speech audio |
| `transcription.min_audio_seconds` | 0.4 | discard sub-400ms chunks |

### Whisper model choices (English-only)

| Model | RAM | Speed (CPU int8) | Accuracy |
|---|---|---|---|
| `Systran/faster-whisper-tiny.en` | ~400MB | very fast | OK |
| `Systran/faster-whisper-base.en` | ~600MB | fast | **good (recommended)** |
| `Systran/faster-whisper-small.en` | ~1.5GB | medium | very good |
| `Systran/faster-whisper-medium.en` | ~3.5GB | slow | excellent |

Start with `base.en`. Bump to `small.en` if you see too many false transcriptions.

## Wordlist (`wordlist.txt`)

Auto-generated on first start. Sections:

```
[EN-PROFANITY]    # low - log only
damn
hell

[EN-MEDIUM]       # medium - in-game warn
shit
asshole

[EN-MUTE]         # high - auto-mute
fuck
cunt

[EN-CRITICAL]     # critical - urgent staff ping, ban-worthy
# add slurs and threats here per your policy
```

The plugin can also push wordlist updates over WebSocket via `wordlist_update` message,
so you can edit lists from the plugin side without restarting the processor.

## Endpoints

- `GET /health` - public, returns `{status, model_loaded, active_sessions}`
- `GET /stats` - requires `X-Server-Key` header, returns counters
- `WS /ws` - the main WebSocket the plugin connects to

## Wire protocol

### Plugin -> Processor

```json
// First message after connect
{"type": "auth", "server_key": "..."}

// Audio chunk (PCM s16le mono base64)
{
  "type": "audio",
  "request_id": "uuid",
  "player_uuid": "uuid",
  "player_name": "Steve",
  "sample_rate": 48000,
  "audio": "base64..."
}

// Hot-reload wordlists
{
  "type": "wordlist_update",
  "profanity": ["..."],
  "medium": ["..."],
  "mute": ["..."],
  "critical": ["..."]
}

// Keepalive
{"type": "ping"}
```

### Processor -> Plugin

```json
{"type": "auth_ok", "session_id": "...", "config": {...}}

{
  "type": "transcription",
  "request_id": "...",
  "player_uuid": "...",
  "player_name": "Steve",
  "transcript": "what was said",
  "language": "en",
  "flagged": true,
  "severity": "high",
  "matched_words": ["..."]
}

{"type": "error", "error": "...", "request_id": "..."}
{"type": "wordlist_updated"}
{"type": "pong"}
```

## Securing

- **Always use a strong `server_key`.** Treat it like a password.
- Run behind nginx with HTTPS (`wss://`) if the processor is on a public IP.
- Don't expose `/stats` without auth (it already requires the key).
- Firewall the port to your Folia node's IP if possible.
