# First-Time Setup

This guide walks you from zero to your first AI-powered Book Index in KOReader.

Estimated time: **10–15 minutes**.

---

## What you need

- **Python 3.11+** on your desktop/laptop (macOS, Linux, or Windows)
- **KOReader** on your e-reader (Boox, Kindle, or any Android device)
- **Network connectivity** between your e-reader and computer (see [Network setup](#network-setup) below)
- An AI provider API key — [OpenAI](https://platform.openai.com/api-keys), [Anthropic](https://console.anthropic.com), or AWS Bedrock credentials (see [docs/providers.md](providers.md))
- **Calibre** (optional but recommended — see [docs/calibre.md](calibre.md))

---

## Step 1 — Clone and install

```bash
git clone https://github.com/samfoy/marginalia
cd marginalia
```

### macOS / Linux (create a virtual environment first)

macOS Homebrew Python and most Linux distros enforce isolated installs. Use a venv:

```bash
python3 -m venv .venv
source .venv/bin/activate    # on Windows: .venv\Scripts\activate

# then install your provider:
pip install -e ".[openai,embed]"    # OpenAI + local embeddings
pip install -e ".[anthropic,embed]" # Anthropic + local embeddings
pip install -e ".[bedrock]"         # AWS Bedrock
pip install -e ".[all]"             # everything
```

> **Tip:** Add `source /path/to/marginalia/.venv/bin/activate` to your shell profile so `marginalia serve` is always available.

### Windows

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[openai,embed]"
```

The bridge runs fine on Windows — only the auto-start instructions differ (see [Running as a service](#running-as-a-service)).

---

## Step 2 — Configure

Copy the example env file and fill it in:

```bash
cp .env.example .env
$EDITOR .env   # uncomment your provider block and set MARGINALIA_VAULT
```

Minimum required settings:

```bash
# .env — pick ONE provider block

# OpenAI:
export MARGINALIA_OPENAI_API_KEY=sk-...
export MARGINALIA_MODEL_ID=openai:gpt-4o

# Anthropic:
# export MARGINALIA_ANTHROPIC_API_KEY=sk-ant-...
# export MARGINALIA_MODEL_ID=anthropic:claude-opus-4-5

# Your Obsidian vault:
export MARGINALIA_VAULT=~/Documents/YourVault
```

Then load it:

```bash
# macOS / Linux — use set -a so vars are exported to child processes:
set -a && source .env && set +a

# Windows (PowerShell):
Get-Content .env | Where-Object { $_ -match '^(export\s+)?[A-Z_]+=' } | ForEach-Object { $line = $_ -replace '^export\s+',''; $k,$v=$line.Split('=',2); [System.Environment]::SetEnvironmentVariable($k.Trim(),$v.Trim(),'Process') }
```

> **Note:** This only applies to the current terminal session. For permanent configuration use the LaunchAgent (`install.sh`) or systemd unit — they bake env vars into the service definition.

---

## Step 3 — Start the bridge

```bash
marginalia serve
# marginalia listening on :7731  model=openai:gpt-4o
```

Verify:

```bash
curl http://localhost:7731/ping   # → pong
```

---

## Network setup

The bridge runs on your computer and the KOReader plugin connects to it over the network. You need to know the IP address or hostname of your computer that the e-reader can reach.

### Same Wi-Fi network (most common)

Find your computer's LAN IP:

```bash
# macOS
ipconfig getifaddr en0         # Wi-Fi
ipconfig getifaddr en1         # Ethernet

# Linux
ip addr show | grep "inet " | grep -v 127.0.0.1

# Windows (PowerShell)
(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notmatch '^127'}).IPAddress
```

Use this IP address (e.g. `192.168.1.42`) when configuring the plugin.

**mDNS (`.local` hostnames):** `yourcomputer.local` works on macOS and most Linux setups (needs `avahi-daemon` on Linux). It's unreliable on corporate, mesh, or multi-subnet networks — use the IP when in doubt.

### Tailscale (different networks, travel)

If you use [Tailscale](https://tailscale.com):

1. Install Tailscale on both your computer and e-reader
2. Get your computer's Tailscale IP: `tailscale ip -4`
3. Use that IP in the plugin settings

Tailscale works across networks and through NAT — it's the most reliable option if your e-reader leaves your home network.

### USB (Android debugging)

If you've set up ADB and want to use USB port forwarding:

```bash
adb reverse tcp:7731 tcp:7731
```

Then set the plugin host to `localhost` and port `7731`. The e-reader will tunnel through the USB connection to the bridge on your computer.

---

## Step 4 — Install the KOReader plugin

### Via ADB (Android/Boox)

```bash
adb push marginalia.koplugin /sdcard/koreader/plugins/marginalia.koplugin
```

### Via file manager (MTP / USB storage)

Copy the `marginalia.koplugin/` folder to `koreader/plugins/` on your device.

### Manual copy (SSH / SFTP)

```bash
scp -r marginalia.koplugin user@device:/sdcard/koreader/plugins/
```

Restart KOReader after copying.

---

## Step 5 — Configure the plugin

1. Open KOReader → top menu → **Tools** (wrench) → **marginalia**
2. Set **Host** to your computer's IP or hostname (from [Network setup](#network-setup) above)
3. Set **Port** to `7731`
4. Tap **Test connection** — you should see **✓ Connected**

> If you see **✗ Cannot reach**: double-check the IP, confirm the bridge is running (`curl http://localhost:7731/ping`), and check that your firewall allows port 7731.

---

## Step 6 — Open a book and try it

Open any EPUB in KOReader. marginalia silently requests a Book Index in the background. You'll see a brief loading indicator; once done the index is cached for instant access on future opens.

**Select text → Ask AI:** Pick a mode (Who/What is this?, Explain, Story context, Translate). The answer pops up and — with Auto-capture on (default) — the passage is highlighted in the book with the AI answer as the highlight note.

**AI: Save Note:** Select text → save → the passage becomes a KOReader highlight and is appended to the book's Obsidian vault note.

**Browse Book Index:** Top menu → Tools → marginalia → Browse Book Index.

---

## Running as a service

### macOS — LaunchAgent (starts at login)

```bash
cd bridge
./install.sh    # detects Python, prompts for vault path, installs and starts the service
```

To manage it afterwards:
```bash
tail -f ~/Library/Logs/marginalia.log
launchctl kill TERM gui/$(id -u)/com.marginalia.bridge   # temporary stop (KeepAlive restarts in ~10s)
launchctl bootout gui/$(id -u)/com.marginalia.bridge   # permanent stop/remove
```

### Linux — systemd user service

```bash
# Edit bridge/marginalia.service: update ExecStart path and Environment vars
cp bridge/marginalia.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now marginalia

# Check status and logs
systemctl --user status marginalia
journalctl --user -u marginalia -f
```

### Windows — Task Scheduler or manual

The simplest approach is to run `marginalia serve` manually in a terminal when you want to use it.

To start it automatically at login, create a batch file:

```bat
@echo off
cd C:\path\to\marginalia
call .venv\Scripts\activate
marginalia serve
```

Then add it to Task Scheduler: open **Task Scheduler** → **Create Basic Task** → trigger at logon → action is your batch file.

---

## Troubleshooting

**"Cannot reach host:7731" in KOReader**
- Verify the bridge is running: `curl http://localhost:7731/ping`
- Use the IP address instead of a hostname — mDNS (`.local`) is unreliable on many networks
- Temporarily disable your firewall to test: if that fixes it, add an exception for port 7731
- If on different networks, use Tailscale or USB forwarding (see [Network setup](#network-setup))

**macOS: `pip install` fails with "externally managed environment"**
- You need a venv: `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[openai,embed]"`

**Book Index never completes / times out**
- Check `~/Library/Logs/marginalia.log` (macOS) or `journalctl --user -u marginalia` (Linux)
- Very large books (600K+ words) can take several minutes on first generation
- Knowledge-only mode (without Calibre) is faster

**Calibre not found**
- Check your library path (from repo root with venv active): `python3 -c "import sys; sys.path.insert(0,'bridge'); from book_finder import CALIBRE_LIB; print(CALIBRE_LIB)"`
- Override with: `export MARGINALIA_CALIBRE_DB="path/to/your/Calibre Library"`
- See [docs/calibre.md](calibre.md) for details

**Notes not appearing in Obsidian**
- Confirm `MARGINALIA_VAULT` points to the folder containing `.obsidian/`
- Notes queue offline and flush automatically when you open a book — check the note_queue.json file on device if they seem stuck
- Check bridge logs for write errors

---

## Next steps

- [docs/providers.md](providers.md) — model selection, cost estimates, multiple providers
- [docs/calibre.md](calibre.md) — full EPUB extraction, RAG, series intelligence
- [docs/obsidian.md](obsidian.md) — vault structure, note format, offline queue
