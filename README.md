# JARVIS Backend

Local-only FastAPI + PostgreSQL service that powers the JARVIS desktop client.
It owns all persistent state, scheduling, the assistant runtime, automation
execution and the authenticated desktop-agent bridge.

## Requirements

- Python 3.12+
- PostgreSQL 15+ running locally
- A database named `Jarvis` (or your own `DATABASE_URL`)

## Setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env   # then edit DATABASE_URL and the API token
.\.venv\Scripts\python.exe -m alembic upgrade head
```

## Run

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8756
```

The Electron client normally starts and stops this process itself. Set
`JARVIS_MANAGE_BACKEND=0` in the client environment when running uvicorn
manually.

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8756/health
```

## Authentication

Every route except `/health` requires `X-Jarvis-Token`. The voice WebSocket
accepts the same token as a `token` query parameter because browser WebSocket
handshakes cannot set custom headers. The Electron main process generates a
64-character token in its `userData` directory and passes it to the backend as
`JARVIS_API_TOKEN`. For manual development the fallback token is
`jarvis-local-dev-token`.

## Layout

```text
backend/
├── app/
│   ├── ai/              # provider abstraction, offline assistant, tool registry
│   ├── api/v1/          # routers and endpoint modules
│   ├── automation/      # action validation, workflow engine, scheduler jobs
│   ├── core/            # config, logging, security, datetime/timezone helpers
│   ├── db/              # SQLAlchemy session and engine
│   ├── models/          # ORM entities
│   ├── repositories/    # query composition
│   ├── scheduler/       # APScheduler ticks (reminders, workflows, briefings)
│   ├── schemas/         # Pydantic request/response contracts
│   ├── services/        # domain logic, voice, Spotify, system telemetry
│   └── main.py          # application factory, lifespan, middleware
├── alembic/             # schema migrations
├── tests/               # pytest suite
├── alembic.ini
├── requirements.txt
└── requirements-dev.txt
```

## Environment

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL connection string | local `Jarvis` database |
| `JARVIS_API_HOST` / `JARVIS_API_PORT` | Bind address | `127.0.0.1:8756` |
| `JARVIS_API_TOKEN` | Required client token | dev fallback |
| `JARVIS_DEFAULT_TIMEZONE` | Display/scheduling zone | `UTC` |
| `JARVIS_SCHEDULER_ENABLED` | Enable APScheduler ticks | `true` |
| `AI_PROVIDER` | `mock`, `openai`, `anthropic`, `ollama` | `mock` |
| `AI_MODEL` | Provider model name | `gpt-4o-mini` |
| `AI_BASE_URL` | OpenAI-compatible endpoint | OpenAI |
| `AI_API_KEY` | Provider credential | empty (offline mode) |
| `JARVIS_OLLAMA_BASE_URL` | Local Ollama endpoint | `http://127.0.0.1:11434` |
| `JARVIS_WHISPER_MODEL` | faster-whisper model | `base.en` |
| `JARVIS_WHISPER_DEVICE` / `_COMPUTE_TYPE` | Local STT execution | `cpu` / `int8` |
| `JARVIS_TTS_VOICE` / `_LANGUAGE` | Default Edge TTS voice | `en-GB-RyanNeural` / `en-GB` |
| `JARVIS_WAKE_MODEL` | openWakeWord model | `hey_jarvis` |
| `JARVIS_WAKE_THRESHOLD` / `_COOLDOWN_SECONDS` | Wake detection tuning | `0.6` / `2.0` |
| `JARVIS_MAX_AUDIO_BYTES` | Upload cap for transcription | 25 MiB |
| `JARVIS_SPOTIFY_CLIENT_ID` | Spotify app client ID | empty (disabled) |
| `JARVIS_SPOTIFY_CLIENT_SECRET` | Optional PKCE secret | empty |
| `JARVIS_SPOTIFY_REDIRECT_URI` | Exact registered callback | `http://127.0.0.1:8756/api/v1/spotify/callback` |
| `JARVIS_SECRETS_KEY` | Fernet source key | generated in `.data/secrets.key` |

Secrets belong in `.env` (ignored by git) or in the Windows credential vault
managed by the desktop client. They are never returned by the settings API.

## Voice and Spotify

- `POST /api/v1/voice/transcribe` accepts multipart audio and returns a
  faster-whisper transcript. `POST /api/v1/voice/speak` returns Edge TTS audio.
- `WS /api/v1/voice/wake` accepts 16 kHz mono PCM frames of 1280 samples
  (80 ms) and replies with detection events. Audio is processed locally.
- `GET /api/v1/system/status` and the volume endpoints use psutil and pycaw.
- Spotify uses the authorization-code PKCE flow. Tokens are encrypted with
  Fernet before storage. Playback commands are verified by reading the player
  state back; fallback paths are explicit and reported as unverified.
- The full setup and manual verification checklist is in
  [`../docs/HUD.md`](../docs/HUD.md).

## Migrations

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe change"
```

## Checks

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest
```

Tests use an isolated temporary SQLite database created by the test fixtures, so
they never touch the development PostgreSQL database. External AI, Spotify and
desktop services are mocked. Platform-dependent voice, volume and wake tests
skip or assert the documented degraded state when their optional dependencies
are unavailable.
