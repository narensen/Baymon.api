# Baymon

A personal AI companion you can talk to. Baymon listens, replies by voice or text, works on code in your projects, and remembers what matters about you across conversations.

- **Text & coding agent** — `openai/gpt-oss-120b` or `minimaxai/minimax-m2.7` (preview), both on Groq
- **Speech-to-text** — Whisper (`whisper-large-v3-turbo`) on Groq
- **Text-to-speech** — Canopy Labs Orpheus (`canopylabs/orpheus-v1-english`) on Groq
- **Memory & auth** — Supabase (JSONB memory per user)

## Quick start

Baymon has two parts: the **API server** (holds the Groq and Supabase keys) and the **`baymon` CLI** (runs on your machine and signs in to the server). Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env     # fill in your keys
uv run baymon-api        # terminal 1: the server, http://localhost:8000
uv run baymon login      # terminal 2: sign in
uv run baymon            # start chatting
```

### `.env`

| Variable | Required | Description |
|---|---|---|
| `SUPABASE_URL` | ✅ | `https://<project-ref>.supabase.co` |
| `SUPABASE_SECRET_KEY` | ✅ | Secret key (`sb_secret_...`). Server-side only — never ship to a client. |
| `GROQ_API_KEY` | ✅ | Groq API key |
| `BAYMON_TEST_EMAIL` / `BAYMON_TEST_PASSWORD` | | Test user for the API tests and frontend dev |
| `BAYMON_CORS_ORIGINS` | | Comma-separated origins (default `localhost:3000`, `localhost:5173`) |
| `BAYMON_HOST` / `BAYMON_PORT` / `BAYMON_RELOAD` | | API server settings (default `127.0.0.1:8000`) |
| `BAYMON_TTS_VOICE` | | `troy` (default), `austin`, `daniel`, `autumn`, `diana`, `hannah` |
| `BAYMON_MIC_RATIO` | | Speech sensitivity vs. room noise (default `1.5`; lower = more sensitive) |
| `BAYMON_MIC_THRESHOLD` | | Fixed loudness threshold instead of auto |
| `BAYMON_END_SILENCE_MS` | | Silence that ends your turn (default `1500`) |

### Database

Memory is stored in the `public.memory` table: one row per user (`user_id` → `users.user_id`), with memories under `memory -> 'items'`. Concurrent writes are safe via optimistic locking on the `version` column, so no database functions are needed. The row is created automatically on first write; the user must already exist in `users`. See [`supabase/schema.sql`](supabase/schema.sql).

## CLI

### Install

| Platform | |
|---|---|
| Debian / Ubuntu | `sudo apt install ./baymon_0.2.0_amd64.deb` (installs `/usr/bin/baymon`) |
| Windows | Put `baymon.exe` somewhere on your `PATH` |
| From source | `uv run baymon` |

Then point it at your server (once) and sign in:

```bash
baymon config api_url https://your-baymon-api.onrender.com
baymon login
```

The CLI never holds the Groq or Supabase keys; it signs in with your Baymon account and the server makes the model calls. Settings and the saved sign-in live in `~/.config/baymon/` (Windows: `%APPDATA%\Baymon`).

### Use

```bash
baymon                          # interactive session in the current folder
baymon "why are the tests failing?"   # start with a message
baymon --talk                   # hands-free voice
baymon -p "summarize this repo" # answer once and exit; piped stdin is included
git diff | baymon -p "review this"
baymon -m minimax-m2.7          # pick a model for this session
baymon -y                       # auto-approve edits and commands (careful)
baymon -C ~/code/app            # work in another folder
```

Other commands: `baymon login | logout | whoami`, `baymon config [key [value]]` (`api_url`, `model`, `mode`, `voice`, `auto_approve`), `baymon memory [list | add <text> | rm <id> | import <file>]`, `baymon mic-check`.

### What Baymon can do

| Tool | Approval |
|---|---|
| `read_file`, `list_dir`, `glob`, `grep` | Automatic inside the working folder; asks for anything outside it |
| `edit_file`, `write_file` | Asks, showing a diff |
| `run_command` (bash / PowerShell) | Asks, showing the command; output streams live |
| `fetch_url` | Automatic |
| `remember`, `recall` | Automatic |
| `forget` | Asks |
| `update_plan` | Shows a checklist for multi-step work |
| `switch_mode` | When you ask to switch between talking and typing |

At an approval prompt: **y**es, **n**o (then optionally tell Baymon what to do instead), or **a**lways for the rest of the session.

Baymon also learns as you go: after every turn the server decides in the background whether anything is worth remembering, and the agent can call `remember` itself.

### Commands inside a session

| | |
|---|---|
| `/model [name]` | Show or switch the model |
| `/talk`, `/type` | Hands-free voice or typing (or just ask: *"switch to typing"*) |
| `/voice [name]` | Speaking voice: troy, austin, daniel, autumn, diana, hannah |
| `/memory`, `/remember <text>`, `/forget <id>`, `/import <file>` | Manage memory |
| `/clear`, `/compact` | New conversation, or summarize to free up context |
| `/auto` | Toggle auto-approve |
| `/cd <path>` | Change the working folder |
| `/reasoning` | Show or hide the model's reasoning |
| `/whoami`, `/logout`, `/quit` | |

Esc+Enter inserts a new line. Ctrl+C interrupts a reply (or a running command); Ctrl+D exits.

**Talk mode** is hands-free: your words appear as you pause, your turn ends after ~1.5 s of silence, Baymon speaks a short version of the reply and listens again. Steady background noise (fans, hum) is ignored; tune with `baymon mic-check`. Type a line at any time to send it or run a command.

### Models

| Key | Model | Notes |
|---|---|---|
| `gpt-oss-120b` | GPT-OSS 120B on Groq | Default |
| `minimax-m2.7` | MiniMax M2.7 on Groq (`minimaxai/minimax-m2.7`) | Preview; works once Groq enables it for your account |

### Build the installers

```bash
scripts/build_deb.sh                                   # → dist/baymon_<version>_amd64.deb
BAYMON_DEFAULT_API_URL=https://… scripts/build_deb.sh  # bake in your server URL
```

On Windows: `powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1` → `dist\baymon.exe`.

Or push a `v*` tag to GitHub: [`.github/workflows/build-cli.yml`](.github/workflows/build-cli.yml) builds both and attaches them to a release. Set the repository variable `BAYMON_DEFAULT_API_URL` to bake in your server.

## API

```bash
uv run baymon-api   # http://localhost:8000 — interactive docs at /docs
```

Every `/v1` route needs a Supabase access token. From the frontend:

```js
const { data } = await supabase.auth.signInWithPassword({ email, password })
fetch("http://localhost:8000/v1/chat", {
  method: "POST",
  headers: {
    Authorization: `Bearer ${data.session.access_token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ message: "Hey Baymon!", voice: true }),
})
```

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness check (no auth) |
| `GET /v1/me` | Signed-in user |
| `POST /v1/chat` | `{message, history?, voice?, mode?, tts_voice?}` → `{text, speech_text, audio_base64, audio_mime, mode, warnings}` |
| `POST /v1/talk` | One hands-free voice turn. Multipart `file` + optional `history` (JSON string) and `tts_voice` → `{transcript, text, caption, audio_base64, audio_mime, mode, warnings}`. `caption` is exactly what's spoken: plain text, ≤ 2 sentences / 40 words |
| `POST /v1/transcribe` | Multipart `file` (webm, wav, mp3, m4a, ogg, flac…; ≤ 25 MB), optional `prompt` → `{text}` |
| `POST /v1/speak` | `{text, voice?}` → `audio/wav` |
| `GET /v1/memory` | `{items: [...]}` |
| `POST /v1/memory` | `{content, category?}` — store a memory verbatim |
| `POST /v1/memory/import` | `{text}` — AI extracts memories from free-form notes |
| `PATCH /v1/memory/{id}` | `{content?, category?}` |
| `DELETE /v1/memory/{id}` | 204 |
| `POST /v1/memory/observe` | `{user_message, reply}` — learn from a finished turn in the background (202) |
| `POST /v1/auth/login` | `{email, password}` → `{access_token, refresh_token, expires_at, user}` (no auth; for the CLI) |
| `POST /v1/auth/refresh` | `{refresh_token}` → same as login |
| `GET /v1/models` | Available agent models |
| `POST /v1/agent/complete` | One model step for a client-side agent loop. `{messages, tools, model, mode, environment}` → NDJSON stream of `text`, `reasoning`, `tool_calls`, `status`, `done`, `error` events |
| `POST /v1/speak/reply` | `{text, voice?}` → `{caption, audio_base64, audio_mime, warnings}` |

Memory categories: `preference`, `fact`, `task`, `decision`, `other`.

**Notes**

- The user always comes from the token (verified locally against Supabase's JWKS), never from the request body.
- Conversation history is kept by the client and sent with each `/v1/chat` call; the server is stateless.
- After every chat turn, the AI decides in the background whether anything is worth remembering.
- If `mode` is sent, the AI can switch modes when the user asks; the requested mode comes back in `mode`.
- Errors are always `{"error": {"code", "message"}}` with a matching HTTP status (e.g. `429 groq_rate_limit`, `404 memory_not_found`). Non-fatal problems in `/v1/chat` (e.g. audio failed) come back in `warnings` alongside the text.

## Project layout

```
src/baymon/
  services.py      core runtime: listen, generate_text, generate_audio, memory, run_turn
  completions.py   agent models, streaming, system prompt
  api.py           FastAPI app
  formatting.py    rich-UI reply format for the app
  cli/
    __init__.py    `baymon` entry point and subcommands
    repl.py        interactive session, slash commands, talk mode
    agent.py       agent loop: stream, run tools, repeat
    tools.py       local tools and approvals
    client.py      API client (sign-in, refresh, streaming)
    ui.py          terminal rendering
    audio.py       mic, voice detection, playback
    config.py      settings and saved sign-in
packaging/         PyInstaller spec
scripts/           build_deb.sh, build_windows.ps1
supabase/schema.sql
tests/             test_api.py (integration), test_cli.py (unit)
```

## Tests

```bash
uv run pytest
```

`tests/test_cli.py` runs offline. `tests/test_api.py` is integration: it hits the real Supabase project and Groq, signing in as the test user from `.env`. Audio tests skip if Groq's TTS quota is exhausted.

## Known limits

- **TTS quota:** Groq's free tier for Orpheus allows ~3,600 tokens/day, only a few minutes of speech. Use the Dev tier for real use.
- **Orpheus input:** ~200 characters per request, so long replies are split up and the audio joined back together.
- **Agent speed on the free tier:** Groq allows GPT-OSS 120B 8,000 tokens/minute, and each agent step resends the instructions and conversation, so multi-step coding tasks pause to wait out the limit (the CLI shows the wait). The Dev tier removes this.
- **CLI audio:** Linux uses `arecord`/`aplay`/`pw-play` (alsa-utils); Windows uses `sounddevice`. You can't interrupt Baymon by voice while it's speaking.
- **`.deb` portability:** binaries built on a newer Ubuntu need an equally new glibc. The GitHub workflow builds on Ubuntu 22.04 for wider compatibility.
- **Rate limiting:** there's no per-user rate limiting on the API yet.
