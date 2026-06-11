"""
pi_session.py — Warm pi --mode rpc session for the piread bridge.

Keeps a single pi process alive with a focused reading-assistant system
prompt and minimal extensions so responses come back fast (~2-4s vs ~8s
cold start). Thread-safe: uses a lock so concurrent /chat requests queue
behind each other rather than confusing the pi process.
"""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("piread.pi_session")

# Which pi binary to use
PI_BIN = os.environ.get("PIREAD_PI_BIN", "pi")

# Model — default to whatever the bridge uses, override with env
PI_MODEL = os.environ.get("PIREAD_PI_MODEL", "amazon-bedrock/us.anthropic.claude-sonnet-4-6")

# System prompt: tight reading-assistant identity, no tool preamble needed
SYSTEM_PROMPT = (
    "You are Pi, a reading assistant running inside KOReader on an e-ink device. "
    "The reader will ask questions about the book they are currently reading. "
    "Be concise — 3-4 sentences maximum unless a list is genuinely needed. "
    "Responses are read on a small screen so brevity is critical. "
    "Avoid spoilers for content past the reader's current position. "
    "Answer directly without preamble. No markdown headers or bullet points. "
    "Plain prose only. Short paragraphs of 1-2 sentences."
)

# How long to wait for a response before giving up
RESPONSE_TIMEOUT = int(os.environ.get("PIREAD_CHAT_TIMEOUT", "60"))


class PiSession:
    """
    Manages a single warm `pi --mode rpc` subprocess.
    Thread-safe via _lock — requests are serialized.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._ready = False
        self._start()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _start(self):
        """Spawn a fresh pi --mode rpc process."""
        if self._proc and self._proc.poll() is None:
            self._proc.kill()

        home = Path.home()
        aws_profile = os.environ.get("PIREAD_AWS_PROFILE", "openclaw-bedrock")
        aws_region  = os.environ.get("PIREAD_AWS_REGION", "us-east-1")

        args = [
            PI_BIN,
            "--mode", "rpc",
            "--no-session",          # ephemeral — no history between calls
            "--no-context-files",    # skip AGENTS.md / CLAUDE.md discovery
            "--no-extensions",       # no extension loading overhead
            "--no-skills",           # no skills loading overhead
            "--no-prompt-templates", # skip prompts dir
            "--no-themes",           # skip themes dir
            "--no-tools",            # reading-only: no bash/edit/write
            "--model", PI_MODEL,
            "--system-prompt", SYSTEM_PROMPT,
        ]

        env = {
            **os.environ,
            "AWS_PROFILE":  aws_profile,
            "AWS_REGION":   aws_region,
            "PI_RUNTIME":   "piread-bridge",
        }

        log.info("Starting warm pi session: %s", " ".join(args[:6]) + " ...")
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(home),
        )
        self._ready = False

        # Block until pi confirms ready (get_state response)
        ok = self._wait_ready(timeout=20)
        if ok:
            log.info("Warm pi session ready (pid=%d)", self._proc.pid)
        else:
            log.warning("pi session did not confirm ready within 20s — will retry on first request")

    def _wait_ready(self, timeout: int = 20) -> bool:
        """Send get_state and wait for a response."""
        req_id = "ready-check"
        self._send_raw({"type": "get_state", "id": req_id})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._readline(timeout=2)
            if line is None:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("id") == req_id or ev.get("type") == "get_state":
                self._ready = True
                return True
        return False

    def _ensure_alive(self):
        """Restart if the process has died."""
        if self._proc is None or self._proc.poll() is not None:
            log.warning("pi session died — restarting")
            self._start()

    # ── I/O ───────────────────────────────────────────────────────────────────

    def _send_raw(self, obj: dict):
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(json.dumps(obj) + "\n")
                self._proc.stdin.flush()
            except BrokenPipeError:
                log.warning("pi stdin broken pipe")

    def _readline(self, timeout: float = 1.0) -> Optional[str]:
        """Read one line from stdout with a timeout."""
        import select
        if self._proc is None or self._proc.stdout is None:
            return None
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        if ready:
            try:
                line = self._proc.stdout.readline()
                return line.strip() if line else None
            except Exception:
                return None
        return None

    # ── Public API ────────────────────────────────────────────────────────────

    def ask(self, question: str, book_context: str) -> str:
        """
        Send a question (with embedded book context) to the warm pi session.
        Blocks until pi responds. Thread-safe.
        Returns the assistant text or raises RuntimeError on timeout/error.
        """
        with self._lock:
            self._ensure_alive()

            # Embed book context into the message itself
            if book_context:
                message = f"{book_context}\n\nQuestion: {question}"
            else:
                message = question

            self._send_raw({"type": "prompt", "message": message})

            # Collect events until agent_end
            text = self._collect_response(timeout=RESPONSE_TIMEOUT)
            return text

    def _collect_response(self, timeout: int) -> str:
        """Read stdout events until agent_end, return the assistant text."""
        deadline = time.monotonic() + timeout
        collected_text = []

        while time.monotonic() < deadline:
            line = self._readline(timeout=1.0)
            if line is None:
                if self._proc and self._proc.poll() is not None:
                    raise RuntimeError("pi process exited unexpectedly")
                continue

            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                log.debug("non-JSON stdout: %s", line[:80])
                continue

            ev_type = ev.get("type")

            if ev_type == "agent_end":
                # Extract text from final messages array
                for msg in ev.get("messages", []):
                    if msg.get("role") != "assistant":
                        continue
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                                collected_text.append(part["text"].strip())
                    elif isinstance(content, str) and content.strip():
                        collected_text.append(content.strip())
                return "\n\n".join(t for t in collected_text if t) or "(no response)"

            # Skip streaming deltas — we collect from agent_end only

        raise RuntimeError(f"pi response timeout after {timeout}s")

    def close(self):
        if self._proc:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()


# ── Module-level singleton ─────────────────────────────────────────────────────

_session: Optional[PiSession] = None
_session_lock = threading.Lock()


def get_session() -> PiSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = PiSession()
        return _session
