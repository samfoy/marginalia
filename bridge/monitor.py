"""
monitor.py — in-memory request monitor for piread-bridge.

Tracks every HTTP request the bridge handles: what's in flight right now
(with a live-growing elapsed timer), a ring buffer of recently completed
requests, and per-endpoint stats (count / errors / latency).

All state is in-memory and resets when the bridge restarts — same lifecycle
as the X-Ray job registry. No dependencies beyond the stdlib.

Wired into server.py:
  - Handler.send_response is overridden to capture the status code.
  - do_GET / do_POST call begin() before dispatch and end() in a finally.
  - GET /monitor       → render_html()  (the dashboard page)
  - GET /monitor/data  → snapshot()     (JSON the page polls every second)
"""

import json
import threading
import time
from collections import deque
from itertools import count

_lock = threading.Lock()
_ids = count(1)
_inflight: dict[int, dict] = {}
_recent: deque = deque(maxlen=250)
_stats: dict[str, dict] = {}
_started_at = time.time()
_total = 0
_errors = 0

# Paths we never record (the monitor's own polling would drown everything else).
_SKIP_PREFIXES = ("/monitor",)


def should_track(path: str) -> bool:
    return not any(path.startswith(p) for p in _SKIP_PREFIXES)


def _endpoint(method: str, path: str) -> str:
    """Collapse parameterised paths so stats group sensibly."""
    if path.startswith("/xray/status/"):
        return "/xray/status/*"
    return path


def begin(method: str, path: str, detail: str = "") -> dict:
    rec = {
        "id": next(_ids),
        "method": method,
        "path": path,
        "endpoint": _endpoint(method, path),
        "detail": detail,
        "started_at": time.time(),
    }
    with _lock:
        _inflight[rec["id"]] = rec
    return rec


def end(rec: dict, status: int) -> None:
    global _total, _errors
    now = time.time()
    duration_ms = (now - rec["started_at"]) * 1000.0
    is_error = status >= 400
    done = {
        "id": rec["id"],
        "method": rec["method"],
        "endpoint": rec["endpoint"],
        "detail": rec["detail"],
        "status": status,
        "duration_ms": round(duration_ms, 1),
        "ended_at": now,
    }
    with _lock:
        _inflight.pop(rec["id"], None)
        _recent.appendleft(done)
        _total += 1
        if is_error:
            _errors += 1
        s = _stats.get(rec["endpoint"])
        if s is None:
            s = {"count": 0, "errors": 0, "total_ms": 0.0,
                 "min_ms": duration_ms, "max_ms": duration_ms, "last_ms": duration_ms}
            _stats[rec["endpoint"]] = s
        s["count"] += 1
        s["total_ms"] += duration_ms
        s["last_ms"] = round(duration_ms, 1)
        s["min_ms"] = round(min(s["min_ms"], duration_ms), 1)
        s["max_ms"] = round(max(s["max_ms"], duration_ms), 1)
        if is_error:
            s["errors"] += 1


# ── Detail extraction (so in-flight rows say which book / entity / %) ─────────

def _short(s, n: int = 70) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _pct(req: dict) -> str:
    p = req.get("reading_pct")
    try:
        return f" @{float(p):.0f}%" if p else ""
    except (TypeError, ValueError):
        return ""


def detail_for_post(path: str, raw: bytes) -> str:
    """Build a human label for an in-flight POST from its JSON body."""
    try:
        req = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(req, dict):
        return ""

    title = _short(req.get("book_title"), 40)
    head = f"{title} — " if title else ""

    if path == "/wiki":
        return f'{head}wiki "{_short(req.get("entity_name"), 40)}"{_pct(req)}'
    if path == "/chat":
        return f'{head}chat "{_short(req.get("question"), 50)}"{_pct(req)}'
    if path == "/recap":
        return f"{head}recap{_pct(req)}"
    if path == "/section":
        sec = req.get("chapter_title") or f'{req.get("start_pct", 0)}–{req.get("end_pct", 100)}%'
        return f"{head}section {_short(sec, 40)}"
    if path == "/ask":
        return f'{head}ask({req.get("mode", "explain")}) "{_short(req.get("text"), 40)}"'
    if path == "/xray/init":
        return f"{head}xray init{_pct(req)}"
    if path == "/xray/progress":
        return f"progress{_pct(req)}"
    if path == "/note":
        return f"{head}save note"
    if path == "/v1/chat/completions":
        msgs = req.get("messages") or []
        last = msgs[-1].get("content") if msgs else ""
        return f'KO Assistant "{_short(last, 50)}"'
    return head.rstrip(" —")


def detail_for_get(path: str) -> str:
    if path.startswith("/xray/status/"):
        return f"poll job {path.rsplit('/', 1)[-1]}"
    return ""


# ── Snapshot for the JSON endpoint ────────────────────────────────────────────

def snapshot() -> dict:
    now = time.time()
    with _lock:
        inflight = [
            {
                "id": r["id"],
                "method": r["method"],
                "path": r["path"],
                "endpoint": r["endpoint"],
                "detail": r["detail"],
                "started_at": r["started_at"],
                "elapsed_s": round(now - r["started_at"], 1),
            }
            for r in sorted(_inflight.values(), key=lambda x: x["started_at"])
        ]
        recent = list(_recent)[:80]
        stats = {
            ep: {
                "count": s["count"],
                "errors": s["errors"],
                "avg_ms": round(s["total_ms"] / s["count"], 1) if s["count"] else 0.0,
                "min_ms": s["min_ms"],
                "max_ms": s["max_ms"],
                "last_ms": s["last_ms"],
            }
            for ep, s in sorted(_stats.items())
        }
        totals = {"requests": _total, "errors": _errors, "inflight": len(_inflight)}
    return {
        "now": now,
        "uptime_s": round(now - _started_at, 1),
        "inflight": inflight,
        "recent": recent,
        "stats": stats,
        "totals": totals,
    }


# ── Dashboard page ─────────────────────────────────────────────────────────────

_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>piread monitor</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --border:#30363d; --fg:#e6edf3;
    --dim:#8b949e; --accent:#58a6ff; --good:#3fb950; --warn:#d29922;
    --bad:#f85149; --hot:#ff7b72;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--fg);
    font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  header {
    display:flex; align-items:center; gap:20px; flex-wrap:wrap;
    padding:14px 20px; border-bottom:1px solid var(--border); background:var(--panel);
    position:sticky; top:0; z-index:10;
  }
  header h1 { font-size:15px; margin:0; letter-spacing:.5px; }
  header h1 .dot {
    display:inline-block; width:9px; height:9px; border-radius:50%;
    background:var(--good); margin-right:8px; box-shadow:0 0 6px var(--good);
    animation:pulse 2s infinite;
  }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  .meta { display:flex; gap:18px; flex-wrap:wrap; color:var(--dim); margin-left:auto; }
  .meta b { color:var(--fg); font-weight:600; }
  .meta .err b { color:var(--bad); }
  main { padding:20px; display:flex; flex-direction:column; gap:22px; max-width:1200px; }
  section h2 {
    font-size:12px; text-transform:uppercase; letter-spacing:1px; color:var(--dim);
    margin:0 0 8px; font-weight:600;
  }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:8px; overflow:hidden; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:7px 12px; border-bottom:1px solid var(--border); white-space:nowrap; }
  th { color:var(--dim); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  tr:last-child td { border-bottom:none; }
  td.detail { white-space:normal; color:var(--fg); }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .ep { color:var(--accent); }
  .empty { padding:18px 12px; color:var(--dim); font-style:italic; }
  .pill { padding:1px 7px; border-radius:10px; font-size:11px; font-weight:600; }
  .s2 { color:var(--good); } .s4 { color:var(--warn); } .s5 { color:var(--bad); }
  .elapsed { font-variant-numeric:tabular-nums; font-weight:600; }
  .e-ok { color:var(--good); } .e-warn { color:var(--warn); } .e-hot { color:var(--hot); }
  tr.hot { background:rgba(248,81,73,.08); }
  .age { color:var(--dim); }
  .bar { height:4px; background:var(--accent); border-radius:2px; opacity:.5; }
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span>piread monitor</h1>
  <div class="meta">
    <span>uptime <b id="uptime">—</b></span>
    <span>model <b id="model">—</b></span>
    <span>effort <b id="effort">—</b></span>
    <span>books <b id="books">—</b></span>
    <span>requests <b id="total">—</b></span>
    <span class="err">errors <b id="errtot">—</b></span>
  </div>
</header>
<main>
  <section>
    <h2>In flight <span id="inflight-count"></span></h2>
    <div class="card"><table>
      <thead><tr><th>elapsed</th><th>endpoint</th><th>detail</th><th>method</th></tr></thead>
      <tbody id="inflight"></tbody>
    </table></div>
  </section>
  <section>
    <h2>Endpoints</h2>
    <div class="card"><table>
      <thead><tr><th>endpoint</th><th class="num">count</th><th class="num">err</th>
        <th class="num">avg</th><th class="num">min</th><th class="num">max</th><th class="num">last</th></tr></thead>
      <tbody id="stats"></tbody>
    </table></div>
  </section>
  <section>
    <h2>Recent</h2>
    <div class="card"><table>
      <thead><tr><th>when</th><th>status</th><th>dur</th><th>endpoint</th><th>detail</th></tr></thead>
      <tbody id="recent"></tbody>
    </table></div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let data = null, lastFetch = 0;

function fmtDur(ms){
  if (ms == null) return "—";
  if (ms < 1000) return ms.toFixed(0)+"ms";
  return (ms/1000).toFixed(ms<10000?2:1)+"s";
}
function fmtAge(sec){
  if (sec < 1) return "now";
  if (sec < 60) return sec.toFixed(0)+"s ago";
  if (sec < 3600) return (sec/60).toFixed(0)+"m ago";
  return (sec/3600).toFixed(1)+"h ago";
}
function fmtUptime(s){
  if (s < 60) return s.toFixed(0)+"s";
  if (s < 3600) return (s/60).toFixed(0)+"m";
  if (s < 86400) return (s/3600).toFixed(1)+"h";
  return (s/86400).toFixed(1)+"d";
}
function statusClass(c){ return c>=500?"s5":c>=400?"s4":"s2"; }
function elapsedClass(s){ return s>=40?"e-hot":s>=12?"e-warn":"e-ok"; }
function esc(s){ const d=document.createElement("div"); d.textContent=s==null?"":s; return d.innerHTML; }

async function poll(){
  try {
    const r = await fetch("/monitor/data", {cache:"no-store"});
    data = await r.json();
    lastFetch = performance.now();
    render();
  } catch(e){ /* keep last good render; bridge may be restarting */ }
}

function render(){
  if (!data) return;
  $("uptime").textContent = fmtUptime(data.uptime_s);
  $("model").textContent  = data.model || "—";
  $("effort").textContent = data.effort || "—";
  $("books").textContent  = data.books_cached ?? "—";
  $("total").textContent  = data.totals.requests;
  $("errtot").textContent = data.totals.errors;

  // skew = clientNow - serverNow at fetch time, so we can tick elapsed locally
  const skew = (lastFetch/1000) - data.now;
  const liveNow = (performance.now()/1000) - skew;

  const inf = data.inflight || [];
  $("inflight-count").textContent = inf.length ? `(${inf.length})` : "";
  $("inflight").innerHTML = inf.length ? inf.map(r=>{
    const el = liveNow - r.started_at;
    return `<tr class="${el>=40?"hot":""}">
      <td class="elapsed ${elapsedClass(el)}" data-start="${r.started_at}">${el.toFixed(1)}s</td>
      <td class="ep">${esc(r.endpoint)}</td>
      <td class="detail">${esc(r.detail) || "—"}</td>
      <td>${esc(r.method)}</td></tr>`;
  }).join("") : `<tr><td colspan="4" class="empty">nothing in flight — bridge is idle</td></tr>`;

  const stats = data.stats || {};
  const eps = Object.keys(stats);
  $("stats").innerHTML = eps.length ? eps.map(ep=>{
    const s = stats[ep];
    return `<tr><td class="ep">${esc(ep)}</td>
      <td class="num">${s.count}</td>
      <td class="num ${s.errors?"s5":""}">${s.errors||""}</td>
      <td class="num">${fmtDur(s.avg_ms)}</td>
      <td class="num">${fmtDur(s.min_ms)}</td>
      <td class="num">${fmtDur(s.max_ms)}</td>
      <td class="num">${fmtDur(s.last_ms)}</td></tr>`;
  }).join("") : `<tr><td colspan="7" class="empty">no requests yet</td></tr>`;

  const rec = data.recent || [];
  $("recent").innerHTML = rec.length ? rec.map(r=>{
    const age = liveNow - r.ended_at;
    return `<tr><td class="age">${fmtAge(age)}</td>
      <td><span class="pill ${statusClass(r.status)}">${r.status}</span></td>
      <td class="num">${fmtDur(r.duration_ms)}</td>
      <td class="ep">${esc(r.endpoint)}</td>
      <td class="detail">${esc(r.detail) || "—"}</td></tr>`;
  }).join("") : `<tr><td colspan="5" class="empty">no completed requests yet</td></tr>`;
}

// Tick in-flight elapsed locally every 250ms for a smooth live counter,
// and re-poll the server every second.
function tick(){
  if (!data) return;
  const skew = (lastFetch/1000) - data.now;
  const liveNow = (performance.now()/1000) - skew;
  document.querySelectorAll("#inflight .elapsed").forEach(td=>{
    const el = liveNow - parseFloat(td.dataset.start);
    td.textContent = el.toFixed(1)+"s";
    td.className = "elapsed " + elapsedClass(el);
    td.closest("tr").className = el>=40 ? "hot" : "";
  });
}

poll();
setInterval(poll, 1000);
setInterval(tick, 250);
</script>
</body>
</html>
"""


def render_html() -> str:
    return _HTML
