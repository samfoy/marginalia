# marginalia — Handover Document

*Last updated: 2026-06-16 | Version: 0.8.2*

---

## What this is

marginalia is a Mac bridge + KOReader plugin that brings AI reading intelligence to e-readers. It generates a "Book Index" (like Kindle X-Ray) from EPUB text via any configured LLM, answers reading questions using position-bounded RAG (no spoilers by construction), captures highlights and notes to both KOReader and Obsidian, and syncs everything over a local HTTP bridge.

**GitHub:** https://github.com/samfoy/marginalia  
**Repo dir:** `~/Projects/piread/` (git remote updated; local dir name kept)

---

## Architecture

```
KOReader (Palma)                    Mac (bridge, port 7731)
────────────────────                ────────────────────────────────────
marginalia.koplugin  ←── Tailscale/LAN ──→  bridge/server.py
  - Book Index browser                          ├── xray_generator.py  (LLM calls, fallback chain)
  - Ask AI (highlight+vault)                    ├── rag.py             (embeddings, retrieval)
  - AI: Save Note                               ├── xray_cache.py      (~/.marginalia/cache/)
  - Recap / Wiki / Chat / Section               ├── series.py          (cross-book scope)
  - Offline note queue                          ├── mentions.py        (jump-to-chapter index)
                                                ├── book_finder.py     (Calibre lookup)
                                                ├── epub_extract.py    (EPUB → text)
                                                ├── monitor.py         (request dashboard)
                                                └── server.py          (HTTP dispatcher)
```

The bridge runs as a macOS LaunchAgent (`com.sam.marginalia`), always-on, restarts on crash.

---

## Current state (2026-06-16)

- **Bridge:** running on port 7731, model `openai.gpt-5.5` via bedrock-mantle (fallback: `openai.gpt-5.4` → `us.anthropic.claude-sonnet-4-6`)
- **Plugin:** `marginalia.koplugin` deployed to Palma at `100.123.174.80`
- **Cache:** `~/.marginalia/cache/` — 38 Book Index files across multiple books/series
- **All endpoints tested:** 12/12 pass against The Expectant Father

### What works
- Book Index generation from EPUB text (Calibre) or model knowledge (fallback)
- Position-bounded RAG — /recap, /wiki, /chat, /section all spoiler-safe
- Series-aware cross-book context (prior books included, future books excluded)
- Ask AI → auto-highlights passage in KOReader with AI answer as note, syncs to Obsidian
- AI: Save Note → saves highlight in book, appends to Obsidian vault note
- Offline note queue (durable across connection loss)
- Model fallback chain with per-model circuit breaker (120s cooldown, auto-recovery)
- `marginalia setup` wizard — interactive first-run setup
- Docker (`docker compose up -d`)

### Known issues / active monitoring
- **gpt-5.5 outage (ongoing):** `openai.gpt-5.5` via bedrock-mantle returns 500s. Circuit breaker handles it transparently — falls to gpt-5.4. Will self-heal when AWS recovers. Test: `curl -X POST https://bedrock-mantle.us-east-2.api.aws/openai/v1/responses` with SigV4.
- **Streaming:** companion endpoints block until complete. Would require protocol change in the KOReader plugin (add chunked response handling).
- **`/v1/chat/completions`:** Bedrock-only OpenAI-compat proxy for KO Assistant. Intentionally bypasses the provider fallback chain — Bedrock clients only.

---

## Deployment

### Bridge (Mac)

The bridge is installed as a LaunchAgent and auto-starts at login. To pick up code changes (no plist edit):
```bash
launchctl kickstart -k gui/$(id -u)/com.sam.marginalia
```

To pick up **plist changes** (env vars, Python path):
```bash
launchctl bootout gui/$(id -u)/com.sam.marginalia
sleep 6  # wait for teardown race
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sam.marginalia.plist
# retry if it fails — race condition is normal
```

Live plist: `~/Library/LaunchAgents/com.sam.marginalia.plist`  
Repo template: `bridge/com.marginalia.bridge.plist` (uses `{{PLACEHOLDER}}` substitution via `install.sh`)  
Logs: `tail -f ~/Library/Logs/marginalia.log`  
Monitor: http://localhost:7731/monitor

### KOReader plugin (Palma)

```bash
# USB (fast)
adb -s c2fb36b9 push marginalia.koplugin/. /sdcard/koreader/plugins/marginalia.koplugin/

# Over Tailscale (when USB not connected)
adb connect 100.123.174.80:<port>  # port shown in Wireless Debugging on device
adb -s 100.123.174.80:<port> push ...
```

After pushing: restart KOReader (clean exit saves highlights; force-stop via `adb shell am force-stop org.koreader.launcher` skips the save).

Plugin settings stored on device at:
`/sdcard/koreader/settings.reader.lua` → `["marginalia"]` section  
Note queue: `/sdcard/koreader/settings/marginalia/note_queue.json`

---

## Environment

### Sam's setup
| Variable | Value |
|---|---|
| `MARGINALIA_AWS_PROFILE` | `openclaw-bedrock` |
| `MARGINALIA_MODEL_ID` | `openai.gpt-5.5` |
| `MARGINALIA_VAULT` | `/Users/sam.painter/Documents/Sam` |
| `MARGINALIA_PORT` | `7731` |
| `MARGINALIA_BOOKS_DIR` | `<vault>/Notes/Books` (default) |

### Config file (from `marginalia setup`)
`~/.marginalia.env` — auto-loaded by `marginalia serve`

### ADB
- **USB:** `c2fb36b9` (fastest, requires Allow USB debugging on first connect)
- **Tailscale:** `100.123.174.80` — connect with `adb connect 100.123.174.80:<port>`
- **mDNS:** `adb-c2fb36b9-kMQ5w3._adb-tls-connect._tcp` — only works on same network, adb kill-server then start-server sometimes needed
- **`~/.android`** was a broken symlink to `/Volumes/Storage/dev/.android` — replaced with `mkdir ~/.android`. If adb server fails to start, check this.

### Python
Bridge uses `/opt/homebrew/bin/python3` (3.14). The bridge runs standalone via `python3 bridge/server.py` — no venv required for Sam's setup since all deps are installed globally.

---

## Key files

| File | Purpose |
|---|---|
| `bridge/server.py` | HTTP dispatcher — all routes, request tracking, vault note saving |
| `bridge/xray_generator.py` | LLM routing: `_complete()` → fallback chain, `_invoke_openai_direct()`, `_invoke_anthropic_direct()`, `_invoke_bedrock()`, `_call_gpt()` (bedrock-mantle) |
| `bridge/rag.py` | Embedding + retrieval: `build_index()`, `retrieve()`, `retrieve_series()`, multi-backend (`_embed_local/openai/bedrock`) |
| `bridge/xray_cache.py` | Cache read/write at `~/.marginalia/cache/` |
| `bridge/series.py` | Series scope: which books to include in cross-book RAG |
| `bridge/monitor.py` | In-memory request tracker, HTML dashboard |
| `bridge/setup_wizard.py` | Interactive first-run wizard |
| `bridge/cli.py` | `marginalia serve` / `marginalia setup` entry points, auto-loads `~/.marginalia.env` |
| `marginalia.koplugin/main.lua` | Plugin main: Book Index fetch, highlight capture, note save, companion UI |
| `marginalia.koplugin/bridge.lua` | HTTP client — all bridge API calls (noteAsync is **blocking**, others async) |
| `marginalia.koplugin/piread_queue.lua` | Durable offline note queue (JSON on device) |
| `marginalia.koplugin/marginalia_xray.lua` | Book Index browser UI |

---

## Provider routing

Model IDs determine the routing path in `_invoke_one()`:

| Prefix | Routes to | Notes |
|---|---|---|
| `openai:` | `_invoke_openai_direct()` | Direct OpenAI API, requires `MARGINALIA_OPENAI_API_KEY` |
| `anthropic:` | `_invoke_anthropic_direct()` | Direct Anthropic API, requires `MARGINALIA_ANTHROPIC_API_KEY` |
| `openai.` | `_call_gpt()` (bedrock-mantle) | Internal AWS service, requires allowlisting |
| *(other)* | `_invoke_bedrock()` | Bedrock `invoke_model`, requires `MARGINALIA_AWS_PROFILE` |

Fallback chain auto-derived from primary model's provider — non-AWS primaries get cheap same-provider fallbacks, not useless AWS fallbacks. Override with `MARGINALIA_MODEL_CHAIN`.

---

## Testing

### Quick bridge health check
```bash
curl http://localhost:7731/ping            # → pong
curl http://localhost:7731/monitor/data   # → JSON stats
```

### Full API test suite (run from repo root)
```bash
cd bridge && python3 - << 'EOF'
import json, urllib.request
BOOK = {'book_title': 'The Expectant Father', 'book_author': 'Jennifer Ash Rudick', 'reading_pct': 52}
def post(path, data):
    r = urllib.request.urlopen(urllib.request.Request(
        f'http://localhost:7731{path}', json.dumps(data).encode(),
        headers={'Content-Type': 'application/json'}), timeout=60)
    return json.loads(r.read())
d = post('/book-index/init', BOOK)
print('cache hit:', d.get('status'), d.get('cached'))
d = post('/wiki', {**BOOK, 'entity_name': 'epidural', 'entity_kind': 'term'})
print('wiki:', d['response'][:80] if d.get('response') else d.get('error'))
EOF
```

### Device log stream
```bash
adb -s c2fb36b9 logcat -s 'KOReader:*'   # filters to KOReader logs only
# Key lines to watch:
# I marginalia: Book Index loaded from local cache
# I marginalia: requesting Book Index for <title>
# I marginalia_queue: enqueued note ... written true
# I marginalia: note synced to vault <path>
```

---

## Gotchas

1. **`launchctl kickstart -k` doesn't reload plist env vars** — use bootout + bootstrap for plist changes.
2. **Force-stopping KOReader loses in-memory highlights** — use clean exit (top menu → Exit) to save sidecar before restarts.
3. **ADB `~/.android` must exist** — was a broken symlink. If `adb start-server` fails with "Cannot mkdir '~/.android'", recreate: `rm ~/.android && mkdir ~/.android`.
4. **Zombie watcher loops** — previous sessions sometimes leave `while true; do sleep N; curl .../status/JOBID` bash loops that hammer dead job IDs. Check with `ps aux | grep "xray/status"`, kill with `pkill -9 -f "xray/status"`.
5. **Stale orphan bridge procs** — old `piread-bridge/server.py` processes (wrong path) ignore SIGTERM. Kill with `pkill -9 -f "piread-bridge/server.py"` if bridge logs appear doubled.
6. **rapidjson encodes empty Lua table as `{}` not `[]`** — a decoded `{}` (JSON object) can't be re-encoded as an array. The queue module works around this with `write()` rebuilding a fresh Lua array before encoding. Don't bypass this.
7. **`noteAsync` is blocking (intentional)** — `bridge.lua:noteAsync` uses a synchronous HTTP call (4s/7s timeout) specifically to avoid subprocess FD cross-talk with concurrent `/book-index/init` async calls. Don't change it back to async.
8. **KOReader settings key is `"marginalia"`** — the plugin stores settings under `G_reader_settings:readSetting("marginalia")`. Old installs had `"piread"`. If settings are missing after install, check the device's `settings.reader.lua` for the right key.

---

## Backlog / next steps

- [ ] **gpt-5.5 recovery** — when bedrock-mantle gpt-5.5 recovers, verify fallback chain auto-heals (test with bridge monitor)
- [ ] **Streaming responses** — would eliminate the blocking wait on long /wiki or /recap calls; requires plugin protocol change
- [ ] **Ollama support** — add `_invoke_ollama()` in xray_generator.py for local models; low friction, high value for privacy-conscious users
- [ ] **KOReader plugin manager distribution** — package as `.zip` for the in-app installer (currently requires manual ADB/file copy)
- [ ] **GitHub Actions CI** — build + lint on push; publish plugin zip to releases automatically
- [ ] **Calibre optional improvements** — current fallback to knowledge-only is good but no way to manually specify an EPUB path as override

---

## Decision log

| Date | Decision | Rationale |
|---|---|---|
| Jun 10 | Bedrock over direct OpenAI API | AWS access already set up; SigV4 auth avoids storing API keys |
| Jun 11 | Cohere via Bedrock for embeddings | No local GPU; Python 3.14 lacks torch wheels at the time |
| Jun 15 | `noteAsync` → blocking HTTP call | Async forks were cross-talking with concurrent `/book-index/init` subprocess via inherited pipe FDs |
| Jun 16 | piread → marginalia rebrand | Name was pi-specific; project is a general AI/KOReader/Obsidian integration layer |
| Jun 16 | `openai.gpt-5.5` kept as default (not `openai:gpt-4o`) | Sam uses bedrock-mantle; public users override via `marginalia setup` which sets the right model |
