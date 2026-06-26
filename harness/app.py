import json
import os
import pathlib
import shutil
import time
import urllib.parse
import uuid

import requests as http_requests
from flask import Flask, Response, jsonify, render_template, request, session
from openai import OpenAI

VLLM_URL = os.environ.get("VLLM_URL", "http://nemotron-omni:8000/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "nemotron-omni")
SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://sandbox:8000")
# The one port inside the sandbox that the Preview tab proxies to. Nemo serves
# his apps here; the harness reverse-proxies it under /preview/ so a single
# front door (this harness) stays the only thing exposed to the host.
PREVIEW_PORT = int(os.environ.get("PREVIEW_PORT", "7777"))
_SANDBOX_HOST = urllib.parse.urlsplit(SANDBOX_URL).hostname or "sandbox"
PREVIEW_BASE = f"http://{_SANDBOX_HOST}:{PREVIEW_PORT}"
# Headers we must not copy verbatim when relaying the upstream response.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "content-length",
}
BROWSE_ROOT = pathlib.Path(os.environ.get("BROWSE_ROOT", "/host")).resolve()
MEDIA_ROOT = pathlib.Path(os.environ.get("MEDIA_ROOT", "/media")).resolve()
CONVERSATIONS_DIR = pathlib.Path(os.environ.get("CONVERSATIONS_DIR", "/app/conversations"))
CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)

MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "15"))
EXEC_HTTP_TIMEOUT = 320  # slightly more than sandbox's MAX_EXEC_TIMEOUT

MEMORY_PATH = ".nemo/MEMORY.md"
MAX_MEMORY_BYTES = 50_000

DEFAULT_SYSTEM_PROMPT = (
    "You are Nemo, a multimodal reasoning assistant running locally on a DGX Spark. "
    "You can see images, hear audio, and watch video clips when the user attaches them.\n\n"
    "You have a persistent sandbox workspace at /workspace where you can read, write, "
    "and edit files, run shell commands, and start long-running processes. The workspace "
    "survives between conversations. Network access is available, so you can curl APIs and "
    "pip-install packages. To keep installs persistent, create a venv inside /workspace "
    "(e.g. `python -m venv /workspace/.venv`) — packages installed into the system python "
    "are lost on container restart.\n\n"
    "## Memory\n"
    "Your persistent memory lives at /workspace/.nemo/MEMORY.md and is auto-loaded into your "
    "context (you can see it below if it exists). When the user asks you to remember, save, or "
    "note something, call the `remember` tool with **structured markdown** — section headings, "
    "bullet points, multi-line entries with examples. Don't squash everything into one sentence; "
    "write it the way you'd want to read it next time.\n\n"
    "Group related facts under `## Topic` headings. If a relevant heading already exists in "
    "memory, extend it; otherwise create a new one. For unrelated topics, prefer separate "
    "`remember` calls so each topic lands as its own block. For larger cleanups or restructures, "
    "use `read_file` + `write_file` on the file directly.\n\n"
    "Example shape for a typical entry:\n\n"
    "```markdown\n"
    "## Music backend (`gary`)\n"
    "Runs on this Spark alongside Nemo. Provides five model APIs:\n"
    "- `stable-audio-open-small` — BPM-aware drum loops\n"
    "- `foundation-1` — BPM- and key-aware synth loops\n"
    "- `musicgen` — 32 kHz audio continuations; multiple finetunes available\n"
    "- `melodyflow` — audio→audio transformation, output matches input length\n"
    "- `ace-step-v15` — continuations, remixes, vocals with lyrics; LoRAs include `john`, `billie`\n"
    "```\n\n"
    "## Building apps (the Preview tab)\n"
    "You can build web apps the user watches live in their **Preview** tab. The harness "
    f"reverse-proxies one fixed port inside your sandbox — **{PREVIEW_PORT}** — into that tab. "
    "The loop:\n"
    "1. Write your app under /workspace (e.g. /workspace/apps/<name>/).\n"
    f"2. Serve it on 0.0.0.0:{PREVIEW_PORT} with `start_server`. For a static page: "
    f"`python -m http.server {PREVIEW_PORT}` with `cwd` set to the app folder. For a Flask "
    f"app: bind `host='0.0.0.0', port={PREVIEW_PORT}`.\n"
    "3. Tell the user to open (or reload) the ▶ Preview tab.\n"
    f"Only one app can hold port {PREVIEW_PORT} at a time — `stop_server` the previous one "
    "first, or reuse the same server name. Keep pages self-contained or use **relative** asset "
    "paths (`./app.js`, not `/app.js`) so they resolve under the proxy. Client-side JavaScript "
    "runs normally; for a backend, prefer server-rendered HTML and form posts.\n\n"
    "Use the available tools rather than describing what you would do. Keep responses concise."
)

DEFAULT_SETTINGS = {
    "temperature": 0.6,
    "top_p": 0.95,
    "max_tokens": 2048,
    "enable_thinking": False,
    "thinking_token_budget": 4096,
    "use_audio_in_video": False,
    "tools_enabled": True,
}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


def kind_for(name: str):
    ext = pathlib.Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return None


# ---------------------------------------------------------------------------
# Tool schemas — what Nemo sees when we hand the chat-completion `tools=` arg.
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the contents of a directory in the sandbox workspace. Use empty string for the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within /workspace"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the sandbox workspace. Returns up to 5MB.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a text file in the sandbox workspace, replacing any existing content. Creates parent directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exactly one occurrence of `old` with `new` in a workspace file. Fails if the string is missing or appears more than once — include enough surrounding context to make `old` unique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file or directory in the sandbox workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_dir",
            "description": "Create a directory in the sandbox workspace (mkdir -p).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move",
            "description": "Move or rename a file or directory in the sandbox workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "dst": {"type": "string"},
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a bash command in the sandbox workspace. Returns exit_code, stdout (last 32KB), stderr (last 8KB). Default timeout 30s, max 300s. Default cwd is /workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "description": "Relative to /workspace"},
                    "timeout": {"type": "integer", "description": "Seconds; capped at 300"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_server",
            "description": "Launch a long-running process in the background (e.g. a flask app, a dev server). stdout/stderr is captured to /workspace/.sandbox/logs/<name>.log. Returns once the process is launched (does not wait for completion).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Optional handle; auto-generated if omitted"},
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_server",
            "description": "Stop a process previously launched with start_server.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_servers",
            "description": "List background processes (running and recently stopped) with their commands, pids, and exit codes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "server_logs",
            "description": "Read the last ~16KB of a background process's combined stdout/stderr log.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Append markdown content to your persistent memory at /workspace/.nemo/MEMORY.md. "
                "Use this whenever the user asks you to remember, save, or note something durable "
                "about themselves, their projects, or the environment. Memory is auto-loaded into "
                "your context on every future conversation. "
                "Pass well-structured markdown — section headings (`## Topic`), bullet points, "
                "multi-line entries with examples. The content is appended verbatim. "
                "Existing memory is already visible in your system prompt — use that to decide "
                "whether to add a new section or extend an existing one. For unrelated topics, "
                "prefer separate calls so each lands as its own block; for one cohesive topic, a "
                "single richly-structured call is better than several thin ones. "
                "For larger reorganizations or cleanups, use read_file + write_file directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Markdown content to append. May include headings, bullets, multiple lines.",
                    },
                },
                "required": ["content"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# App + storage
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "nemo-omni-dev-secret")
client = OpenAI(base_url=VLLM_URL, api_key="EMPTY")


@app.context_processor
def inject_asset_versions():
    base = pathlib.Path(__file__).parent / "static"
    versions = {}
    for name in ("app.js", "app.css"):
        try:
            versions[name.replace(".", "_")] = int((base / name).stat().st_mtime)
        except OSError:
            versions[name.replace(".", "_")] = 0
    return versions


def get_session_id() -> str:
    sid = session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        session["sid"] = sid
    return sid


def conversation_path(sid: str) -> pathlib.Path:
    return CONVERSATIONS_DIR / f"{sid}.json"


def load_conversation(sid: str) -> dict:
    path = conversation_path(sid)
    if path.exists():
        conv = json.loads(path.read_text())
        for key, val in DEFAULT_SETTINGS.items():
            conv.setdefault("settings", {}).setdefault(key, val)
        return conv
    now = time.time()
    return {
        "id": sid,
        "created_at": now,
        "title": "",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "system_prompt_history": [
            {"ts": now, "source": "default", "content": DEFAULT_SYSTEM_PROMPT}
        ],
        "messages": [],
        "settings": dict(DEFAULT_SETTINGS),
    }


def save_conversation(conv: dict) -> None:
    conv["updated_at"] = time.time()
    conversation_path(conv["id"]).write_text(json.dumps(conv, indent=2))


def current_thread_id() -> str:
    """The conversation the browser is viewing. If the session has no active
    thread yet (or its file was deleted), resume the most recently updated
    conversation so you pick up where you left off; only mint a fresh id when
    there are no conversations at all (and the legacy per-session id seeds it)."""
    tid = session.get("thread")
    if tid and conversation_path(tid).exists():
        return tid
    existing = list_threads("")
    tid = existing[0]["id"] if existing else get_session_id()
    session["thread"] = tid
    return tid


def valid_tid(tid: str) -> bool:
    # Our ids are uuid4().hex (alphanumeric). Reject anything else so a crafted
    # id can't escape CONVERSATIONS_DIR through the filename.
    return bool(tid) and tid.isalnum()


def thread_title(conv: dict) -> str:
    if conv.get("title"):
        return conv["title"]
    for m in conv.get("messages", []):
        if m.get("role") == "user":
            text = (m.get("text") or "").strip().replace("\n", " ")
            if text:
                return text[:60]
    return "New conversation"


def list_threads(active_id: str) -> list:
    threads = []
    for path in CONVERSATIONS_DIR.glob("*.json"):
        try:
            conv = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        tid = conv.get("id", path.stem)
        threads.append({
            "id": tid,
            "title": thread_title(conv),
            "message_count": sum(1 for m in conv.get("messages", []) if m.get("role") == "user"),
            "created_at": conv.get("created_at"),
            "updated_at": conv.get("updated_at") or path.stat().st_mtime,
            "active": tid == active_id,
        })
    threads.sort(key=lambda t: t["updated_at"] or 0, reverse=True)
    return threads


def under_root(target: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Sandbox tool dispatcher
# ---------------------------------------------------------------------------

def sandbox_call(method: str, path: str, **kwargs) -> dict:
    url = f"{SANDBOX_URL}{path}"
    timeout = kwargs.pop("timeout", 30)
    try:
        r = http_requests.request(method, url, timeout=timeout, **kwargs)
    except http_requests.RequestException as exc:
        return {"error": f"sandbox unreachable: {type(exc).__name__}: {exc}"}
    try:
        return r.json()
    except ValueError:
        return {"error": f"non-json sandbox response (status {r.status_code}): {r.text[:500]}"}


def execute_tool(conv: dict, name: str, arguments: dict) -> dict:
    """Dispatch a tool call. Returns a dict suitable for json-serializing as the
    `tool` message content sent back to the model.
    """
    if name == "list_files":
        return sandbox_call("GET", "/fs", params={"path": arguments.get("path", "")})
    if name == "read_file":
        return sandbox_call("GET", "/fs/file", params={"path": arguments.get("path", "")})
    if name == "write_file":
        return sandbox_call("POST", "/fs/file", json=arguments)
    if name == "edit_file":
        return sandbox_call("POST", "/fs/edit", json=arguments)
    if name == "delete_file":
        return sandbox_call("POST", "/fs/delete", json=arguments)
    if name == "make_dir":
        return sandbox_call("POST", "/fs/mkdir", json=arguments)
    if name == "move":
        return sandbox_call("POST", "/fs/move", json=arguments)
    if name == "run_shell":
        return sandbox_call("POST", "/exec", json=arguments, timeout=EXEC_HTTP_TIMEOUT)
    if name == "start_server":
        return sandbox_call("POST", "/processes/start", json=arguments)
    if name == "stop_server":
        return sandbox_call("POST", "/processes/stop", json=arguments)
    if name == "list_servers":
        return sandbox_call("GET", "/processes")
    if name == "server_logs":
        return sandbox_call("GET", "/processes/logs", params=arguments)
    if name == "remember":
        block = arguments.get("content", "")
        if not isinstance(block, str) or not block.strip():
            return {"error": "'content' must be a non-empty string"}
        block = block.rstrip() + "\n"
        existing = sandbox_call("GET", "/fs/file", params={"path": MEMORY_PATH})
        prior = ""
        if isinstance(existing, dict) and isinstance(existing.get("content"), str):
            prior = existing["content"]
        if not prior.strip():
            new_content = (
                "# Memory\n"
                "<persistent context auto-loaded into every conversation; "
                "edit with `remember`, `read_file`, `write_file`>\n\n"
                f"{block}"
            )
        else:
            sep = "" if prior.endswith("\n\n") else ("\n" if prior.endswith("\n") else "\n\n")
            new_content = f"{prior}{sep}{block}"
        result = sandbox_call("POST", "/fs/file", json={"path": MEMORY_PATH, "content": new_content})
        if isinstance(result, dict) and result.get("error"):
            return result
        return {"ok": True, "appended_chars": len(block), "memory_size": result.get("size")}
    return {"error": f"unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    get_session_id()
    return render_template("index.html")


@app.route("/api/session")
def api_session():
    return jsonify(load_conversation(current_thread_id()))


@app.route("/api/session/system-prompt", methods=["PATCH"])
def api_update_system_prompt():
    sid = current_thread_id()
    conv = load_conversation(sid)
    body = request.get_json() or {}
    new_prompt = body.get("content", "")
    source = body.get("source", "user")
    conv["system_prompt"] = new_prompt
    conv["system_prompt_history"].append(
        {"ts": time.time(), "source": source, "content": new_prompt}
    )
    save_conversation(conv)
    return jsonify({"ok": True, "system_prompt": new_prompt})


@app.route("/api/session/settings", methods=["PATCH"])
def api_update_settings():
    sid = current_thread_id()
    conv = load_conversation(sid)
    body = request.get_json() or {}
    for key, val in body.items():
        if key in DEFAULT_SETTINGS:
            conv["settings"][key] = val
    save_conversation(conv)
    return jsonify({"ok": True, "settings": conv["settings"]})


@app.route("/api/session/reset", methods=["POST"])
def api_reset():
    sid = current_thread_id()
    conv = load_conversation(sid)
    conv["messages"] = []
    save_conversation(conv)
    return jsonify({"ok": True})


# --- Threads (multiple conversations you can switch between) ----------------

@app.route("/api/threads")
def api_threads():
    active = current_thread_id()
    return jsonify({"active": active, "threads": list_threads(active)})


@app.route("/api/threads", methods=["POST"])
def api_threads_new():
    tid = uuid.uuid4().hex
    save_conversation(load_conversation(tid))   # fresh default, persisted
    session["thread"] = tid
    return jsonify({"ok": True, "active": tid})


@app.route("/api/threads/activate", methods=["POST"])
def api_threads_activate():
    tid = (request.get_json() or {}).get("id", "")
    if not valid_tid(tid) or not conversation_path(tid).exists():
        return jsonify({"error": "no such thread"}), 404
    session["thread"] = tid
    return jsonify({"ok": True, "active": tid})


@app.route("/api/threads/<tid>", methods=["PATCH"])
def api_threads_rename(tid):
    if not valid_tid(tid) or not conversation_path(tid).exists():
        return jsonify({"error": "no such thread"}), 404
    conv = load_conversation(tid)
    conv["title"] = ((request.get_json() or {}).get("title") or "").strip()[:120]
    save_conversation(conv)
    return jsonify({"ok": True, "title": conv["title"]})


@app.route("/api/threads/<tid>", methods=["DELETE"])
def api_threads_delete(tid):
    if not valid_tid(tid):
        return jsonify({"error": "bad id"}), 400
    conversation_path(tid).unlink(missing_ok=True)
    if session.get("thread") == tid:
        remaining = list_threads(tid)
        if remaining:
            session["thread"] = remaining[0]["id"]
        else:
            ntid = uuid.uuid4().hex
            save_conversation(load_conversation(ntid))
            session["thread"] = ntid
    return jsonify({"ok": True, "active": session["thread"]})


# --- Media uploads ---------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
def api_upload():
    sid = get_session_id()
    if "file" not in request.files:
        return jsonify({"error": "no file in request"}), 400
    f = request.files["file"]
    name = pathlib.Path(f.filename or "upload").name
    kind = kind_for(name)
    if kind is None:
        return jsonify({"error": f"unsupported file type: {name}"}), 400
    target_dir = MEDIA_ROOT / sid
    target_dir.mkdir(parents=True, exist_ok=True)
    final_name = f"{uuid.uuid4().hex[:8]}-{name}"
    target = target_dir / final_name
    f.save(target)
    return jsonify({
        "display_name": name,
        "path": str(target),
        "url": f"file://{target}",
        "kind": kind,
        "size": target.stat().st_size,
    })


# --- Host file picker ------------------------------------------------------

@app.route("/api/files")
def api_files():
    rel = request.args.get("path", "").lstrip("/")
    target = (BROWSE_ROOT / rel).resolve()
    if not under_root(target, BROWSE_ROOT):
        return jsonify({"error": "outside browse root"}), 400
    if not target.exists() or not target.is_dir():
        return jsonify({"error": "not a directory"}), 404
    entries = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return jsonify({"error": "permission denied"}), 403
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            stat = child.stat()
        except (PermissionError, FileNotFoundError, OSError):
            continue
        entries.append({
            "name": child.name,
            "is_dir": child.is_dir(),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "kind": kind_for(child.name) if not child.is_dir() else None,
        })
    rel_path = "" if target == BROWSE_ROOT else str(target.relative_to(BROWSE_ROOT))
    parent_rel = ""
    if target != BROWSE_ROOT:
        parent = target.parent
        parent_rel = "" if parent == BROWSE_ROOT else str(parent.relative_to(BROWSE_ROOT))
    return jsonify({
        "path": rel_path,
        "absolute": str(target),
        "parent": parent_rel,
        "entries": entries,
    })


@app.route("/api/files/select", methods=["POST"])
def api_files_select():
    sid = get_session_id()
    body = request.get_json() or {}
    rel = body.get("path", "").lstrip("/")
    target = (BROWSE_ROOT / rel).resolve()
    if not under_root(target, BROWSE_ROOT):
        return jsonify({"error": "outside browse root"}), 400
    if not target.is_file():
        return jsonify({"error": "not a file"}), 404
    name = target.name
    kind = kind_for(name)
    if kind is None:
        return jsonify({"error": f"unsupported file type: {name}"}), 400
    media_dir = MEDIA_ROOT / sid
    media_dir.mkdir(parents=True, exist_ok=True)
    final_name = f"{uuid.uuid4().hex[:8]}-{name}"
    dest = media_dir / final_name
    shutil.copy2(target, dest)
    return jsonify({
        "display_name": name,
        "path": str(dest),
        "url": f"file://{dest}",
        "kind": kind,
        "size": dest.stat().st_size,
    })


# --- Sandbox proxy (workspace browser + edits from the UI) -----------------

@app.route("/api/sandbox/files")
def api_sandbox_files():
    return jsonify(sandbox_call("GET", "/fs", params={"path": request.args.get("path", "")}))


@app.route("/api/sandbox/file", methods=["GET"])
def api_sandbox_read():
    return jsonify(sandbox_call("GET", "/fs/file", params={"path": request.args.get("path", "")}))


@app.route("/api/sandbox/file", methods=["POST"])
def api_sandbox_write():
    body = request.get_json() or {}
    return jsonify(sandbox_call("POST", "/fs/file", json=body))


@app.route("/api/sandbox/file", methods=["DELETE"])
def api_sandbox_delete():
    body = request.get_json() or {}
    return jsonify(sandbox_call("POST", "/fs/delete", json=body))


@app.route("/api/sandbox/mkdir", methods=["POST"])
def api_sandbox_mkdir():
    body = request.get_json() or {}
    return jsonify(sandbox_call("POST", "/fs/mkdir", json=body))


# ---------------------------------------------------------------------------
# Preview: reverse-proxy the app Nemo is serving on PREVIEW_PORT inside the
# sandbox. The iframe is sandboxed (no allow-same-origin) so the previewed app
# is a foreign origin to the harness and can't reach back at its chrome.
# ---------------------------------------------------------------------------

@app.route("/api/preview/status")
def api_preview_status():
    try:
        r = http_requests.get(PREVIEW_BASE + "/", timeout=2)
        return jsonify({"up": True, "port": PREVIEW_PORT, "status": r.status_code})
    except http_requests.RequestException:
        return jsonify({"up": False, "port": PREVIEW_PORT})


@app.route("/preview/", defaults={"subpath": ""},
           methods=["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"])
@app.route("/preview/<path:subpath>",
           methods=["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"])
def preview_proxy(subpath):
    target = f"{PREVIEW_BASE}/{subpath}"
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "cookie", "content-length")
    }
    try:
        upstream = http_requests.request(
            request.method, target,
            params=request.args, data=request.get_data(),
            headers=fwd_headers, allow_redirects=False, timeout=30,
        )
    except http_requests.RequestException as exc:
        return Response(
            "<!doctype html><meta charset=utf-8>"
            "<body style='font:14px system-ui;padding:2rem;color:#bbb;background:#1a1a1a'>"
            f"<h3>Nothing is serving on port {PREVIEW_PORT}</h3>"
            "<p>Ask Nemo to build an app and <code>start_server</code> it listening on "
            f"<code>0.0.0.0:{PREVIEW_PORT}</code>, then reload this tab.</p>"
            f"<p style='color:#666'>({type(exc).__name__})</p></body>",
            status=502, mimetype="text/html",
        )
    headers = [(k, v) for k, v in upstream.headers.items()
               if k.lower() not in _HOP_BY_HOP]
    return Response(upstream.content, status=upstream.status_code, headers=headers)


# ---------------------------------------------------------------------------
# Chat: agentic loop
# ---------------------------------------------------------------------------

def build_message_content(text: str, attachments: list):
    if not attachments:
        return text or ""
    parts = []
    for a in attachments:
        kind = a["kind"]
        url = a["url"]
        if kind == "image":
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif kind == "audio":
            parts.append({"type": "audio_url", "audio_url": {"url": url}})
        elif kind == "video":
            parts.append({"type": "video_url", "video_url": {"url": url}})
    if text:
        parts.append({"type": "text", "text": text})
    return parts


def load_memory() -> str | None:
    """Fetch /workspace/.nemo/MEMORY.md via the sandbox API. Returns None if
    missing or unreadable."""
    res = sandbox_call("GET", "/fs/file", params={"path": MEMORY_PATH})
    if not isinstance(res, dict) or res.get("error"):
        return None
    if res.get("binary"):
        return None
    content = res.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    if len(content) > MAX_MEMORY_BYTES:
        content = content[:MAX_MEMORY_BYTES] + "\n\n... (memory truncated)"
    return content


def build_system_prompt(conv: dict) -> str:
    base = conv.get("system_prompt") or ""
    memory = load_memory()
    if not memory:
        return base
    sep = (
        "\n\n# Memory\n"
        f"The following is your persistent memory loaded from /workspace/{MEMORY_PATH}. "
        "It survives across conversations. When you learn something durable, edit this "
        "file with `edit_file` or `write_file` to keep it.\n\n"
    )
    return base + sep + memory


def to_api_messages(conv: dict):
    out = [{"role": "system", "content": build_system_prompt(conv)}]
    for m in conv["messages"]:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            api_msg = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                api_msg["tool_calls"] = m["tool_calls"]
                # OpenAI requires content=None when tool_calls is present and content is empty
                if not api_msg["content"]:
                    api_msg["content"] = None
            out.append(api_msg)
        elif m["role"] == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m["tool_call_id"],
                "content": m["content"],
            })
    return out


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.route("/api/chat", methods=["POST"])
def api_chat():
    sid = current_thread_id()
    body = request.get_json() or {}
    user_text = body.get("text", "")
    attachments = body.get("attachments", []) or []

    conv = load_conversation(sid)
    settings = dict(conv["settings"])

    user_message = {
        "role": "user",
        "content": build_message_content(user_text, attachments),
        "ts": time.time(),
        "text": user_text,
        "attachments": [
            {"display_name": a["display_name"], "kind": a["kind"], "url": a["url"]}
            for a in attachments
        ],
    }
    conv["messages"].append(user_message)
    save_conversation(conv)

    extra_body = {
        "chat_template_kwargs": {
            "enable_thinking": bool(settings.get("enable_thinking")),
            "reasoning_budget": int(settings.get("thinking_token_budget", 4096)),
        },
    }
    if settings.get("enable_thinking"):
        extra_body["thinking_token_budget"] = int(settings.get("thinking_token_budget", 4096)) + 1024
    if any(a["kind"] == "video" for a in attachments) and settings.get("use_audio_in_video"):
        extra_body["mm_processor_kwargs"] = {"use_audio_in_video": True}

    tools = TOOL_SCHEMAS if settings.get("tools_enabled", True) else None

    def generate():
        rounds = 0
        try:
            while rounds < MAX_TOOL_ROUNDS:
                round_id = uuid.uuid4().hex[:8]
                yield sse("round_start", {"round_id": round_id, "iter": rounds})

                api_messages = to_api_messages(conv)
                kwargs = dict(
                    model=VLLM_MODEL,
                    messages=api_messages,
                    temperature=float(settings.get("temperature", 0.6)),
                    top_p=float(settings.get("top_p", 0.95)),
                    max_tokens=int(settings.get("max_tokens", 2048)),
                    stream=True,
                    extra_body=extra_body,
                )
                if tools:
                    kwargs["tools"] = tools

                stream = client.chat.completions.create(**kwargs)
                content_buf = []
                reasoning_buf = []
                tool_calls_acc: dict[int, dict] = {}
                finish_reason = None

                for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    d = delta.model_dump() if hasattr(delta, "model_dump") else dict(delta)

                    rc = d.get("reasoning_content") or d.get("reasoning")
                    if rc:
                        reasoning_buf.append(rc)
                        yield sse("reasoning", {"round_id": round_id, "delta": rc})

                    cd = d.get("content")
                    if cd:
                        content_buf.append(cd)
                        yield sse("content", {"round_id": round_id, "delta": cd})

                    for tc in d.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls_acc.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                            yield sse("tool_call_start", {
                                "round_id": round_id,
                                "index": idx,
                                "id": slot["id"],
                                "name": fn["name"],
                            })
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                            yield sse("tool_call_args", {
                                "round_id": round_id,
                                "index": idx,
                                "delta": fn["arguments"],
                            })

                full_content = "".join(content_buf)
                full_reasoning = "".join(reasoning_buf) or None

                tool_calls_list = []
                for idx in sorted(tool_calls_acc.keys()):
                    slot = tool_calls_acc[idx]
                    tool_calls_list.append({
                        "id": slot["id"] or f"call_{round_id}_{idx}",
                        "type": "function",
                        "function": {
                            "name": slot["name"],
                            "arguments": slot["arguments"],
                        },
                    })

                assistant_msg = {
                    "role": "assistant",
                    "content": full_content,
                    "reasoning": full_reasoning,
                    "round_id": round_id,
                    "ts": time.time(),
                    "finish_reason": finish_reason,
                }
                if tool_calls_list:
                    assistant_msg["tool_calls"] = tool_calls_list
                conv["messages"].append(assistant_msg)
                save_conversation(conv)

                if finish_reason != "tool_calls" or not tool_calls_list:
                    yield sse("done", {"finish_reason": finish_reason, "round_id": round_id})
                    return

                # Execute each tool call, persist result, emit to UI
                for tc in tool_calls_list:
                    tname = tc["function"]["name"]
                    targs_raw = tc["function"]["arguments"]
                    try:
                        targs = json.loads(targs_raw) if targs_raw else {}
                    except json.JSONDecodeError as exc:
                        result = {"error": f"invalid JSON arguments: {exc}", "raw": targs_raw}
                    else:
                        yield sse("tool_call_exec", {
                            "round_id": round_id,
                            "id": tc["id"],
                            "name": tname,
                        })
                        result = execute_tool(conv, tname, targs)

                    yield sse("tool_result", {
                        "round_id": round_id,
                        "id": tc["id"],
                        "name": tname,
                        "result": result,
                        "is_error": "error" in (result or {}),
                    })

                    conv["messages"].append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tname,
                        "content": json.dumps(result),
                        "round_id": round_id,
                        "ts": time.time(),
                    })
                    save_conversation(conv)

                rounds += 1

            yield sse("error", {
                "message": f"reached max tool-call rounds ({MAX_TOOL_ROUNDS}); stopping.",
            })
        except Exception as exc:  # noqa: BLE001
            yield sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True, debug=False)
