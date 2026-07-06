#!/usr/bin/env python3
"""Run viewer — the founder's local debug view for a Claude Managed Agent.

COPY this file into the build kit as `viewer.py` and edit ONLY the CONFIG
block below (fill it from build-sheet.json). Do not regenerate the rest from
scratch — it encodes hard-won fixes for the events API's sharp edges:
idle-before-kickoff race, resilient polling, is_error as the string "False",
tool_use→result pairing via event ids, pagination, repl-script summarising,
and the three shapes tool_result content arrives in. If a call here 404s,
check the event shapes against cma-api.md / the live docs before editing.

    python3 viewer.py        # → http://127.0.0.1:<port>

Three tabs per run: 🎬 Flow (animated replay of the event log: task → agent
fanning out its hero tools → Outcome grader → deliverable; black box = call
going out, green box = data coming back, click either to expand & freeze),
⚙ Log (the raw timestamped feed), 📄 Report (the deliverable, rendered).

Auth (server-side only; nothing secret ever reaches the page): ANTHROPIC_API_KEY
from ./.env if real, else the ant CLI's OAuth token — the same ladder the
launch scripts use. Binds to 127.0.0.1 only: this proxies the credential.
"""
import html
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ── CONFIG — the only part to customize per agent (from build-sheet.json) ──
CONFIG = {
    "title": "my-agent",                 # agent name, shown in the header
    "emoji": "🤖",                       # header emoji
    "model": "claude-opus-4-8",          # full model slug once picked
    # The deliverable node: label shown, filename regex to find it in the
    # session's output files, and how to render it in the Report tab:
    #   "markdown" — render inline (reports, digests, drafted emails)
    #   "html"     — sandboxed iframe preview (dashboards, HTML reports)
    #   "download" — list output files with download links (xlsx/pptx/pdf/…)
    "deliverable": {"label": "report.md", "match": r"\.md$", "render": "markdown"},
    # Hero tools fan out as labeled boxes in the Agent node (the visual story).
    # Everything else scrolls through the one-line ticker. Pick by archetype:
    #   research/digest agents → ["web_search", "web_fetch"]
    #   coding agents          → ["write", "edit", "read"]
    #   data-analyst agents    → ["bash", "write"]
    "hero_tools": ["web_search", "web_fetch"],
    "port": 8787,
    # Attach the memory store to viewer-launched runs? (matches launch.sh)
    "attach_memory": True,
    "memory_instructions": "Read the memory store before working; update it after.",
}
# ────────────────────────────────────────────────────────────────────────────

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://api.anthropic.com/v1"


def env_file(path):
    """Parse a shell-sourceable .env: tolerate `export KEY=...` and quoted values."""
    vals = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.startswith("export "):
                    k = k[len("export "):]
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


IDS = env_file(os.path.join(HERE, "IDS.env"))
AGENT_ID = IDS.get("AGENT_ID", "")
ENV_ID = IDS.get("ENV_ID", "")
MEMSTORE_ID = IDS.get("MEMSTORE_ID", "")


def auth_header():
    """Same ladder as the launch scripts: shell env → ./.env → ant CLI token."""
    key = os.environ.get("ANTHROPIC_API_KEY", "") \
        or env_file(os.path.join(HERE, ".env")).get("ANTHROPIC_API_KEY", "")
    if key and key != "paste-your-key-here":
        return {"x-api-key": key}
    creds_path = os.path.expanduser("~/.config/anthropic/credentials/default.json")
    try:
        creds = json.load(open(creds_path))
    except FileNotFoundError:
        raise RuntimeError(
            "No auth found: export ANTHROPIC_API_KEY, put it in ./.env, "
            "or run `ant auth login`.") from None
    return {"Authorization": "Bearer " + creds["access_token"]}


def api(path, body=None, raw=False):
    headers = {"anthropic-version": "2023-06-01",
               "anthropic-beta": "managed-agents-2026-04-01",
               "content-type": "application/json", **auth_header()}
    req = urllib.request.Request(BASE + path, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError(
                "API 401 (unauthorized). On the ant-login path the OAuth token "
                "expires — run any ant command (e.g. `ant beta:models list`) to "
                "refresh it, then retry.") from e
        raise
    if raw:
        return data
    # python (strict=False), never jq-style strict parsing: session payloads
    # embed the agent's system prompt with control characters
    return json.JSONDecoder(strict=False).decode(data.decode())


def slim_session(s):
    evals = [{"result": e.get("result"), "explanation": e.get("explanation") or "",
              "iteration": e.get("iteration")} for e in s.get("outcome_evaluations", [])]
    return {"id": s["id"], "title": s.get("title") or s["id"], "status": s.get("status"),
            "created_at": s.get("created_at"), "evals": evals}


def tool_detail(name, inp):
    """One-line label for a tool call (the ticker / tray box text)."""
    if not isinstance(inp, dict):
        return ""
    if name == "web_search":
        return inp.get("query", "")
    if name == "web_fetch":
        u = inp.get("url", "")
        return urlparse(u).netloc if u else ""
    if name == "bash":
        return (inp.get("command") or "")[:100]
    if name in ("write", "read", "edit"):
        return inp.get("path") or inp.get("file_path") or ""
    if name == "repl":
        script = inp.get("script") or ""
        lines = [ln.strip() for ln in script.splitlines()
                 if ln.strip() and not ln.strip().startswith(("//", "/*", "*"))]
        good = [ln for ln in lines
                if ("await" in ln or "(" in ln)
                and not ln.startswith(("try", "}", "let out", "const out", "out.push"))]
        return " · ".join((good or lines)[:2])[:110] or "script"
    for v in inp.values():
        if isinstance(v, str):
            return v[:100]
    return ""


def tool_full(name, inp):
    """Expanded view of a tool call (click-to-expand on the black box)."""
    if not isinstance(inp, dict):
        return ""
    if name == "repl":
        return (inp.get("script") or "")[:2500]
    if name == "bash":
        return (inp.get("command") or "")[:2500]
    if name == "web_search":
        return inp.get("query", "")
    if name == "web_fetch":
        return inp.get("url", "")
    if name in ("write", "edit"):
        body = inp.get("content") or inp.get("new_str") or ""
        return ((inp.get("path") or inp.get("file_path") or "") + "\n\n" + str(body))[:2500]
    return json.dumps(inp)[:2000]


def result_full(content):
    """Expanded view of a tool result: all titles+urls, or the raw text."""
    if isinstance(content, str):
        return content[:2500]
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("title"):
            parts.append("• " + b.get("title", "") + (" — " + b["url"] if b.get("url") else ""))
        elif isinstance(b.get("text"), str):
            parts.append(b["text"])
        elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
            parts += ["• " + r.get("title", "") + (" — " + r["url"] if r.get("url") else "")
                      for r in b["content"] if isinstance(r, dict)]
    return "\n".join(parts)[:2500]


def result_detail(content):
    """One-line data snippet for a tool result (the green line)."""
    if isinstance(content, str):
        return " ".join(content.split())[:160]
    if not isinstance(content, list):
        return ""
    titles, text = [], ""
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("title"):
            titles.append(b["title"])
        elif not text and isinstance(b.get("text"), str):
            text = " ".join(b["text"].split())[:160]
        elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
            titles += [r.get("title", "") for r in b["content"] if isinstance(r, dict)]
    if len(titles) > 1:
        return f"{len(titles)} results — " + " · ".join(t for t in titles[:2] if t)[:130]
    if titles:
        return titles[0][:150]
    return text


def rubric_criteria(rubric):
    """Pull the checkable criteria out of the rubric markdown (numbered or
    bulleted lines), so the grader node can show what it will score."""
    content = rubric.get("content") if isinstance(rubric, dict) else ""
    crits = []
    for ln in (content or "").splitlines():
        m = re.match(r"\s*(?:\d+\.|[-*])\s+(.*)", ln)
        if m:
            txt = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1)).strip()
            if txt:
                crits.append(txt[:110])
    return crits[:8]


def slim_events(events, tool_names, items):
    """Slim raw events into flow/log items; tool_names maps use-id → name."""
    for e in events:
        t, ts = e.get("type"), (e.get("processed_at") or "")[11:19]
        if t == "user.define_outcome":
            items.append({"t": "outcome_set", "ts": ts,
                          "description": (e.get("description") or "")[:180],
                          "criteria": rubric_criteria(e.get("rubric")),
                          "max_iterations": e.get("max_iterations")})
        elif t == "agent.message":
            text = " ".join(b.get("text", "") for b in (e.get("content") or [])
                            if isinstance(b, dict))
            if text.strip():
                items.append({"t": "msg", "ts": ts, "text": text[:400]})
        elif t == "agent.tool_use":
            tool_names[e.get("id")] = e.get("name", "?")
            items.append({"t": "tool", "ts": ts, "name": e.get("name", "?"),
                          "detail": tool_detail(e.get("name"), e.get("input")),
                          "full": tool_full(e.get("name"), e.get("input"))})
        elif t == "agent.tool_result":
            name = tool_names.get(e.get("tool_use_id"), "")
            snippet = result_detail(e.get("content"))
            err = bool(e.get("is_error")) and e.get("is_error") not in ("False", "false")
            if snippet or err:
                items.append({"t": "result", "ts": ts, "name": name,
                              "is_error": err, "detail": snippet,
                              "full": result_full(e.get("content"))})
        elif t == "span.outcome_evaluation_start":
            items.append({"t": "grade_start", "ts": ts, "iteration": e.get("iteration")})
        elif t == "span.outcome_evaluation_end":
            items.append({"t": "grade", "ts": ts, "iteration": e.get("iteration"),
                          "result": e.get("result"),
                          "explanation": (e.get("explanation") or "")})
        elif t == "session.status_idle":
            items.append({"t": "idle", "ts": ts})
    return items


# The event log is append-only, so completed pages never change: cache them per
# session and only refetch from the last known cursor. Keeps the 4s flow poll
# O(new events) instead of re-paging the whole log every time.
_trace_cache = {}  # sid → {"items": [...], "page": cursor, "tools": {id: name}}


def build_trace(sid):
    """Slim the session's event log for the flow/log views (incremental)."""
    c = _trace_cache.setdefault(sid, {"items": [], "page": None, "tools": {}})
    items, page = list(c["items"]), c["page"]
    for _ in range(30):
        d = api(f"/sessions/{sid}/events?limit=100" + (f"&page={page}" if page else ""))
        next_page = d.get("next_page")
        if next_page:
            # complete page — safe to cache and never refetch
            slim_events(d.get("data", []), c["tools"], items)
            c["items"], c["page"] = list(items), next_page
            page = next_page
        else:
            # final (possibly still-growing) page — return fresh, don't cache
            slim_events(d.get("data", []), dict(c["tools"]), items)
            break
    return items


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def guard(self, csrf=False):
        """Block DNS-rebinding (foreign Host) and cross-origin CSRF.

        The server proxies a real credential, so: Host must be local, any
        Origin header must be the local origin, and state-changing routes
        additionally require a custom header the page's fetch() sends —
        cross-origin pages can't add one without a CORS preflight, which
        this server never answers.
        """
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send(403, {"error": "forbidden host"})
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            self.send(403, {"error": "forbidden origin"})
            return False
        if csrf and self.headers.get("X-Viewer-Csrf") != "1":
            self.send(403, {"error": "missing csrf header"})
            return False
        return True

    def do_GET(self):
        if not self.guard():
            return
        try:
            if self.path == "/":
                self.send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/sessions":
                d = api(f"/sessions?agent_id={AGENT_ID}&limit=30")
                self.send(200, {"sessions": [slim_session(s) for s in d.get("data", [])]})
            elif self.path.startswith("/api/session/"):
                d = api("/sessions/" + self.path.split("/")[-1])
                self.send(200, slim_session(d))
            elif self.path.startswith("/api/trace/"):
                self.send(200, {"items": build_trace(self.path.split("/")[-1])})
            elif self.path.startswith("/api/outputs/"):
                sid = self.path.split("/")[-1]
                d = api(f"/files?scope_id={sid}")
                self.send(200, {"files": [{"id": f["id"], "filename": f.get("filename", "")}
                                          for f in d.get("data", [])]})
            elif self.path.startswith("/api/file/"):
                data = api("/files/" + self.path.split("/")[-1] + "/content", raw=True)
                self.send(200, data, "text/plain; charset=utf-8")
            else:
                self.send(404, {"error": "not found"})
        except Exception as e:  # surface API errors to the page, don't crash
            self.send(502, {"error": str(e)})

    def do_POST(self):
        if not self.guard(csrf=True):
            return
        try:
            if self.path == "/api/run":
                resources = []
                if CONFIG["attach_memory"] and MEMSTORE_ID:
                    resources = [{"type": "memory_store", "memory_store_id": MEMSTORE_ID,
                                  "access": "read_write",
                                  "instructions": CONFIG["memory_instructions"]}]
                sess = api("/sessions", {
                    "agent": AGENT_ID, "environment_id": ENV_ID,
                    "title": CONFIG["title"] + " — run from viewer",
                    "resources": resources})
                task = open(os.path.join(HERE, "first_prompt.txt")).read()
                rubric = open(os.path.join(HERE, "outcome.md")).read()
                api(f"/sessions/{sess['id']}/events", {
                    "events": [{"type": "user.define_outcome", "description": task,
                                "rubric": {"type": "text", "content": rubric},
                                "max_iterations": 3}]})
                self.send(200, {"id": sess["id"]})
            else:
                self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(502, {"error": str(e)})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — runs</title>
<style>
:root{--bg:#faf9f5;--panel:#fff;--inset:#f0eee6;--ink:#141413;--body:#3d3d3a;--sub:#73726c;
--mut:#9c9a92;--hair:rgba(20,20,19,.12);--soft:rgba(20,20,19,.07);--live:#1d7a52;--amber:#9a7d24;
--clay:#ad5724;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--body);font-size:14px;line-height:1.55}
header{display:flex;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--hair);
position:sticky;top:0;background:rgba(250,249,245,.94);backdrop-filter:blur(12px);z-index:5}
header h1{font-size:16px;margin:0;color:var(--ink)}header .sub{color:var(--mut);font-family:var(--mono);font-size:11.5px}
button{background:var(--ink);color:#fff;border:0;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
button.ghost{background:var(--panel);color:var(--sub);border:1px solid var(--hair)}
.layout{display:grid;grid-template-columns:320px 1fr;min-height:calc(100vh - 54px)}
nav{border-right:1px solid var(--hair);overflow-y:auto}
.item{padding:12px 16px;border-bottom:1px solid var(--soft);cursor:pointer}
.item:hover{background:var(--inset)}.item.sel{background:var(--inset);border-left:3px solid var(--live);padding-left:13px}
.item .t{font-weight:600;color:var(--ink);font-size:13px}
.item .m{font-family:var(--mono);font-size:11px;color:var(--mut);margin-top:2px}
.chip{display:inline-block;font-family:var(--mono);font-size:10.5px;padding:1px 8px;border-radius:999px;
border:1px solid var(--hair);background:var(--inset);color:var(--sub);margin-top:4px}
.chip.satisfied{color:var(--live);border-color:rgba(34,134,92,.4)}
.chip.running,.chip.pending{color:var(--amber);border-color:rgba(154,125,36,.45)}
.chip.needs_revision,.chip.failed,.chip.max_iterations_reached{color:var(--clay);border-color:rgba(173,87,36,.5)}
main{padding:18px 26px 40px;overflow-y:auto}
.tabs{display:flex;gap:8px;margin-bottom:14px;align-items:center}
.tab{font-size:13px;padding:6px 14px;border-radius:8px;border:1px solid var(--hair);background:var(--panel);
color:var(--sub);cursor:pointer}
.tab.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.verdict{background:var(--panel);border:1px solid var(--hair);border-radius:10px;padding:12px 16px;margin-bottom:14px;font-size:13px;max-width:900px}
.verdict b{color:var(--ink)}
.report{background:var(--panel);border:1px solid var(--hair);border-radius:12px;padding:26px 32px;max-width:900px}
.report h1,.report h2,.report h3{color:var(--ink);margin:1.1em 0 .4em;line-height:1.25}.report h1{font-size:20px}.report h2{font-size:16px}
.report a{color:var(--live)}.report table{border-collapse:collapse;width:100%;font-size:13px;margin:.6em 0}
.report th{text-align:left;font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;
color:var(--mut);border-bottom:1px solid var(--hair);padding:5px 8px}
.report td{padding:6px 8px;border-bottom:1px solid var(--soft);vertical-align:top}
.report code{font-family:var(--mono);font-size:12.5px;background:var(--inset);padding:1px 5px;border-radius:4px}
.report .subject{font-family:var(--mono);font-size:12.5px;color:var(--sub);border-bottom:1px dashed var(--hair);
padding-bottom:10px;margin-bottom:14px}
.htmlframe{width:100%;max-width:980px;height:640px;border:1px solid var(--hair);border-radius:12px;background:#fff}
.filelist a{display:block;padding:9px 0;color:var(--live);font-family:var(--mono);font-size:13px}
.empty{color:var(--mut);padding:60px 0;text-align:center}
/* ── log feed ── */
.feed{background:var(--panel);border:1px solid var(--hair);border-radius:12px;padding:8px 0;max-height:520px;overflow-y:auto;max-width:900px}
.ev{display:flex;gap:10px;padding:5px 16px;font-size:13px;align-items:baseline}
.ev:hover{background:var(--inset)}
.ev .ts{font-family:var(--mono);font-size:10.5px;color:var(--mut);flex:none;width:58px}
.ev .ico{flex:none;width:20px;text-align:center}
.ev .det{font-family:var(--mono);font-size:12px;color:var(--sub);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ev .nm{color:var(--ink);font-weight:600;font-size:12.5px;flex:none}
.ev.msg .txt{color:var(--body);font-size:12.5px;font-style:italic}
.gradecard{margin:8px 12px;border-radius:10px;border:1px solid rgba(154,125,36,.45);background:rgba(154,125,36,.06);padding:10px 14px;font-size:12.5px}
.gradecard.satisfied{border-color:rgba(34,134,92,.45);background:rgba(34,134,92,.06)}
.gradecard.needs_revision{border-color:rgba(173,87,36,.5);background:rgba(173,87,36,.06)}
.gradecard b{color:var(--ink)}
.gradecard .ex{color:var(--sub);margin-top:4px;white-space:pre-wrap;max-height:140px;overflow-y:auto}
/* ── flow board ── */
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
@keyframes popIn{0%{transform:scale(.6);opacity:0}70%{transform:scale(1.06)}100%{transform:scale(1);opacity:1}}
@keyframes flowdash{to{background-position:-24px 0}}
.board{display:grid;grid-template-columns:220px 34px minmax(340px,1fr) 34px 240px 34px 170px;
gap:0;align-items:start;margin-top:6px}
.fnode{background:var(--panel);border:1.5px solid var(--hair);border-radius:14px;overflow:hidden;opacity:.45;
transition:opacity .4s,border-color .4s}
.fnode.on{opacity:1}
.fnode.active{border-color:rgba(154,125,36,.55)}
.fnode.active .fled{background:var(--amber);animation:pulse 1.4s ease-in-out infinite}
.fnode.done{border-color:rgba(34,134,92,.45)}
.fnode.done .fled{background:var(--live)}
.fhead{display:flex;align-items:center;gap:8px;padding:10px 13px;border-bottom:1px solid var(--soft)}
.fhead h3{margin:0;font-size:13.5px;color:var(--ink)}
.fled{width:8px;height:8px;border-radius:50%;background:var(--mut);flex:none;margin-left:auto}
.fbody{padding:10px 13px;font-size:12.5px;color:var(--sub)}
.fbody .mono{font-family:var(--mono);font-size:11.5px}
.conn{height:2px;margin-top:46px;background:var(--hair);position:relative}
.conn.flow{background:repeating-linear-gradient(90deg,rgba(34,134,92,.65) 0 10px,transparent 10px 24px);
animation:flowdash .7s linear infinite}
.conn.back{background:repeating-linear-gradient(90deg,rgba(173,87,36,.6) 0 10px,transparent 10px 24px);
animation:flowdash .7s linear infinite reverse}
.tray-label{font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);margin:8px 2px 6px}
.sbox{display:flex;gap:8px;align-items:baseline;background:var(--inset);border:1px solid var(--hair);
border-radius:9px;padding:6px 10px;margin:5px 0;font-size:12px;animation:popIn .35s ease-out;overflow:hidden}
.sbox .q{font-family:var(--mono);font-size:11.5px;color:var(--body);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sbox.donebox{opacity:.55}
.morebox{font-family:var(--mono);font-size:11px;color:var(--mut);padding:2px 4px}
.nowline{margin-top:10px;background:var(--ink);color:#e8e6df;border-radius:9px;padding:7px 11px;
font-family:var(--mono);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nowline .cur::after{content:"▋";animation:pulse 1s infinite;margin-left:2px}
.dataline{margin-top:5px;background:rgba(34,134,92,.07);border:1px solid rgba(34,134,92,.25);
color:var(--live);border-radius:9px;padding:5px 11px;font-family:var(--mono);font-size:11px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dataline.err{background:rgba(173,87,36,.07);border-color:rgba(173,87,36,.35);color:var(--clay)}
.nowline,.dataline{cursor:pointer}
.nowline.expanded,.dataline.expanded{white-space:pre-wrap;overflow-y:auto;max-height:280px;
text-overflow:clip;word-break:break-word}
.nowline.expanded{outline:1px solid rgba(154,125,36,.5)}
.dataline.expanded{outline:1px solid rgba(34,134,92,.5)}
.saidline{margin-top:8px;font-size:12px;font-style:italic;color:var(--sub);max-height:56px;overflow:hidden}
.iterbadge{display:inline-block;font-family:var(--mono);font-size:10.5px;border:1px solid var(--hair);
border-radius:999px;padding:1px 8px;color:var(--sub);margin-left:6px}
.verdictpill{display:inline-block;font-family:var(--mono);font-size:11px;padding:2px 10px;border-radius:999px;margin-top:6px}
.verdictpill.satisfied{background:rgba(34,134,92,.1);border:1px solid rgba(34,134,92,.45);color:var(--live)}
.verdictpill.needs_revision{background:rgba(173,87,36,.08);border:1px solid rgba(173,87,36,.5);color:var(--clay)}
.verdictpill.other{background:var(--inset);border:1px solid var(--hair);color:var(--sub)}
.feedback{margin-top:8px;font-size:11.5px;color:var(--clay);background:rgba(173,87,36,.06);
border:1px solid rgba(173,87,36,.35);border-radius:8px;padding:6px 9px;max-height:90px;overflow-y:auto}
.gradeex{margin-top:8px;font-size:11.5px;color:var(--sub);max-height:150px;overflow-y:auto;white-space:pre-wrap}
.rublist{margin:8px 0 0;padding:0;list-style:none}
.rublist li{font-size:11.5px;color:var(--sub);margin:3px 0;display:flex;gap:6px;align-items:baseline}
.rublist li::before{content:"□";color:var(--mut);flex:none}
.rublist.pass li::before{content:"✓";color:var(--live)}
.openreport{margin-top:10px;width:100%}
@media(max-width:1150px){.board{grid-template-columns:1fr;gap:10px}.conn{display:none}}
</style></head><body>
<header><h1>__EMOJI__ __TITLE__</h1><span class="sub" id="agentline"></span>
<span style="flex:1"></span><button id="runbtn" onclick="runNow()">▶ Run now</button></header>
<div class="layout"><nav id="list"></nav><main id="main"><div class="empty">Pick a run on the left.</div></main></div>
<script>
const CFG=__CONFIG_JSON__;
const RENDER=CFG.render,DELIV=new RegExp(CFG.deliv_match,'i'),
      DELIV_LABEL=CFG.deliv_label,HERO=new Set(CFG.hero),MODEL=CFG.model;
let sel=null,tab='flow',flow=null,pollTimer=null,tickTimer=null;
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
function md(src){const lines=src.split('\n');let out=[],i=0;
  const inline=t=>esc(t)
    .replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,'<a href="$2" target="_blank">$1</a>')
    .replace(/`([^`]+)`/g,'<code>$1</code>');
  while(i<lines.length){const L=lines[i];
    if(/^\s*\|/.test(L)){const rows=[];while(i<lines.length&&/^\s*\|/.test(lines[i])){rows.push(lines[i]);i++;}
      const cells=r=>r.replace(/^\s*\||\|\s*$/g,'').split('|').map(c=>inline(c.trim()));
      let h='<table><thead><tr>'+cells(rows[0]).map(c=>'<th>'+c+'</th>').join('')+'</tr></thead><tbody>';
      for(let r=1;r<rows.length;r++){if(/^[\s|:-]+$/.test(rows[r]))continue;
        h+='<tr>'+cells(rows[r]).map(c=>'<td>'+c+'</td>').join('')+'</tr>';}
      out.push(h+'</tbody></table>');continue;}
    if(/^###\s/.test(L))out.push('<h3>'+inline(L.slice(4))+'</h3>');
    else if(/^##\s/.test(L))out.push('<h2>'+inline(L.slice(3))+'</h2>');
    else if(/^#\s/.test(L))out.push('<h1>'+inline(L.slice(2))+'</h1>');
    else if(/^\s*[-*]\s/.test(L)){let items=[];while(i<lines.length&&/^\s*[-*]\s/.test(lines[i])){items.push('<li>'+inline(lines[i].replace(/^\s*[-*]\s/,''))+'</li>');i++;}
      out.push('<ul>'+items.join('')+'</ul>');continue;}
    else if(/^---+\s*$/.test(L))out.push('<hr>');
    else if(/^Subject:/i.test(L))out.push('<div class="subject">'+inline(L)+'</div>');
    else if(L.trim())out.push('<p>'+inline(L)+'</p>');
    i++;}
  return out.join('\n');}
function chip(s){const v=esc((s.evals&&s.evals.length&&s.evals[s.evals.length-1].result)||s.status||'');
  return '<span class="chip '+v.replace(/[^a-z_]/g,'')+'">'+v+'</span>';}
async function load(){const d=await (await fetch('/api/sessions')).json();
  const el=document.getElementById('list');el.innerHTML='';
  (d.sessions||[]).forEach(s=>{const div=document.createElement('div');
    div.className='item'+(sel===s.id?' sel':'');div.onclick=()=>selectRun(s);
    div.innerHTML='<div class="t">'+esc(s.title)+'</div><div class="m">'+esc(s.id)+' · '+
      (s.created_at||'').replace('T',' ').slice(0,16)+'</div>'+chip(s);el.appendChild(div);});
  if(d.error)el.innerHTML='<div class="item">'+esc(d.error)+'</div>';}
function selectRun(s){sel=s.id;tab='flow';render();load();}
function render(){if(!sel)return;clearTimeout(tickTimer);clearTimeout(pollTimer);
  const m=document.getElementById('main');
  m.innerHTML='<div class="tabs">'+
    '<div class="tab'+(tab==='flow'?' on':'')+'" onclick="tab=\'flow\';render()">🎬 Flow</div>'+
    '<div class="tab'+(tab==='log'?' on':'')+'" onclick="tab=\'log\';render()">⚙ Log</div>'+
    '<div class="tab'+(tab==='report'?' on':'')+'" onclick="tab=\'report\';render()">📄 Report</div>'+
    (tab==='flow'?'<span style="flex:1"></span><button class="ghost" onclick="skipFlow()">⏩ Skip to end</button>':'')+
    '</div><div id="pane"></div>';
  if(tab==='flow')startFlow();else if(tab==='log')renderLog();else renderReport();}

/* ── flow board ─────────────────────────────────────── */
function boardHTML(){return `
<div class="board">
  <div class="fnode" id="n-task">
    <div class="fhead">📋 <h3>Task</h3><span class="fled"></span></div>
    <div class="fbody"><div id="taskdesc" class="mono">…</div>
      <div class="tray-label">sent as user.define_outcome</div>
      <div id="taskmeta">rubric attached · max_iterations —</div></div>
  </div>
  <div class="conn" id="c1"></div>
  <div class="fnode" id="n-agent">
    <div class="fhead">🤖 <h3>Agent</h3><span id="iterb" class="iterbadge">iteration 0</span><span class="fled"></span></div>
    <div class="fbody">
      <div class="mono">${MODEL} · inside its 📦 sandbox</div>
      <div class="saidline" id="said"></div>
      <div class="nowline" id="now" style="display:none" onclick="toggleNow()" title="click to expand &amp; freeze — click again to resume"></div>
      <div class="dataline" id="data" style="display:none" onclick="toggleData()" title="click to expand &amp; freeze — click again to resume"></div>
      <div class="tray-label" id="traylabel" style="display:none"></div>
      <div class="searchtray" id="tray"></div>
      <div class="feedback" id="fb" style="display:none"></div>
    </div>
  </div>
  <div class="conn" id="c2"></div>
  <div class="fnode" id="n-grader">
    <div class="fhead">🎯 <h3>Outcome grader</h3><span class="fled"></span></div>
    <div class="fbody"><span class="mono">separate context window — checks the deliverable against your rubric:</span>
      <ol class="rublist" id="rublist"></ol>
      <div id="gstate" style="margin-top:8px">waiting for the agent to finish a draft…</div>
      <div id="gverdict"></div><div class="gradeex" id="gex"></div></div>
  </div>
  <div class="conn" id="c3"></div>
  <div class="fnode" id="n-report">
    <div class="fhead">📤 <h3>${esc(DELIV_LABEL)}</h3><span class="fled"></span></div>
    <div class="fbody"><div id="repstate">not written yet</div>
      <button class="openreport" id="repbtn" style="display:none" onclick="tab='report';render()">Open it</button></div>
  </div>
</div>`;}
const DELAY={outcome_set:900,msg:850,tool:340,result:280,grade_start:1000,grade:1500,idle:600};
async function startFlow(){
  document.getElementById('pane').innerHTML=boardHTML();
  flow={items:[],cursor:0,counts:{},trayTotal:0,status:null,
        holdNow:false,holdData:false,lastNow:null,lastData:null};
  await extendFlow();tickFlow();}
function drawNow(){const n=el('now');if(!flow||!flow.lastNow)return;n.style.display='';
  if(flow.holdNow){n.classList.add('expanded');n.textContent=flow.lastNow.full||'';}
  else{n.classList.remove('expanded');n.innerHTML='<span class="cur">'+flow.lastNow.short+'</span>';}}
function drawData(){const d=el('data');if(!flow||!flow.lastData)return;d.style.display='';
  d.className='dataline'+(flow.lastData.err?' err':'')+(flow.holdData?' expanded':'');
  d.textContent=flow.holdData?(flow.lastData.full||flow.lastData.text):flow.lastData.text;}
function toggleNow(){if(!flow)return;flow.holdNow=!flow.holdNow;drawNow();}
function toggleData(){if(!flow)return;flow.holdData=!flow.holdData;drawData();}
function flowFinished(){ // done only when the event log itself says so —
  // status can read 'idle' before the kickoff is processed, so status alone lies
  return flow.status==='terminated'||
         (flow.status==='idle'&&flow.items.some(e=>e.t==='idle'));}
async function extendFlow(){
  try{
    const [tr,s]=await Promise.all([
      fetch('/api/trace/'+sel).then(r=>r.json()),
      fetch('/api/session/'+sel).then(r=>r.json())]);
    if(!flow)return;
    if(Array.isArray(tr.items)&&tr.items.length>=flow.items.length)flow.items=tr.items;
    if(s&&s.status)flow.status=s.status;
  }catch(err){/* transient (server restart, blip) — keep polling */}
  if(flow&&!flowFinished()&&tab==='flow')pollTimer=setTimeout(extendFlow,4000);}
function el(id){return document.getElementById(id);}
function setNode(id,cls){const n=el(id);if(!n)return;n.classList.add('on');
  n.classList.remove('active','done');if(cls)n.classList.add(cls);}
function tickFlow(){if(!flow||tab!=='flow')return;
  if(flow.cursor>=flow.items.length){
    if(!flowFinished()){tickTimer=setTimeout(tickFlow,1000);return;}
    return;}
  const e=flow.items[flow.cursor++];
  try{applyFlow(e);}catch(err){/* one bad item must not kill the animation */}
  tickTimer=setTimeout(tickFlow,DELAY[e.t]||400);}
function skipFlow(){if(!flow)return;clearTimeout(tickTimer);
  while(flow.cursor<flow.items.length){
    try{applyFlow(flow.items[flow.cursor]);}catch(err){}
    flow.cursor++;}
  if(!flowFinished())tickTimer=setTimeout(tickFlow,1000);}
const ICONS={web_search:'🔍',web_fetch:'🌐',bash:'💻',write:'📝',edit:'✏️',read:'📖',glob:'🗂️',grep:'🔎',repl:'⚙️'};
function applyFlow(e){
  if(e.t==='outcome_set'){setNode('n-task','done');
    el('taskdesc').textContent=e.description||'task';
    el('taskmeta').textContent='rubric attached · max_iterations '+(e.max_iterations||3);
    if(e.criteria&&e.criteria.length){el('n-grader').classList.add('on');
      el('rublist').innerHTML=e.criteria.map(c=>'<li>'+esc(c)+'</li>').join('');
      el('gstate').textContent='will score these '+e.criteria.length+' criteria once the agent finishes a draft…';}
    el('c1').classList.add('flow');setNode('n-agent','active');}
  else if(e.t==='msg'){setNode('n-agent','active');
    el('said').textContent='💬 '+e.text;}
  else if(e.t==='tool'){setNode('n-agent','active');
    if(HERO.has(e.name)){flow.counts[e.name]=(flow.counts[e.name]||0)+1;flow.trayTotal++;
      el('traylabel').style.display='';
      el('traylabel').textContent=Object.entries(flow.counts)
        .map(([k,v])=>(ICONS[k]||'🛠️')+' '+k+' ×'+v).join(' · ');
      addBox(ICONS[e.name]||'🛠️',e.detail);}
    else{const pre={bash:'💻 $ ',write:'📝 write ',edit:'✏️ edit ',read:'📖 read ',repl:'⚙️ js: '}[e.name]||'🛠️ '+esc(e.name)+' ';
      flow.lastNow={short:pre+esc(e.detail||''),full:e.full||e.detail||''};
      if(!flow.holdNow)drawNow();}}
  else if(e.t==='result'){
    flow.lastData={text:(e.is_error?'⚠ ':'↩ ')+(e.name?e.name+': ':'')+e.detail,
                   full:(e.name?e.name+'\n':'')+(e.full||e.detail||''),err:e.is_error};
    if(!flow.holdData)drawData();}
  else if(e.t==='grade_start'){el('c2').classList.add('flow');el('c1').classList.remove('flow');
    setNode('n-agent','done');setNode('n-grader','active');
    el('iterb').textContent='iteration '+e.iteration;
    el('now').style.display='none';el('data').style.display='none';
    flow.holdNow=flow.holdData=false;
    el('gstate').textContent='evaluating iteration '+e.iteration+'… (grader reasoning is opaque — you see that it works, not what it thinks)';}
  else if(e.t==='grade'){el('c2').classList.remove('flow');
    const cls=e.result==='satisfied'?'satisfied':(e.result==='needs_revision'?'needs_revision':'other');
    el('gverdict').innerHTML='<span class="verdictpill '+cls+'">'+esc(e.result)+'</span>';
    el('gex').textContent=e.explanation||'';
    if(e.result==='needs_revision'){setNode('n-grader','done');setNode('n-agent','active');
      el('c2').classList.add('back');
      el('fb').style.display='';el('fb').textContent='↩ grader feedback → new iteration: '+(e.explanation||'').slice(0,220);
      el('iterb').textContent='iteration '+(parseInt(e.iteration||0)+1);
      el('gstate').textContent='sent feedback back to the agent';}
    else{setNode('n-grader','done');
      el('gstate').textContent='evaluation finished';
      if(e.result==='satisfied'){el('rublist').classList.add('pass');
        el('c3').classList.add('flow');setNode('n-report','done');
        el('repstate').textContent=DELIV_LABEL+' written to /mnt/session/outputs/ — fetched via the Files API';
        el('repbtn').style.display='';}}}
  else if(e.t==='idle'){setNode('n-agent','done');
    el('repbtn').style.display='';  // outputs may exist even without a satisfied verdict
    el('now').style.display='none';el('data').style.display='none';
    flow.holdNow=flow.holdData=false;}}
function addBox(ico,text){const tray=el('tray');
  const d=document.createElement('div');d.className='sbox';
  d.innerHTML='<span>'+ico+'</span><span class="q">'+esc(text||'')+'</span>';
  tray.appendChild(d);
  const boxes=tray.querySelectorAll('.sbox');
  if(boxes.length>6)boxes[boxes.length-7].remove();
  let more=el('morebox');const hidden=flow.trayTotal-Math.min(6,boxes.length);
  if(hidden>0){if(!more){more=document.createElement('div');more.id='morebox';more.className='morebox';
    tray.parentNode.insertBefore(more,tray);}
    more.textContent='… +'+hidden+' earlier';}
  boxes.forEach((b,i)=>{if(i<boxes.length-3)b.classList.add('donebox');});}

/* ── log + report ───────────────────────────────────── */
async function renderLog(){const pane=el('pane');pane.innerHTML='<div class="empty">Loading…</div>';
  const tr=await (await fetch('/api/trace/'+sel)).json();
  let feed='<div class="feed">';
  (tr.items||[]).forEach(e=>{
    if(e.t==='outcome_set')feed+='<div class="ev"><span class="ts">'+esc(e.ts)+'</span><span class="ico">📋</span><span class="nm">Task set</span><span class="det">user.define_outcome · max_iterations '+esc(e.max_iterations)+'</span></div>';
    else if(e.t==='msg')feed+='<div class="ev msg"><span class="ts">'+esc(e.ts)+'</span><span class="ico">💬</span><span class="txt">'+esc(e.text)+'</span></div>';
    else if(e.t==='tool')feed+='<div class="ev"><span class="ts">'+esc(e.ts)+'</span><span class="ico">'+(ICONS[e.name]||'🛠️')+'</span><span class="nm">'+esc(e.name)+'</span><span class="det">'+esc(e.detail||'')+'</span></div>';
    else if(e.t==='result')feed+='<div class="ev" style="opacity:.65"><span class="ts">'+esc(e.ts)+'</span><span class="ico">'+(e.is_error?'⚠':'↩')+'</span><span class="det">'+esc(e.detail||'')+'</span></div>';
    else if(e.t==='grade_start')feed+='<div class="ev"><span class="ts">'+esc(e.ts)+'</span><span class="ico">🎯</span><span class="nm">Grader starts</span><span class="det">iteration '+esc(e.iteration)+'</span></div>';
    else if(e.t==='grade')feed+='<div class="gradecard '+esc(e.result).replace(/[^a-z_]/g,'')+'"><b>🎯 '+esc(e.result)+'</b> (iteration '+esc(e.iteration)+')<div class="ex">'+esc(e.explanation)+'</div></div>';
    else if(e.t==='idle')feed+='<div class="ev"><span class="ts">'+esc(e.ts)+'</span><span class="ico">🏁</span><span class="nm">Session idle</span><span class="det">run complete</span></div>';});
  pane.innerHTML=feed+'</div>';}
async function renderReport(){const pane=el('pane');pane.innerHTML='<div class="empty">Loading…</div>';
  const s=await (await fetch('/api/session/'+sel)).json();
  let v='<div class="verdict"><b>Status:</b> '+esc(s.status||'?');
  (s.evals||[]).forEach(e=>{v+=' · <b>grader:</b> '+esc(e.result||'pending');});v+='</div>';
  const fl=await (await fetch('/api/outputs/'+sel)).json();
  const files=fl.files||[];
  const main=files.find(f=>DELIV.test(f.filename));
  const list=fs=>'<div class="report filelist">'+fs.map(f=>
    '<a href="/api/file/'+esc(f.id)+'" download="'+esc(f.filename.split('/').pop())+'">⬇ '+esc(f.filename)+'</a>').join('')+'</div>';
  let body;
  if(!files.length)body='<div class="empty">No output files yet'+(s.status==='running'?' — still running: watch 🎬 Flow.':'.')+'</div>';
  else if(!main)body='<div class="verdict">No file matched the configured deliverable pattern ('+esc(DELIV_LABEL)+') — all outputs:</div>'+list(files);
  else if(RENDER==='markdown'){const txt=await (await fetch('/api/file/'+main.id)).text();
    body='<div class="report">'+md(txt)+'</div>';}
  else if(RENDER==='html'){const txt=await (await fetch('/api/file/'+main.id)).text();
    body='<iframe class="htmlframe" sandbox="" srcdoc="'+esc(txt)+'"></iframe>';}
  else{body=list(files);}
  pane.innerHTML=v+body;}
async function runNow(){const b=el('runbtn');b.disabled=true;b.textContent='Starting…';
  try{const d=await (await fetch('/api/run',{method:'POST',headers:{'X-Viewer-Csrf':'1'}})).json();
    if(d.error)alert(d.error);else{sel=d.id;tab='flow';render();load();}}
  catch(err){alert('run failed: '+err);}
  finally{b.disabled=false;b.textContent='▶ Run now';}}
document.getElementById('agentline').textContent=CFG.agentline;
load();setInterval(load,20000);
</script></body></html>"""
# CONFIG reaches the page as one JSON literal (JSON is valid JS, json.dumps
# escapes quotes/backslashes, and <-escaping keeps `</script>` inert), so
# no CONFIG value can break out of the script. Title/emoji land in HTML and get
# html-escaped instead.
_cfg_json = json.dumps({
    "render": CONFIG["deliverable"]["render"],
    "deliv_match": CONFIG["deliverable"]["match"],
    "deliv_label": CONFIG["deliverable"]["label"],
    "hero": CONFIG["hero_tools"],
    "model": CONFIG["model"],
    "agentline": f"{AGENT_ID} · {CONFIG['model']}",
}).replace("<", "\\u003c")
PAGE = (PAGE
        .replace("__CONFIG_JSON__", _cfg_json)
        .replace("__TITLE__", html.escape(CONFIG["title"]))
        .replace("__EMOJI__", html.escape(CONFIG["emoji"])))

if __name__ == "__main__":
    print(f"{CONFIG['title']} runs → http://127.0.0.1:{CONFIG['port']}")
    ThreadingHTTPServer(("127.0.0.1", CONFIG["port"]), Handler).serve_forever()
