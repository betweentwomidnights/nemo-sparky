"""
Sandbox API server.

Exposes file ops, shell exec, and long-lived process management, all scoped
to /workspace. Reachable only on the compose internal network — not exposed
to the host.
"""

import os
import pathlib
import shutil
import signal
import subprocess
import time
import uuid

from flask import Flask, jsonify, request

WORKSPACE = pathlib.Path(os.environ.get("WORKSPACE", "/workspace")).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

PROCESS_LOG_DIR = WORKSPACE / ".sandbox" / "logs"
PROCESS_LOG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_EXEC_TIMEOUT = 30
MAX_EXEC_TIMEOUT = 300
MAX_FILE_BYTES = 5 * 1024 * 1024
STDOUT_TAIL = 32_000
STDERR_TAIL = 8_000

app = Flask(__name__)
PROCESSES: dict[str, dict] = {}  # name -> {popen, started_at, command, log_path, log_file, cwd}


def safe_path(rel: str) -> pathlib.Path:
    if rel is None:
        rel = ""
    rel = str(rel).lstrip("/")
    target = (WORKSPACE / rel).resolve()
    try:
        target.relative_to(WORKSPACE)
    except ValueError as exc:
        raise PermissionError(f"path escapes workspace: {rel}") from exc
    return target


def err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


@app.route("/health")
def health():
    return jsonify({"ok": True, "workspace": str(WORKSPACE)})


@app.route("/fs")
def list_files():
    rel = request.args.get("path", "")
    try:
        target = safe_path(rel)
    except PermissionError as exc:
        return err(str(exc), 403)
    if not target.exists():
        return err("not found", 404)
    if target.is_file():
        return err("path is a file; use /fs/file", 400)
    entries = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return err("permission denied", 403)
    for child in children:
        try:
            stat = child.stat()
        except (PermissionError, FileNotFoundError, OSError):
            continue
        entries.append({
            "name": child.name,
            "is_dir": child.is_dir(),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    rel_path = "" if target == WORKSPACE else str(target.relative_to(WORKSPACE))
    return jsonify({"path": rel_path, "entries": entries})


@app.route("/fs/file", methods=["GET"])
def read_file():
    rel = request.args.get("path", "")
    if not rel:
        return err("'path' query param required")
    try:
        target = safe_path(rel)
    except PermissionError as exc:
        return err(str(exc), 403)
    if not target.is_file():
        return err("not a file", 404)
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        return jsonify({
            "path": rel, "size": size, "binary": False,
            "content": None, "error": f"file too large ({size} bytes); limit is {MAX_FILE_BYTES}",
        }), 200
    try:
        text = target.read_text()
        return jsonify({"path": rel, "size": size, "content": text})
    except UnicodeDecodeError:
        return jsonify({"path": rel, "size": size, "binary": True, "content": None})


@app.route("/fs/file", methods=["POST"])
def write_file():
    body = request.get_json(silent=True) or {}
    rel = body.get("path", "")
    content = body.get("content", "")
    if not rel:
        return err("'path' is required")
    try:
        target = safe_path(rel)
    except PermissionError as exc:
        return err(str(exc), 403)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return jsonify({"path": rel, "size": target.stat().st_size})


@app.route("/fs/edit", methods=["POST"])
def edit_file():
    body = request.get_json(silent=True) or {}
    rel = body.get("path", "")
    old = body.get("old", "")
    new = body.get("new", "")
    if not rel:
        return err("'path' is required")
    if not old:
        return err("'old' must be a non-empty string")
    try:
        target = safe_path(rel)
    except PermissionError as exc:
        return err(str(exc), 403)
    if not target.is_file():
        return err("not a file", 404)
    text = target.read_text()
    count = text.count(old)
    if count == 0:
        return err("'old' string not found in file")
    if count > 1:
        return err(f"'old' string is ambiguous ({count} matches); include more context")
    target.write_text(text.replace(old, new, 1))
    return jsonify({"path": rel, "size": target.stat().st_size, "replaced": 1})


@app.route("/fs/delete", methods=["POST"])
def delete_path():
    body = request.get_json(silent=True) or {}
    rel = body.get("path", "")
    if not rel:
        return err("'path' is required")
    try:
        target = safe_path(rel)
    except PermissionError as exc:
        return err(str(exc), 403)
    if target == WORKSPACE:
        return err("cannot delete workspace root", 400)
    if not target.exists():
        return err("not found", 404)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return jsonify({"deleted": rel})


@app.route("/fs/mkdir", methods=["POST"])
def make_dir():
    body = request.get_json(silent=True) or {}
    rel = body.get("path", "")
    if not rel:
        return err("'path' is required")
    try:
        target = safe_path(rel)
    except PermissionError as exc:
        return err(str(exc), 403)
    target.mkdir(parents=True, exist_ok=True)
    return jsonify({"path": rel, "is_dir": True})


@app.route("/fs/move", methods=["POST"])
def move_path():
    body = request.get_json(silent=True) or {}
    src = body.get("src", "")
    dst = body.get("dst", "")
    if not src or not dst:
        return err("'src' and 'dst' are required")
    try:
        s = safe_path(src)
        d = safe_path(dst)
    except PermissionError as exc:
        return err(str(exc), 403)
    if not s.exists():
        return err("source not found", 404)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return jsonify({"src": src, "dst": dst})


@app.route("/exec", methods=["POST"])
def exec_shell():
    body = request.get_json(silent=True) or {}
    command = body.get("command", "")
    cwd_rel = body.get("cwd", "")
    try:
        timeout = int(body.get("timeout", DEFAULT_EXEC_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_EXEC_TIMEOUT
    timeout = max(1, min(timeout, MAX_EXEC_TIMEOUT))
    if not command:
        return err("'command' is required")
    try:
        cwd = safe_path(cwd_rel) if cwd_rel else WORKSPACE
    except PermissionError as exc:
        return err(str(exc), 403)
    if not cwd.is_dir():
        cwd = WORKSPACE
    cwd_disp = "" if cwd == WORKSPACE else str(cwd.relative_to(WORKSPACE))
    try:
        result = subprocess.run(
            command, shell=True, cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
        out = result.stdout or ""
        errout = result.stderr or ""
        return jsonify({
            "command": command,
            "cwd": cwd_disp,
            "exit_code": result.returncode,
            "stdout": out[-STDOUT_TAIL:],
            "stderr": errout[-STDERR_TAIL:],
            "truncated": len(out) > STDOUT_TAIL or len(errout) > STDERR_TAIL,
            "timeout": False,
        })
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"")
        errout = (exc.stderr or b"")
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(errout, bytes):
            errout = errout.decode(errors="replace")
        return jsonify({
            "command": command,
            "cwd": cwd_disp,
            "exit_code": None,
            "stdout": out[-STDOUT_TAIL:],
            "stderr": errout[-STDERR_TAIL:],
            "truncated": False,
            "timeout": True,
            "error": f"timed out after {timeout}s",
        }), 200


@app.route("/processes", methods=["GET"])
def list_processes():
    out = []
    for name, info in list(PROCESSES.items()):
        running = info["popen"].poll() is None
        if not running and "exit_code" not in info:
            info["exit_code"] = info["popen"].returncode
        out.append({
            "name": name,
            "command": info["command"],
            "cwd": info.get("cwd", ""),
            "started_at": info["started_at"],
            "running": running,
            "exit_code": info.get("exit_code"),
            "log_path": str(info["log_path"].relative_to(WORKSPACE)),
            "pid": info["popen"].pid,
        })
    return jsonify({"processes": out})


@app.route("/processes/start", methods=["POST"])
def start_process():
    body = request.get_json(silent=True) or {}
    command = body.get("command", "")
    cwd_rel = body.get("cwd", "")
    name = body.get("name") or f"proc-{uuid.uuid4().hex[:8]}"
    if not command:
        return err("'command' is required")
    if name in PROCESSES and PROCESSES[name]["popen"].poll() is None:
        return err(f"process '{name}' already running")
    try:
        cwd = safe_path(cwd_rel) if cwd_rel else WORKSPACE
    except PermissionError as exc:
        return err(str(exc), 403)
    if not cwd.is_dir():
        cwd = WORKSPACE
    log_path = PROCESS_LOG_DIR / f"{name}.log"
    log_file = open(log_path, "ab")
    try:
        popen = subprocess.Popen(
            command, shell=True, cwd=str(cwd),
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        log_file.close()
        return err(f"failed to start: {exc}", 500)
    PROCESSES[name] = {
        "popen": popen, "command": command, "started_at": time.time(),
        "log_path": log_path, "log_file": log_file,
        "cwd": "" if cwd == WORKSPACE else str(cwd.relative_to(WORKSPACE)),
    }
    return jsonify({
        "name": name, "pid": popen.pid,
        "log_path": str(log_path.relative_to(WORKSPACE)),
    })


@app.route("/processes/stop", methods=["POST"])
def stop_process():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "")
    info = PROCESSES.get(name)
    if not info:
        return err("not found", 404)
    popen = info["popen"]
    if popen.poll() is None:
        try:
            os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            popen.wait(timeout=5)
    try:
        info["log_file"].close()
    except Exception:
        pass
    info["exit_code"] = popen.returncode
    return jsonify({"name": name, "stopped": True, "exit_code": popen.returncode})


@app.route("/processes/logs", methods=["GET"])
def process_logs():
    name = request.args.get("name", "")
    info = PROCESSES.get(name)
    if not info:
        return err("not found", 404)
    log_path = info["log_path"]
    if not log_path.exists():
        return jsonify({"name": name, "log": "", "size": 0})
    size = log_path.stat().st_size
    with open(log_path, "rb") as f:
        if size > 16 * 1024:
            f.seek(size - 16 * 1024)
        log = f.read().decode(errors="replace")
    return jsonify({"name": name, "log": log, "size": size})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
