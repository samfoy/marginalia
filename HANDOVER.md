# marginalia — Handover Document

*Last updated: 2026-08-05 | Version: 0.10.0*

---

## What this is

marginalia is a bridge server + KOReader plugin that brings AI reading intelligence to e-readers. It generates a "Book Index" (like Kindle X-Ray) from EPUB text via any configured LLM, answers reading questions using position-bounded RAG (no spoilers by construction), precomputes offline translations of foreign-language passages, captures highlights and notes to both KOReader and Obsidian, and syncs everything over a local HTTP bridge.

**GitHub:** https://github.com/samfoy/marginalia
**Repo dir:** `/home/sam/workspace/marginalia`

> Older revisions of this document referenced `~/Projects/piread/`. That path is historical — the working copy now lives in `~/workspace/marginalia`.

---

## Architecture

```
KOReader (Palma)                    Bridge host (port 7731)
--------------------                ------------------------------------
marginalia.koplugin  <-- Tailscale/LAN -->  bridge/server.py
  - Book Index browser                          ├── xray_generator.py     (LLM calls, fallback chain)
  - Ask AI (highlight+vault)                    ├── rag.py                (embeddings, retrieval)
  - Translate to English (local)                ├── xray_cache.py         (~/.marginalia/cache/)
  - AI: Save Note                               ├── translation_sidecar.py (translation index build)
  - Recap / Wiki / Chat / Section                ├── translation_text.py   (normalize + hash keys)
  - Offline note queue                          ├── series.py             (cross-book scope)
                                                ├── mentions.py           (jump-to-chapter index)
                                                ├── book_finder.py        (Calibre lookup)
                                                ├── bookorbit_source.py   (BookOrbit EPUB source)
                                                ├── epub_extract.py       (EPUB → text + candidates)
                                                └── monitor.py            (request dashboard)
```

The bridge runs as a macOS LaunchAgent (`com.sam.marginalia`), always-on, restarts on crash. A Linux systemd unit template ships as `bridge/marginalia.service`.

---

## Current state (2026-08-05)

- **Version:** `0.10.0` — `pyproject.toml` and `marginalia.koplugin/_meta.lua` are aligned. Keep them in lockstep; a packaging test asserts the plugin version.
- **Released:** `v0.10.0` tagged on `main` (commit `c7f6aac`), published to GitHub Releases with a `marginalia.koplugin-v0.10.0.zip` asset.
- **Tests:** 225 passing (`python3 -m pytest tests/ -q`).
- **Repo topic:** `koreader-plugin` is set — this is what makes the App Store browser discover the repo. Do not remove it.

### What works
- Book Index generation from EPUB text (Calibre or BookOrbit) or model knowledge (fallback)
- Position-bounded RAG — /recap, /wiki, /chat, /section all spoiler-safe
- Series-aware cross-book context (prior books included, future books excluded)
- **Offline translations** — precomputed during Book Index build, delivered in the same response, cached per book on device (see below)
- Ask AI → auto-highlights passage in KOReader with AI answer as note, syncs to Obsidian
- AI: Save Note → saves highlight in book, appends to Obsidian vault note
- Offline note queue (durable across connection loss)
- Model fallback chain with per-model circuit breaker (120s cooldown, auto-recovery)
- `marginalia setup` wizard, Docker (`docker compose up -d`)
- App Store install + update via `samfoy/marginalia`

### Known issues / active monitoring
- **Streaming:** companion endpoints block until complete. Would require a protocol change in the KOReader plugin (chunked response handling).
- **`/v1/chat/completions`:** Bedrock-only OpenAI-compat proxy for KO Assistant. Intentionally bypasses the provider fallback chain — Bedrock clients only.
- **Hermes `read_file` binary false positive:** a line that is mostly non-ASCII (e.g. a solid `─` rule at ~78% multi-byte chars) makes Hermes' `read_file` report this whole file as binary, even though it is clean UTF-8 with zero NUL/control bytes. The architecture diagram now uses ASCII `-` for its horizontal rules to avoid this. If it recurs, either lower the non-ASCII density of the offending line or read it with `python3 -c "print(open('HANDOVER.md').read())"`.

---

## Offline translations (v0.10.0)

**Design decision:** translations ride the *existing* Book Index lifecycle. There is no adjacent sidecar to transfer, no EPUB modification, and no BookOrbit involvement.

Flow:
1. While building a Book Index, the bridge extracts marked foreign-language passages (`epub_extract.extract_translation_candidates`).
2. `translation_sidecar.build_translation_index()` batches them to the LLM for language classification + English translation.
3. The result is stored on the record as `translation_index` and returned by `/book-index/init` and `/book-index/status/<id>`.
4. The plugin validates it and caches it per book. Lookups at reading time are local.

**Translate to English is strictly local.** A hit renders immediately and is *not* auto-captured as an AI lookup. A miss shows exactly `No precomputed translation found for this selection.` There is **no** bridge or network fallback from the Translate action. Other Ask AI modes keep their existing bridge behavior and capture semantics.

**Edition binding:** each index carries `source_epub.koreader_partial_md5` (KOReader's sampled-offset partial MD5) plus `size_bytes` and `sha256`. The device recomputes and rejects a mismatch, so an index built for a different edition is never served. The plugin also re-derives each entry's normalization and hash key, so a tampered index cannot inject content.

**Legacy caches** are backfilled automatically on the next online freshness check via a single-flight job that generates from an immutable EPUB snapshot, then merges into the *latest* record (never the stale snapshot).

**Manual export** remains available for inspection only:
```bash
marginalia translations "/path/to/Book.epub" [--batch-size N]
```
This writes `Book.marginalia-translations.json` beside the EPUB. KOReader does **not** require that file.

---

## Deployment

### Bridge (macOS)

Pick up code changes (no plist edit):
```bash
launchctl kickstart -k gui/$(id -u)/com.sam.marginalia
```

Pick up **plist changes** (env vars, Python path):
```bash
launchctl bootout gui/$(id -u)/com.sam.marginalia
sleep 6  # wait for teardown race
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sam.marginalia.plist
# retry if it fails — race condition is normal
```

Live plist: `~/Library/LaunchAgents/com.sam.marginalia.plist`
Repo template: `bridge/com.marginalia.bridge.plist` (`{{PLACEHOLDER}}` substitution via `install.sh`)
Logs: `tail -f ~/Library/Logs/marginalia.log` · Monitor: http://localhost:7731/monitor

### KOReader plugin

**App Store (preferred):** App Store → Plugins → gear → *Install plugin from URL* → `samfoy/marginalia`. Later releases: *Check plugin updates*. The store downloads the repo zipball, validates `_meta.lua`, and compares versions/SHAs — so a release must land on `main` **and** bump `_meta.lua`. Updates replace plugin code but preserve settings, cache, and queued notes (all stored outside the plugin directory, enforced by a test).

**ADB (dev loop):**
```bash
adb -s c2fb36b9 push marginalia.koplugin/. /sdcard/koreader/plugins/marginalia.koplugin/
# Over Tailscale: adb connect 100.123.174.80:<port>   (port from Wireless Debugging)
```

After pushing: restart KOReader. Clean exit (top menu → Exit) saves highlights; force-stop (`adb shell am force-stop org.koreader.launcher`) skips the save.

On-device state:
`/sdcard/koreader/settings.reader.lua` → `["marginalia"]` · queue: `/sdcard/koreader/settings/marginalia/note_queue.json`

---

## Releasing

1. Bump **both** `marginalia.koplugin/_meta.lua` and `pyproject.toml`, and the version assertion in `tests/test_plugin_package.py`.
2. `python3 -m pytest tests/ -q` (expect all green) and validate Lua: `for f in marginalia.koplugin/*.lua; do luac5.1 -o /dev/null "$f"; done`.
3. Merge to `main` and push — the App Store reads `main`, so an unpushed branch ships nothing.
4. Tag + release with a plugin zip:
   ```bash
   zip -rq /tmp/marginalia.koplugin-vX.Y.Z.zip marginalia.koplugin \
     -x "*__pycache__*" -x "*.pyc" -x "*.marginalia-translations.json"
   gh release create vX.Y.Z /tmp/marginalia.koplugin-vX.Y.Z.zip --title "..." --notes "..."
   ```
5. Verify the *published* artifact, not the working tree:
   ```bash
   curl -sSL -o repo.zip https://api.github.com/repos/samfoy/marginalia/zipball/vX.Y.Z
   # unzip and confirm _meta.lua version + all required .lua modules are present
   ```

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

Config file (from `marginalia setup`): `~/.marginalia.env` — auto-loaded by `marginalia serve`.
Full env var list: `bridge/server.py` docstring and `docs/providers.md`.

### ADB
- **USB:** `c2fb36b9` · **Tailscale:** `100.123.174.80`
- **`~/.android` must be a real directory** — was a broken symlink. If `adb start-server` fails, check it.

---

## Key files

| File | Purpose |
|---|---|
| `bridge/server.py` | HTTP dispatcher — all routes, request tracking, vault note saving |
| `bridge/xray_generator.py` | LLM routing: `_complete()` → fallback chain + circuit breaker |
| `bridge/rag.py` | Embedding + retrieval, multi-backend |
| `bridge/xray_cache.py` | Cache at `~/.marginalia/cache/`; atomic writes, `merge_translation_index()`, `find_all_by_title_author()` |
| `bridge/translation_sidecar.py` | `build_translation_index()` (embedded) + `generate_translation_sidecar()` (manual export) |
| `bridge/translation_text.py` | Selection normalization + stable lookup keys |
| `bridge/epub_extract.py` | EPUB → text, `extract_translation_candidates()` |
| `bridge/bookorbit_source.py` | BookOrbit server as an EPUB source |
| `marginalia.koplugin/main.lua` | Plugin main; Translate routing, document-generation guards |
| `marginalia.koplugin/marginalia_translation_sidecar.lua` | Device-side validate + lookup |
| `marginalia.koplugin/marginalia_translation_text.lua` | Portable normalize + djb2-32 hash (mirrors the Python side) |
| `marginalia.koplugin/marginalia_queue.lua` | Durable offline note queue |
| `marginalia.koplugin/marginalia_xray.lua` | Book Index browser UI |

> Note: there is no `piread_queue.lua`; the queue module was renamed `marginalia_queue.lua` in the Jun 16 rebrand.

---

## Provider routing

| Prefix | Routes to | Notes |
|---|---|---|
| `openai:` | `_invoke_openai_direct()` | Direct OpenAI API, requires `MARGINALIA_OPENAI_API_KEY` |
| `anthropic:` | `_invoke_anthropic_direct()` | Direct Anthropic API, requires `MARGINALIA_ANTHROPIC_API_KEY` |
| `openai.` | `_call_gpt()` (bedrock-mantle) | Internal AWS service, requires allowlisting |
| *(other)* | `_invoke_bedrock()` | Bedrock `invoke_model`, requires `MARGINALIA_AWS_PROFILE` |

Fallback chain auto-derived from the primary model's provider. Override with `MARGINALIA_MODEL_CHAIN`.

---

## Testing

```bash
python3 -m pytest tests/ -q                       # 225 tests
curl http://localhost:7731/ping                   # → pong
curl http://localhost:7731/monitor/data           # → JSON stats

# Lua syntax (host lua5.1 or luajit)
for f in marginalia.koplugin/*.lua; do luac5.1 -o /dev/null "$f" || echo "$f FAILED"; done
```

The Lua contract tests (`tests/test_translation_*_lua.py`) drive the real device modules from the host by stubbing `util.partialMD5`, `ffi/sha2`, and `libs/libkoreader-lfs` via `package.preload`. To hand-verify translations end-to-end, build a small EPUB with `lang`-tagged passages, call `build_translation_index()` with a stub completer, then drive `TranslationSidecar.validate` / `lookupDocument` against it. Note `lookupDocument` returns `(translation_string, entry_or_reason)` — not a table.

Device log stream:
```bash
adb -s c2fb36b9 logcat -s 'KOReader:*'
```

---

## Gotchas

1. **`launchctl kickstart -k` doesn't reload plist env vars** — use bootout + bootstrap.
2. **Force-stopping KOReader loses in-memory highlights** — use clean exit.
3. **ADB `~/.android` must exist** — recreate with `mkdir ~/.android` if `adb start-server` fails.
4. **Zombie watcher loops** — kill with `pkill -9 -f "xray/status"`.
5. **Stale orphan bridge procs** — `pkill -9 -f "piread-bridge/server.py"` if logs appear doubled.
6. **rapidjson encodes an empty Lua table as `{}` not `[]`** — `Queue.write()` rebuilds a fresh array before encoding. Don't bypass it.
7. **`noteAsync` is blocking (intentional)** — avoids subprocess FD cross-talk with concurrent `/book-index/init`. Don't make it async.
8. **KOReader settings key is `"marginalia"`** — old installs used `"piread"`.
9. **Version must be bumped in three places** — `_meta.lua`, `pyproject.toml`, and the assertion in `tests/test_plugin_package.py`.
10. **`MARGINALIA_VAULT_HOST` does not exist** — compose uses `MARGINALIA_VAULT` for both the bind mount and the container env var.
11. **Callbacks are generation-guarded** — `_document_generation` invalidates in-flight callbacks when a different book is opened. Preserve this when adding async work.
12. **`force` must stay wired end to end** — the plugin's Reindex sends `force=true` and clears only its *device* cache. The bridge must skip **both** `find_all_by_title_author` and `find_by_title_author` (see `_wants_rebuild`), or reindex silently re-serves the same record. This was broken once: Reindex was the only recovery path and it did nothing. Accept bool/int/string forms — the value crosses Lua/rapidjson, and `isinstance(True, int)` is True so check bool first.
13. **An empty `translation_index` is NOT complete** — zero entries is indistinguishable from a failed build. `_translation_index_valid()` rejects it on purpose. Accepting it created a permanently stuck state: served from cache forever, never retried, device shows no translations. Don't "optimise" this check away.

---

## Backlog / next steps

- [ ] **Real-book validation** — generate a translation index for an actual multilingual book (e.g. Lolita) and confirm on-device lookups; only synthetic EPUBs have been exercised end-to-end so far.
- [ ] **Device deployment of 0.10.0** — push to the Palma (or install via App Store) and confirm the legacy-cache backfill path on a book cached before 0.10.0.
- [ ] **Streaming responses** — eliminates the blocking wait on long /wiki or /recap calls; requires a plugin protocol change.
- [ ] **Ollama support** — add `_invoke_ollama()` in `xray_generator.py` for local models.
- [ ] **GitHub Actions CI** — run pytest + Lua syntax on push; attach the plugin zip to releases automatically.
- [ ] **Calibre override** — no way to manually specify an EPUB path today.

---

## Decision log

| Date | Decision | Rationale |
|---|---|---|
| Jun 10 | Bedrock over direct OpenAI API | AWS access already set up; SigV4 avoids storing API keys |
| Jun 11 | Cohere via Bedrock for embeddings | No local GPU; Python 3.14 lacked torch wheels |
| Jun 15 | `noteAsync` → blocking HTTP call | Async forks cross-talked with concurrent `/book-index/init` via inherited pipe FDs |
| Jun 16 | piread → marginalia rebrand | Name was pi-specific; project is a general AI/KOReader/Obsidian layer |
| Jun 16 | `openai.gpt-5.5` kept as default | Sam uses bedrock-mantle; public users override via `marginalia setup` |
| Aug 4 | Abandoned the BookOrbit sidecar-delivery branch | BookOrbit was never required; a Marginalia-owned path is simpler |
| Aug 4 | Translations embedded in the Book Index, not an adjacent sidecar | One owner already controls generation, transfer, caching, and lookup — no parallel subsystem, no second product coupled in |
| Aug 4 | Edition binding via KOReader partial MD5 | Matches what the device can cheaply recompute, so a wrong-edition index is never served |
| Aug 5 | Translate has no network fallback | Keeps the action instant and predictable offline; a miss is reported plainly instead of silently costing a bridge call |
