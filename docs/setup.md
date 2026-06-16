# First-Time Setup

This guide walks you from zero to your first AI-powered book index in KOReader.

Estimated time: **10–15 minutes**.

---

## Prerequisites

- **macOS** (the bridge runs here)
- **Python 3.11+** — check with `python3 --version`
- **KOReader** installed on your e-reader (Boox, Kindle, or any Android device)
- Your e-reader and Mac on the **same Wi-Fi network**, or connected via [Tailscale](https://tailscale.com)
- An AI provider API key (OpenAI or Anthropic) — see [docs/providers.md](providers.md)

---

## Step 1 — Clone and install

```bash
git clone https://github.com/samfoy/marginalia
cd marginalia

# OpenAI users (recommended):
pip install -e ".[openai,embed]"

# Anthropic users:
pip install -e ".[anthropic,embed]"

# If you use AWS Bedrock:
pip install -e ".[bedrock]"
```

The `embed` extra installs `sentence-transformers` for local embeddings (no API key needed). Skip it if disk space is tight and you have an OpenAI key — it'll use `text-embedding-3-small` instead.

---

## Step 2 — Set your environment variables

Create a file `~/.marginalia.env`:

```bash
# Required: pick ONE provider block

# --- OpenAI ---
export MARGINALIA_OPENAI_API_KEY=sk-...
export MARGINALIA_MODEL_ID=openai:gpt-4o

# --- Anthropic ---
# export MARGINALIA_ANTHROPIC_API_KEY=sk-ant-...
# export MARGINALIA_MODEL_ID=anthropic:claude-opus-4-5

# --- AWS Bedrock ---
# export MARGINALIA_AWS_PROFILE=your-aws-profile
# export MARGINALIA_MODEL_ID=us.anthropic.claude-sonnet-4-6

# Required: path to your Obsidian vault
export MARGINALIA_VAULT=~/Documents/YourVaultName

# Optional: Calibre library (auto-detected if at ~/Calibre Library/)
# export MARGINALIA_CALIBRE_DB=~/Calibre Library/metadata.db
```

Source it in your shell:

```bash
echo 'source ~/.marginalia.env' >> ~/.zshrc
source ~/.marginalia.env
```

---

## Step 3 — Start the bridge

```bash
marginalia serve
```

You should see:

```
marginalia listening on :7731  model=openai:gpt-4o
```

Verify it's running:

```bash
curl http://localhost:7731/ping   # → pong
```

---

## Step 4 — Install the KOReader plugin

**Via ADB** (Android/Boox with USB or ADB wireless):

```bash
adb push marginalia.koplugin /sdcard/koreader/plugins/marginalia.koplugin
```

**Via file manager (MTP/USB):** Copy the `marginalia.koplugin/` folder to `koreader/plugins/` on your device.

Restart KOReader after copying.

---

## Step 5 — Configure the plugin

1. In KOReader, open the top menu → **Tools** (wrench icon) → **marginalia**
2. Set **Host** to your Mac's address:
   - Same Wi-Fi: `YourMacName.local` (check in System Settings → Sharing → Local hostname)
   - Tailscale: your Mac's Tailscale IP (run `tailscale ip -4` on the Mac)
3. Leave **Port** as `7731`
4. Tap **Test connection** — you should see ✓ Connected

---

## Step 6 — Open a book

Open any EPUB in KOReader. marginalia will automatically request a Book Index from the bridge. You'll see a brief loading indicator, then the Book Index is ready.

**If you have Calibre** with the book in its library, the index is generated from the full EPUB text (~30–120 seconds for a novel, cached after the first run).

**Without Calibre**, the index is generated from the LLM's knowledge (~10–30 seconds, no text extraction needed).

Once generated, the index is cached — subsequent opens are instant.

---

## Step 7 — Try it out

**Ask AI (text selection):** Select any text in your book → tap **Ask AI** → pick a mode (Who/What is this?, Explain, Story context, Translate). The answer appears in a popup, and if **Auto-capture** is on (default), the passage is highlighted in the book with the answer attached as a note.

**AI: Save Note:** Select text → tap **AI: Save Note** → optionally type context → Save. The passage becomes a highlight in the book and is appended to the book's Obsidian vault note.

**Browse the Book Index:** Top menu → Tools → marginalia → Browse Book Index. Navigate characters, locations, terms, and timeline events.

---

## Run as a background service (optional)

If you want the bridge to start automatically at login:

```bash
# Edit the plist — set your paths and env vars
$EDITOR bridge/com.sam.marginalia.plist

# Install
cp bridge/com.sam.marginalia.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sam.marginalia.plist

# Verify
curl http://localhost:7731/ping   # → pong
```

See the plist comments for the env vars to set.

---

## Troubleshooting

**"Cannot reach host:7731" in KOReader**
- Verify the bridge is running: `curl http://localhost:7731/ping`
- Check the host setting: `macbook.local` may not resolve over some networks — use the IP address instead
- If using Tailscale, make sure it's running on both devices

**Book Index never completes / times out**
- Check `~/Library/Logs/marginalia.log` for error details
- Try a simpler book first — very large books (600K+ words) can take several minutes
- Knowledge-only mode is much faster if you don't have Calibre

**Notes not appearing in Obsidian**
- Check `MARGINALIA_VAULT` is set to the correct vault root (the folder containing `.obsidian/`)
- Check the log for vault write errors
- Notes are queued offline — open a book in KOReader to trigger the sync flush

**Book not found in Calibre**
- The title/author in the EPUB metadata might not match Calibre. Run:
  ```bash
  cd bridge && python3 -c "from book_finder import find_epub; print(find_epub('Title Here', 'Author Here'))"
  ```
- Try title-only: `find_epub('Title Here', '')`

---

## Next steps

- [AI Provider Setup](providers.md) — model selection, cost estimates, fallback chains
- [Calibre Integration](calibre.md) — what Calibre unlocks and how to configure it
- [Obsidian Integration](obsidian.md) — vault setup, note structure, offline queue
