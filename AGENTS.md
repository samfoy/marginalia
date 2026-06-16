# marginalia — Project Agent Instructions

This is the **marginalia** project: a Python bridge + KOReader Lua plugin for AI-powered reading assistance. Repo is at `~/Projects/piread/` (remote: `github.com/samfoy/marginalia`).

Before starting any work, read `HANDOVER.md` for full context.

---

## Running environment

The bridge is already running as a LaunchAgent on port 7731. **Do not stop it without cause** — Sam's KOReader plugin depends on it being alive.

```bash
curl http://localhost:7731/ping            # verify bridge is alive
curl http://localhost:7731/monitor/data   # check current state
```

Logs: `tail -f ~/Library/Logs/marginalia.log`

## Restarting the bridge

**Code changes only** (no plist edit):
```bash
launchctl kickstart -k gui/$(id -u)/com.sam.marginalia
```

**Plist changes** (env vars, Python path):
```bash
launchctl bootout gui/$(id -u)/com.sam.marginalia; sleep 6
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sam.marginalia.plist
# retry if fails — teardown race is normal, wait ~6s and try again
```

## Deploying to the Palma

```bash
# Check device is connected
adb devices

# USB (device ID c2fb36b9)
adb -s c2fb36b9 push marginalia.koplugin/. /sdcard/koreader/plugins/marginalia.koplugin/

# Over Tailscale (when USB not connected) — get port from Wireless Debugging on device
adb connect 100.123.174.80:<PORT>
```

After pushing the plugin, KOReader needs a restart. Use **clean exit** (top menu → Exit) to avoid losing in-memory highlights. Only force-stop if necessary: `adb shell am force-stop org.koreader.launcher`.

## Testing

Run bridge API tests after any change to `bridge/`:
```bash
cd bridge && python3 -c "
import json, urllib.request
def post(p, d): 
    r = urllib.request.urlopen(urllib.request.Request('http://localhost:7731'+p, json.dumps(d).encode(), headers={'Content-Type':'application/json'}), timeout=60)
    return json.loads(r.read())
BOOK = {'book_title':'The Expectant Father','book_author':'Jennifer Ash Rudick','reading_pct':52}
print('ping:', urllib.request.urlopen('http://localhost:7731/ping').read())
print('cache:', post('/book-index/init', BOOK).get('status'))
print('wiki:', post('/wiki', {**BOOK,'entity_name':'epidural','entity_kind':'term'}).get('response','')[:80])
"
```

Validate Lua syntax before pushing plugin changes:
```bash
cd marginalia.koplugin
for f in *.lua; do luajit -b "$f" /tmp/x.out 2>&1 && echo "$f OK" || echo "$f FAILED"; done
rm -f /tmp/x.out
```

## Code conventions

- **Env vars:** all `MARGINALIA_*` prefix. Document new ones in `bridge/server.py` docstring, `docs/providers.md` table, `.env.example`, and README config table.
- **LLM calls:** always route through `_complete()` in `xray_generator.py` — never call `_call_gpt()` or `_invoke_bedrock()` directly from handlers. `_complete()` provides the fallback chain and circuit breaker.
- **`noteAsync` is blocking (intentional):** the bridge.lua `noteAsync` uses a synchronous HTTP call. This is deliberate — async forks cross-talk with concurrent `/book-index/init` subprocesses via inherited FDs. Don't change it back to async.
- **Queue writes:** always use `Queue.enqueue()`. The `write()` function rebuilds a fresh Lua array before encoding to avoid rapidjson's object-flagged-table bug (decoded `{}` re-encodes as `{}` not `[]`).
- **Commits:** conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`).

## Critical gotchas

- **`launchctl kickstart -k` doesn't reload plist env vars** — it restarts the process but reuses the bootstrapped plist. Use bootout + bootstrap for env var changes.
- **KOReader settings key is `"marginalia"`** (was `"piread"` before Jun 16 rebrand). If a device has old settings, they live under the old key and won't be loaded.
- **`MARGINALIA_VAULT_HOST` does not exist** — Docker compose uses `MARGINALIA_VAULT` for both the host bind mount source and the container env var. Don't add a separate `VAULT_HOST` variable.
- **Systemd env values with spaces must be quoted:** `Environment="MARGINALIA_VAULT=/path/with spaces"`. The wizard handles this but manual edits need care.
- **`~/.android` must be a real directory** — was a broken symlink. If `adb` fails to start, check: `ls -la ~/.android`.

## File layout

```
bridge/
  server.py          HTTP dispatcher (all routes)
  xray_generator.py  LLM routing + fallback chain
  rag.py             Embedding + retrieval
  monitor.py         In-memory request dashboard
  setup_wizard.py    Interactive setup wizard
  cli.py             marginalia serve / setup entry points
  install.sh         macOS LaunchAgent installer
  marginalia.service Linux systemd unit template
  com.marginalia.bridge.plist  LaunchAgent plist template ({{PLACEHOLDER}} substitution)

marginalia.koplugin/
  main.lua           Plugin main (Book Index fetch, Ask AI, Save Note, companion)
  bridge.lua         HTTP client (noteAsync = blocking; others = async subprocesses)
  marginalia_queue.lua  Durable offline note queue
  marginalia_xray.lua   Book Index browser UI
  marginalia_context.lua, marginalia_cache.lua, marginalia_async.lua

docs/
  setup.md     First-time setup guide (cross-platform)
  providers.md AI provider setup (OpenAI, Anthropic, Bedrock)
  calibre.md   Calibre integration
  obsidian.md  Obsidian vault integration

HANDOVER.md  Full project context for agent handover
```
