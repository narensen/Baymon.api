# Baymon

A personal AI companion you can talk to. Baymon listens, replies by voice or text, and remembers what matters about you across conversations.

- **Text** — `openai/gpt-oss-120b` on Groq
- **Speech-to-text** — Whisper (`whisper-large-v3-turbo`) on Groq
- **Text-to-speech** — Canopy Labs Orpheus (`canopylabs/orpheus-v1-english`) on Groq
- **Memory & auth** — Supabase (JSONB memory per user)

## Quick start

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), and on Linux `alsa-utils` (`arecord`/`aplay`) for the mic.

```bash
uv sync
cp .env.example .env   # fill in your keys
uv run baymon          # hands-free voice chat in the terminal
```

### `.env`

| Variable | Required | Description |
|---|---|---|
| `SUPABASE_URL` | ✅ | `https://<project-ref>.supabase.co` |
| `SUPABASE_SECRET_KEY` | ✅ | Secret key (`sb_secret_...`). Server-side only — never ship to a client. |
| `GROQ_API_KEY` | ✅ | Groq API key |
| `BAYMON_USER_ID` | | Supabase user id the CLI uses for memory (memory is off without it) |
| `BAYMON_TEST_EMAIL` / `BAYMON_TEST_PASSWORD` | | Test user for the API tests and frontend dev |
| `BAYMON_CORS_ORIGINS` | | Comma-separated origins (default `localhost:3000`, `localhost:5173`) |
| `BAYMON_HOST` / `BAYMON_PORT` / `BAYMON_RELOAD` | | API server settings (default `127.0.0.1:8000`) |
| `BAYMON_TTS_VOICE` | | `troy` (default), `austin`, `daniel`, `autumn`, `diana`, `hannah` |
| `BAYMON_MIC_RATIO` | | Speech sensitivity vs. room noise (default `1.5`; lower = more sensitive) |
| `BAYMON_MIC_THRESHOLD` | | Fixed loudness threshold instead of auto |
| `BAYMON_END_SILENCE_MS` | | Silence that ends your turn (default `1500`) |

### Database

Memory is stored in the `public.memory` table: one row per user (`user_id` → `users.user_id`), with memories under `memory -> 'items'`. Concurrent writes are safe via optimistic locking on the `version` column, so no database functions are needed. The row is created automatically on first write; the user must already exist in `users`. See [`supabase/schema.sql`](supabase/schema.sql).

## Terminal chat

```bash
uv run baymon              # talk mode (default)
uv run baymon --type       # start in type mode
uv run baymon --mic-check  # live mic levels, for tuning in a noisy room
```

**Talk mode** is hands-free: just speak. Your words appear as you pause, your turn ends after ~1.5 s of silence, Baymon speaks the reply and listens again. Steady background noise (fans, hum) is ignored.

**Type mode** is keyboard in, text out.

| Command | |
|---|---|
| `/type`, `/talk` | Switch modes (typing works even while Baymon is listening) |
| *"switch to typing"* | Or just ask — Baymon switches for you |
| `/memory` | Show what Baymon remembers |
| `/quit` or Ctrl+C | Exit |

For code or long answers, Baymon shows the full text but speaks a short summary.

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
| `POST /v1/transcribe` | Multipart `file` (webm, wav, mp3, m4a, ogg, flac…; ≤ 25 MB), optional `prompt` → `{text}` |
| `POST /v1/speak` | `{text, voice?}` → `audio/wav` |
| `GET /v1/memory` | `{items: [...]}` |
| `POST /v1/memory` | `{content, category?}` — store a memory verbatim |
| `POST /v1/memory/import` | `{text}` — AI extracts memories from free-form notes |
| `PATCH /v1/memory/{id}` | `{content?, category?}` |
| `DELETE /v1/memory/{id}` | 204 |

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
  services.py   core runtime: listen, generate_text, generate_audio, memory, run_turn
  api.py        FastAPI app
  cli.py        terminal chat
  mic.py        mic capture, voice detection, live transcription
supabase/schema.sql
tests/test_api.py
```

## Tests

```bash
uv run pytest
```

These are integration tests: they hit the real Supabase project and Groq, signing in as the test user from `.env`. Memory tests skip if the schema hasn't been applied; audio tests skip if Groq's TTS quota is exhausted.

## Known limits

- **TTS quota:** Groq's free tier for Orpheus allows ~3,600 tokens/day, only a few minutes of speech. Use the Dev tier for real use.
- **Orpheus input:** ~200 characters per request, so long replies are split up and the audio joined back together.
- **CLI audio:** Linux only (`arecord`/`aplay`/`pw-play`). You can't interrupt Baymon by voice while it's speaking.
- **Rate limiting:** there's no per-user rate limiting on the API yet.
