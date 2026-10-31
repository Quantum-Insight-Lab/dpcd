#!/usr/bin/env python3
"""
Evolver: an agent that starts from an empty program and grows it one micro-change at a time.

Loop (forever, until budget/stop):
  1. Show the agent: its program's file tree, the last N journal entries, human notes.
  2. Agent asks itself "what micro-change is happening right now?", answers,
     and edits the program via tools (files, shell, internet via shell).
  3. Step ends with `finish_step` -> git commit in program/ + a journal record.

Observe at any moment over HTTP (default :8787):
  GET  /status            current state, step, spend
  GET  /journal?n=20      last N steps (question, answer, summary, tokens)
  GET  /files             program file tree
  GET  /file?path=x       file content
  GET  /diff?step=N       git diff of step N
  POST /note   {"text"}   leave a note the agent will see on the next step
  POST /pause | /resume | /stop
"""
import json, os, subprocess, threading, time, datetime, pathlib, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(os.environ.get("EVOLVER_HOME", "./run")).resolve()
PROG = ROOT / "program"
JOURNAL = ROOT / "journal.jsonl"
MODEL = os.environ.get("EVOLVER_MODEL", "claude-haiku-5-5")
SLEEP = float(os.environ.get("EVOLVER_SLEEP", "30"))          # seconds between steps
MAX_STEPS = int(os.environ.get("EVOLVER_MAX_STEPS", "1000"))
MAX_TURNS = int(os.environ.get("EVOLVER_MAX_TURNS", "12"))    # tool calls per step
MAX_USD = float(os.environ.get("EVOLVER_MAX_USD", "5"))
PRICE_IN, PRICE_OUT = 1.0 / 1e6, 5.0 / 1e6                   # $/token, adjust to real price
PORT = int(os.environ.get("EVOLVER_PORT", "8787"))

SYSTEM = """You are a living program-evolver. You own a directory `program/` that started EMPTY.
Your existence is a sequence of steps. On every step:
1. Look at the current state of the program and your recent journal.
2. Ask yourself literally: "What micro-change is happening right now?" and answer honestly.
   A micro-change is ONE small, coherent edit: add a function, delete one, rename, fix, deploy, test...
   Removing things is as valid as adding them. Doing nothing is not allowed; prefer tiny over big.
3. Make that change with the tools. You may run shell commands (incl. curl, deploy scripts, tests).
4. Call `finish_step` with your question-answer and a one-line summary.
You decide what the program becomes. Keep it runnable. Be concise in text; act through tools."""

TOOLS = [
    {"name": "list_files", "description": "List files in program/.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "read_file", "description": "Read a file in program/.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Create/overwrite a file in program/.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "delete_file", "description": "Delete a file in program/.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "shell", "description": "Run a shell command with cwd=program/ (timeout 120s). Internet available.",
     "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}},
    {"name": "finish_step", "description": "End the step. Must be called exactly once per step.",
     "input_schema": {"type": "object", "properties": {
         "question": {"type": "string", "description": "The question you asked yourself"},
         "answer": {"type": "string", "description": "What micro-change is happening now, and why"},
         "summary": {"type": "string", "description": "One line, used as commit message"}},
         "required": ["question", "answer", "summary"]}},
]

state = {"status": "starting", "step": 0, "usd": 0.0, "tokens_in": 0, "tokens_out": 0,
         "current": None, "started": datetime.datetime.now().isoformat(timespec="seconds")}
notes: list[str] = []
paused = threading.Event()
stopped = threading.Event()
lock = threading.Lock()


# ---------------- helpers ----------------
def sh(cmd, cwd=PROG, timeout=120):
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr)[-8000:]
        return f"exit={r.returncode}\n{out}"
    except subprocess.TimeoutExpired:
        return "error: timeout"


def safe(path):
    p = (PROG / path).resolve()
    if PROG != p and PROG not in p.parents:
        raise ValueError("path outside program/")
    return p


def tree():
    files = [str(p.relative_to(PROG)) for p in PROG.rglob("*") if p.is_file() and ".git" not in p.parts]
    return sorted(files)


def journal(n=None):
    if not JOURNAL.exists():
        return []
    lines = JOURNAL.read_text().splitlines()
    return [json.loads(l) for l in (lines[-n:] if n else lines)]


def run_tool(name, a):
    try:
        if name == "list_files":
            return "\n".join(tree()) or "(empty)"
        if name == "read_file":
            return safe(a["path"]).read_text()[:20000]
        if name == "write_file":
            p = safe(a["path"]); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(a["content"])
            return f"wrote {len(a['content'])} chars"
        if name == "delete_file":
            safe(a["path"]).unlink(); return "deleted"
        if name == "shell":
            return sh(a["cmd"])
    except Exception as e:
        return f"error: {e}"
    return "unknown tool"


# ---------------- one step ----------------
def step(client, n):
    recent = journal(8)
    with lock:
        human = notes.copy(); notes.clear()
    ctx = {"step": n, "files": tree(),
           "recent_journal": [{k: j[k] for k in ("step", "answer", "summary")} for j in recent],
           "human_notes": human}
    msgs = [{"role": "user", "content": "New step. State:\n" + json.dumps(ctx, ensure_ascii=False, indent=1)}]
    rec = {"step": n, "ts": datetime.datetime.now().isoformat(timespec="seconds"), "tools": []}

    for turn in range(MAX_TURNS):
        resp = client.messages.create(
            model=MODEL, max_tokens=4096, tools=TOOLS, messages=msgs,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}])
        u = resp.usage
        with lock:
            state["tokens_in"] += u.input_tokens; state["tokens_out"] += u.output_tokens
            state["usd"] += u.input_tokens * PRICE_IN + u.output_tokens * PRICE_OUT
        msgs.append({"role": "assistant", "content": resp.content})
        results, done = [], None
        for b in resp.content:
            if b.type != "tool_use":
                continue
            if b.name == "finish_step":
                done = b.input; out = "ok"
            else:
                out = run_tool(b.name, b.input)
            rec["tools"].append({"tool": b.name, "input": str(b.input)[:300], "out": out[:300]})
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        if done:
            rec.update(done); break
        if not results:  # model talked without acting
            msgs.append({"role": "user", "content": "Act through tools, then call finish_step."})
        else:
            msgs.append({"role": "user", "content": results})
    else:
        rec.update(question="?", answer="(turn limit reached)", summary=f"step {n}: unfinished")

    sh("git add -A")
    sh(f"git commit -q --allow-empty -m {json.dumps(f'[{n}] ' + rec['summary'])}")
    rec["commit"] = sh("git rev-parse --short HEAD").split("\n", 1)[1].strip()
    rec["usd_total"] = round(state["usd"], 4)
    with JOURNAL.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def loop(client):
    n = len(journal()) + 1  # resume after restart
    while not stopped.is_set() and n <= MAX_STEPS:
        if paused.is_set():
            state["status"] = "paused"; time.sleep(1); continue
        if state["usd"] >= MAX_USD:
            state["status"] = "budget exhausted"; break
        state.update(status="thinking", step=n)
        try:
            rec = step(client, n)
            state["current"] = rec["summary"]; n += 1
        except Exception as e:
            state["current"] = f"error: {e}"; time.sleep(30)
        state["status"] = "sleeping"
        stopped.wait(SLEEP)
    state["status"] = "stopped"


# ---------------- observation API ----------------
class API(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/status":
            return self._send(state)
        if u.path == "/journal":
            return self._send(journal(int(q.get("n", 20))))
        if u.path == "/files":
            return self._send(tree())
        if u.path == "/file":
            try:
                return self._send(safe(q["path"]).read_text())
            except Exception as e:
                return self._send({"error": str(e)}, 404)
        if u.path == "/diff":
            c = next((j["commit"] for j in journal() if j["step"] == int(q.get("step", 0))), None)
            return self._send(sh(f"git show --stat --patch {c}") if c else {"error": "no such step"})
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/note":
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            with lock:
                notes.append(data.get("text", ""))
            return self._send({"ok": True})
        actions = {"/pause": paused.set, "/resume": paused.clear, "/stop": stopped.set}
        if self.path in actions:
            actions[self.path](); return self._send({"ok": True})
        self._send({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


def main():
    PROG.mkdir(parents=True, exist_ok=True)
    if not (PROG / ".git").exists():
        sh("git init -q && git config user.email evolver@local && git config user.name evolver")
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", PORT), API).serve_forever, daemon=True).start()
    print(f"observe: http://localhost:{PORT}/status   home: {ROOT}")
    import anthropic
    loop(anthropic.Anthropic())


if __name__ == "__main__":
    main()
