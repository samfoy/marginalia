"""
pi_xray.py — X-Ray generation via pi with tools enabled.

Spawns pi as a subprocess with read/bash tools so it can read the EPUB
directly from Calibre, look up real-world references, and apply careful
judgment about spoiler-free character descriptions.

Pi acts as an orchestrator (conductor mode): it reads the book in sections,
extracts entities in parallel passes, then merges into a final X-Ray JSON.
"""

import json
import logging
import os
import select
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("piread.pi_xray")

PI_BIN   = os.environ.get("PIREAD_PI_BIN", "pi")
PI_MODEL = os.environ.get("PIREAD_PI_MODEL", "amazon-bedrock/us.anthropic.claude-sonnet-4-6")
SKIP_PI_XRAY = os.environ.get("PIREAD_SKIP_PI_XRAY", "").lower() in ("1", "true", "yes")

# Timeout for X-Ray generation — big books take time
XRAY_TIMEOUT = int(os.environ.get("PIREAD_XRAY_TIMEOUT", "600"))

SCHEMA = """{
  "book_type": "fiction" or "nonfiction",
  "characters": [
    {
      "name": "Full name as used in the book",
      "role": "Under 40 chars. e.g. 'Protagonist' or 'Antagonist'",
      "description": "Under 200 chars. WHO this person IS at first introduction: appearance, personality, background, occupation, relationships. ZERO plot events. ZERO deaths. ZERO fates. A reader on page 1 should be safe reading this.",
      "aliases": ["other names used for this character"],
      "first_appearance_pct": 0
    }
  ],
  "locations": [
    {
      "name": "Place name",
      "description": "Under 120 chars. What this place is and its atmosphere.",
      "importance": "Under 60 chars. Why it matters to the story."
    }
  ],
  "terms": [
    {
      "name": "Term, jargon, or concept",
      "definition": "Under 150 chars. Plain-language explanation.",
      "aliases": []
    }
  ],
  "historical_figures": [
    {
      "name": "Real person's name",
      "biography": "Under 100 chars.",
      "role": "Under 40 chars.",
      "importance_in_book": "Under 60 chars."
    }
  ],
  "timeline": [
    {
      "event": "Under 80 chars. What happened.",
      "chapter_pct": 0,
      "characters_involved": ["name1"]
    }
  ]
}"""

TASK_PROMPT = """You are generating an X-Ray reading companion for "{title}" by {author}.

The EPUB file is at: {epub_path}

Read the book using your tools. Then produce a complete X-Ray JSON object.

## Rules

### Character descriptions — CRITICAL
Descriptions must be SPOILER-FREE. Describe who a character IS at first introduction:
- their appearance, personality, background, occupation, social role
- their relationships to other characters as established early on
NEVER include: deaths, fates, what happens to them, how their arc ends, plot outcomes.
The reader has only read to {reading_pct}% of the book.
Any character who first appears after {reading_pct}% should have first_appearance_pct set accordingly.

### Timeline events
Timeline events CAN include significant plot events tagged with chapter_pct.
Only include events that have occurred by {reading_pct}% if spoiler_free mode matters.
But generate ALL events — the plugin filters by reading_pct at display time.

### Approach
Use your read tool to read the EPUB. Work through the book systematically:
1. Read the first 30% — establish main characters, setting, initial conflicts
2. Read the middle 40% — track character development, locations introduced
3. Read the final 30% — complete the timeline, but keep descriptions spoiler-free

Return ONLY the JSON object. No preamble, no explanation.

## Required JSON schema
{schema}"""


def generate(epub_path: str, title: str, author: str,
             reading_pct: float = 100.0) -> tuple[dict, str]:
    """
    Generate X-Ray using pi with tools.
    Returns (xray_dict, strategy_name).
    Raises on failure or empty result.
    """
    if SKIP_PI_XRAY:
        raise RuntimeError("pi_xray skipped (PIREAD_SKIP_PI_XRAY=1)")
    prompt = TASK_PROMPT.format(
        title=title,
        author=author,
        epub_path=epub_path,
        reading_pct=int(reading_pct),
        schema=SCHEMA,
    )

    home = Path.home()
    aws_profile = os.environ.get("PIREAD_AWS_PROFILE", "openclaw-bedrock")
    aws_region  = os.environ.get("PIREAD_AWS_REGION", "us-east-1")

    args = [
        PI_BIN,
        "--mode", "rpc",
        "--no-session",
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--tools", "read,bash",
        "--model", PI_MODEL,
        "--system-prompt",
        "You are an X-Ray generator for a reading assistant. "
        "Read the book file using your tools, then output ONLY a single valid JSON object. "
        "No preamble, no explanation, no markdown fences. "
        "Start your response with '{' and end with '}'. "
        "CRITICAL: Character descriptions must NEVER mention deaths, fates, or endings. "
        "Describe only who a character is at first introduction.",
    ]

    env = {
        **os.environ,
        "AWS_PROFILE": aws_profile,
        "AWS_REGION": aws_region,
        "PI_RUNTIME": "piread-xray",
    }

    logger.info("pi_xray: spawning pi for '%s'", title)
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(home),
    )

    # Wait for ready
    if not _wait_ready(proc, timeout=20):
        proc.kill()
        raise RuntimeError("pi_xray: pi did not become ready in 20s")

    # Send the prompt
    _send(proc, {"type": "prompt", "message": prompt})

    # Collect response until agent_end
    logger.info("pi_xray: waiting for response (timeout=%ds)", XRAY_TIMEOUT)
    raw_text = _collect(proc, timeout=XRAY_TIMEOUT)

    try:
        proc.stdin.close()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    if not raw_text:
        raise RuntimeError("pi_xray: empty response from pi")

    # Extract JSON from response
    xray = _parse_json(raw_text)

    # Sanity-check: if pi returned an empty or trivially small result,
    # treat it as a failure so the caller can fall back to direct generation.
    total_entities = (
        len(xray.get("characters", [])) +
        len(xray.get("locations", [])) +
        len(xray.get("timeline", []))
    )
    if total_entities < 3:
        raise RuntimeError(
            f"pi_xray returned near-empty result ({total_entities} entities) — "
            "likely a partial/timed-out response"
        )

    return xray, "pi_conductor"


# ── helpers ───────────────────────────────────────────────────────────────────

def _send(proc: subprocess.Popen, obj: dict):
    try:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
    except BrokenPipeError:
        pass


def _wait_ready(proc: subprocess.Popen, timeout: int = 20) -> bool:
    req_id = "xray-ready"
    _send(proc, {"type": "get_state", "id": req_id})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = _readline(proc, timeout=2.0)
        if not line:
            continue
        try:
            ev = json.loads(line)
            if ev.get("id") == req_id or ev.get("type") == "get_state":
                return True
        except json.JSONDecodeError:
            pass
    return False


def _collect(proc: subprocess.Popen, timeout: int) -> str:
    deadline = time.monotonic() + timeout
    streaming_parts: list[str] = []

    while time.monotonic() < deadline:
        line = _readline(proc, timeout=1.0)
        if line is None:
            if proc.poll() is not None:
                break
            continue

        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("pi_xray non-JSON: %s", line[:80])
            continue

        ev_type = ev.get("type")

        # Accumulate streaming text deltas
        if ev_type == "message_update":
            delta = ev.get("assistantMessageEvent", {})
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                d = delta.get("delta", {})
                if isinstance(d, dict):
                    streaming_parts.append(d.get("text", ""))

        elif ev_type == "agent_end":
            # Prefer final messages if they contain JSON
            final_parts: list[str] = []
            for msg in ev.get("messages", []):
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            final_parts.append(part.get("text", ""))
                elif isinstance(content, str):
                    final_parts.append(content)
            final = "\n".join(final_parts).strip()
            # Use final messages if they contain JSON, else use streaming accumulation
            if final and "{" in final:
                return final
            accumulated = "".join(streaming_parts)
            if accumulated and "{" in accumulated:
                return accumulated
            return final or accumulated

    # Timeout — try streaming buffer
    accumulated = "".join(streaming_parts)
    if accumulated and "{" in accumulated:
        return accumulated
    raise RuntimeError(f"pi_xray: timed out after {timeout}s")


def _readline(proc: subprocess.Popen, timeout: float = 1.0) -> str | None:
    if proc.stdout is None:
        return None
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if ready:
        try:
            line = proc.stdout.readline()
            return line.strip() if line else None
        except Exception:
            return None
    return None


def _parse_json(text: str) -> dict:
    """Extract and parse JSON from pi's response text."""
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
        text = text.strip()

    # Find JSON boundaries
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object in response. Got: {text[:200]}")
    end = text.rfind("}") + 1
    json_str = text[start:end]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error: {e}. Text: {json_str[:200]}")
