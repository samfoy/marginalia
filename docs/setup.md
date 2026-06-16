# First-Time Setup

## The fast path

Four commands. Bridge running, plugin installed, ready to read.

```bash
git clone https://github.com/samfoy/marginalia
cd marginalia
pip install -e ".[openai,embed]"   # or [anthropic,embed] or [bedrock]
marginalia setup
```

The wizard validates your API key, finds your Obsidian vault, installs a background service (LaunchAgent on macOS, systemd on Linux), and prints the exact host/port to paste into the KOReader plugin.

Once setup finishes, **jump to [Install the KOReader plugin](#install-the-koreader-plugin)**.

---

## Prerequisites

- **Python 3.11+** on your desktop/laptop
- **KOReader** on your e-reader (Boox, Kindle, or any Android device)
- Network connectivity between the e-reader and the computer running the bridge — see [Network setup](#network-setup)
- An AI provider API key — [OpenAI](https://platform.openai.com/api-keys), [Anthropic](https://console.anthropic.com), or AWS Bedrock credentials (see [docs/providers.md](providers.md))
- **Calibre** (optional but recommended — see [docs/calibre.md](calibre.md))

---

## What the wizard configures

Running `marginalia setup`:

1. **AI provider** — pick OpenAI, Anthropic, or AWS Bedrock; enter your API key; the wizard validates it live before continuing
2. **Obsidian vault** — auto-detects vaults on your machine; pick one or enter a path manually; also asks for note folder locations:
   - **Book notes folder** (default: `Notes/Books`) — per-book highlight/AI notes
   - **Standalone captures folder** (default: `Notes/Captures`) — standalone notes from "Save as Note"
3. **Background service** — installs and starts a macOS LaunchAgent or Linux systemd unit so the bridge runs automatically at login
4. **KOReader instructions** — prints the exact host/port to paste into the plugin

Config is saved to `~/.marginalia.env` and loaded automatically by `marginalia serve`. Run `marginalia setup` again at any time to update any setting.

---

## Install the KOReader plugin

### Via ADB (Android/Boox)

```bash
adb push marginalia.koplugin /sdcard/koreader/plugins/marginalia.koplugin
```

### Via file manager (MTP / USB storage)

Copy the `marginalia.koplugin/` folder to `koreader/plugins/` on your device.

### Via SSH / SFTP

```bash
scp -r marginalia.koplugin user@device:/sdcard/koreader/plugins/
```

Restart KOReader after copying.

---

## Configure the plugin

1. Open KOReader → top menu → **Tools** (wrench) → **marginalia**
2. Set **Host** to your computer's IP or hostname (see [Network setup](#network-setup) if unsure)
3. Set **Port** to `7731`
4. Tap **Test connection** — you should see **✓ Connected**

> If you see **✗ Cannot reach**: double-check the IP, confirm the bridge is running (`curl http://localhost:7731/ping`), and check that your firewall allows port 7731.

---

## Try it

Open any EPUB in KOReader. marginalia silently requests a Book Index in the background — once done it's cached for instant access on future opens.

**Select text → Ask AI:** pick a mode (Who/What is this?, Explain, Story context, Translate). The answer appears and — with Auto-capture on (default) — the passage is highlighted in the book with the AI answer as the highlight note.

**Top menu → marginalia → Ask AI:** freeform chat grounded in your reading position. Tap **Save as Note** to save the Q&A as a standalone Obsidian note, or **To Book Note** to append it to the book's vault note.

---

## Network setup

The bridge runs on your computer; the KOReader plugin connects to it over the network.

### Same Wi-Fi (most common)

Find your computer's LAN IP:

```bash
# macOS
ipconfig getifaddr en0       # Wi-Fi
ipconfig getifaddr en1       # Ethernet

# Linux
ip addr show | grep "inet " | grep -v 127.0.0.1

# Windows (PowerShell)
(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notmatch '^127'}).IPAddress
```

Use this IP (e.g. `192.168.1.42`) when configuring the plugin. The setup wizard prints it for you at the end.

**mDNS (`.local` hostnames):** works on macOS and most Linux setups (needs `avahi-daemon` on Linux), but unreliable on corporate, mesh, or multi-subnet networks — use the IP when in doubt.

### Tailscale (different networks, travel)

1. Install Tailscale on both your computer and e-reader
2. Get your computer's Tailscale IP: `tailscale ip -4`
3. Use that IP in the plugin settings

### USB (Android debugging)

```bash
adb reverse tcp:7731 tcp:7731
```

Set the plugin host to `localhost`, port `7731`. The e-reader tunnels through USB to the bridge on your computer.

---

## Running as a service

The setup wizard handles this automatically. If you need to manage the service manually:

### macOS — LaunchAgent

```bash
tail -f ~/Library/Logs/marginalia.log
launchctl kill TERM gui/$(id -u)/com.marginalia.bridge   # temporary stop
launchctl bootout gui/$(id -u)/com.marginalia.bridge     # remove permanently
```

To reinstall (e.g. after changing the Python path):

```bash
launchctl bootout gui/$(id -u)/com.marginalia.bridge
marginalia setup   # re-runs install at the end
```

### Linux — systemd user service

```bash
systemctl --user status marginalia
journalctl --user -u marginalia -f   # live logs
systemctl --user restart marginalia
systemctl --user disable marginalia  # stop auto-start
```

### Windows

Run `marginalia serve` manually in a terminal, or add a batch file to Task Scheduler (trigger at logon):

```bat
@echo off
cd C:\path\to\marginalia
call .venv\Scripts\activate
marginalia serve
```

---

## Alternative setup paths

### Docker

```bash
cp .env.example .env   # uncomment your provider block
docker compose up -d
```

The bridge listens on port 7731. Point the KOReader plugin at your machine's IP.

### Manual (no wizard)

<details>
<summary>Configure by hand (CI, custom environments, scripted deploys)</summary>

```bash
git clone https://github.com/samfoy/marginalia
cd marginalia
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[openai,embed]"

export MARGINALIA_OPENAI_API_KEY=sk-...
export MARGINALIA_MODEL_ID=openai:gpt-4o
export MARGINALIA_VAULT=~/Documents/YourVault
# Optional:
# export MARGINALIA_BOOKS_DIR=Notes/Books
# export MARGINALIA_CAPTURES_DIR=Notes/Captures

marginalia serve
```

For permanent configuration, write the exports to `~/.marginalia.env` — `marginalia serve` loads it automatically.

</details>

---

## Troubleshooting

**"Cannot reach host:7731" in KOReader**
- Verify the bridge is running: `curl http://localhost:7731/ping`
- Use the IP address instead of a hostname — mDNS (`.local`) is unreliable on many networks
- Temporarily disable your firewall to test; if that fixes it, add an exception for port 7731
- On different networks? Use Tailscale or USB forwarding (see [Network setup](#network-setup))

**macOS: `pip install` fails with "externally managed environment"**
- You need a venv: `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[openai,embed]"`

**Book Index never completes / times out**
- Check `~/Library/Logs/marginalia.log` (macOS) or `journalctl --user -u marginalia` (Linux)
- Large books (600K+ words) can take several minutes on first generation
- Knowledge-only mode (without Calibre) is faster but less accurate

**Calibre not found**
- Override: `export MARGINALIA_CALIBRE_DB="path/to/your/Calibre Library"`
- See [docs/calibre.md](calibre.md) for details

**Notes not appearing in Obsidian**
- Confirm `MARGINALIA_VAULT` points to the folder containing `.obsidian/`
- Notes queue offline and flush automatically on next book open — check `note_queue.json` on device if stuck

---

## Next steps

- [docs/providers.md](providers.md) — model selection, cost estimates, multiple providers
- [docs/calibre.md](calibre.md) — full EPUB extraction, RAG, series intelligence
- [docs/obsidian.md](obsidian.md) — vault structure, note formats, offline queue
