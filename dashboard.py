#!/usr/bin/env python3
"""
Obsidian Sync Dashboard — Web UI for the sync engine.
Start with: python dashboard.py
Then visit: http://localhost:8820
"""

import contextlib
import io
import json
import os
import re
import subprocess
import threading
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# Reuse the existing sync engine
from sync import Config, ADB, ObsidianSync, GitManager, SyncState, STATE_PATH_TEMPLATE

app = Flask(__name__)
app.secret_key = "obsidian-sync-dashboard"


@app.errorhandler(Exception)
def handle_unhandled_error(e):
    """Catch any unhandled Flask exception and persist to error log."""
    log_error(f"Unhandled Flask error: {e}", exc_info=__import__("sys").exc_info())
    return jsonify({"ok": False, "error": str(e), "status": "error"}), 500

# ── Global Error Logger ────────────────────────────────────────────────────────

ERROR_LOG_PATH = Path(__file__).resolve().parent / ".sync_errors.log"
_error_log_lock = threading.Lock()


def log_error(message, exc_info=None):
    """Append an error entry to .sync_errors.log with timestamp and traceback."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {message}\n"
    if exc_info:
        entry += "".join(traceback.format_exception(*exc_info)) + "\n"
    else:
        entry += f"  (no traceback)\n"
    entry += "-" * 60 + "\n"

    with _error_log_lock:
        try:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(entry)
        except OSError:
            pass  # can't log — nothing we can do

    # Also print to stderr so it appears in the server console
    print(entry, file=__import__("sys").stderr, end="")

# ── Bootstrap ─────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"


class AppState:
    """Mutable global state — reloaded when the user switches vault folders."""

    def __init__(self):
        self.cfg: Config = Config(CONFIG_PATH)
        self.adb: ADB = ADB(self.cfg.adb_path)
        self.sync_engine: ObsidianSync = ObsidianSync(self.cfg, self.adb)
        self.vault: Path = self.cfg.pc_vault
        self.git: GitManager = GitManager(self.vault)

    def reload(self, new_vault_path: str | None = None):
        """Reload config and reinitialize all components.

        If new_vault_path is provided, it is written to config.json first.
        """
        if new_vault_path is not None:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            raw["pc_vault_path"] = new_vault_path
            CONFIG_PATH.write_text(
                json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        self.cfg = Config(CONFIG_PATH)
        self.adb = ADB(self.cfg.adb_path)
        self.sync_engine = ObsidianSync(self.cfg, self.adb)
        self.vault = self.cfg.pc_vault
        self.git = GitManager(self.vault)


state = AppState()


# ── Scan Cache ─────────────────────────────────────────────────────────────────
# Page loads trigger multiple API calls (status, files, health) simultaneously.
# Without caching, each call triggers _scan_pc() which walks the entire vault.
# A short-lived cache avoids redundant I/O within a single page-load burst.

_scan_cache: dict[str, tuple[float, dict]] = {}  # key -> (timestamp, result)
_CACHE_TTL = 30.0  # seconds — 页面内多次请求共享扫描结果


def _cached_scan_pc(compute_hash: bool = False):
    """Call _scan_pc with caching. Dashboard never needs MD5 hashes."""
    cache_key = "pc_scan"
    now = datetime.now().timestamp()
    if cache_key in _scan_cache:
        ts, data = _scan_cache[cache_key]
        if now - ts < _CACHE_TTL:
            return data
    data = state.sync_engine._scan_pc(compute_hash=compute_hash)
    _scan_cache[cache_key] = (now, data)
    return data


def _cached_scan_tablet():
    cache_key = "tablet_scan"
    now = datetime.now().timestamp()
    if cache_key in _scan_cache:
        ts, data = _scan_cache[cache_key]
        if now - ts < _CACHE_TTL:
            return data
    data = state.sync_engine._scan_tablet()
    _scan_cache[cache_key] = (now, data)
    return data


def _clear_scan_cache():
    _scan_cache.clear()


# ── Staging Area ───────────────────────────────────────────────────────────────
# Files selected by the user for the next sync. When non-empty, only staged
# files are synced. After a successful sync, the staging area is cleared.

_staged_files: set[str] = set()

# ── Background Sync Jobs ────────────────────────────────────────────────────────
# Sync can take a long time (scanning tablet files via ADB, computing MD5 hashes).
# Running it synchronously in a Flask request handler blocks the entire server and
# causes the HTTP request to time out. Instead, we spawn a background thread and
# let the frontend poll for completion.

_sync_jobs: dict[str, dict] = {}  # job_id -> {status, output, ok, ...}
_sync_lock = threading.Lock()


# ── Sync Logger ────────────────────────────────────────────────────────────────

class SyncLogger:
    """Capture and persist sync operation output so users can review what happened."""

    def __init__(self):
        self._max = 100
        self._current: dict | None = None

    @property
    def _path(self) -> Path:
        return state.vault / ".sync_log.json"

    def start(self, dry_run: bool = False) -> str:
        jid = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
        self._current = {
            "id": jid,
            "start": datetime.now().isoformat(),
            "dry_run": dry_run,
            "output": "",
            "status": "running",
        }
        return jid

    def write(self, text: str):
        if self._current is not None:
            self._current["output"] += text

    def finish(self, success: bool = True):
        if self._current is None:
            return
        self._current["end"] = datetime.now().isoformat()
        self._current["status"] = "success" if success else "failed"
        logs = self._load()
        logs.insert(0, self._current)
        self._current = None
        self._save(logs[: self._max])

    def get_all(self, limit: int = 30) -> list[dict]:
        return self._load()[:limit]

    def get_one(self, job_id: str) -> dict | None:
        for entry in self._load():
            if entry["id"] == job_id:
                return entry
        return None

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self, data: list[dict]):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


logger = SyncLogger()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _device_available() -> bool:
    return state.adb.check_device()


def _run_git(*args: str) -> str:
    """Run a git command in the vault repo and return stdout."""
    r = subprocess.run(
        ["git"] + list(args),
        cwd=state.vault,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return r.stdout


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).isoformat()


# ── API: Status ───────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    # Fast file count without reading file contents
    pc_count = 0
    if state.vault.is_dir():
        pc_count = sum(1 for _ in state.vault.rglob("*") if _.is_file()
                       and ".git/" not in str(_)
                       and ".syncstate.json" not in str(_))

    tab_count = 0
    tab_available = False
    if _device_available():
        tab_available = True
        try:
            raw = state.adb.get_files(state.cfg.tablet_vault)
            tab_count = len([k for k in raw if not state.cfg.is_ignored(k)])
        except Exception:
            tab_count = -1

    # Use cached lightweight scans (no MD5) for status comparison
    sync_state = SyncState(Path(STATE_PATH_TEMPLATE.format(vault=state.vault)))
    has_state = sync_state.load()

    sync_status = "unknown"
    if tab_available and has_state:
        pc_files = _cached_scan_pc()
        tablet_files = _cached_scan_tablet()
        actions = state.sync_engine._compare(pc_files, tablet_files, sync_state, has_state)
        conflicts = sum(1 for a in actions if a.kind == "conflict")
        if conflicts > 0:
            sync_status = "conflict"
        elif len(actions) > 0:
            sync_status = "pending"
        else:
            sync_status = "synced"

    return jsonify({
        "pc_files": pc_count,
        "tablet_files": tab_count,
        "tablet_available": tab_available,
        "status": sync_status,
        "last_sync": sync_state.last_sync if has_state else None,
    })


# ── API: Files ────────────────────────────────────────────────────────────────

@app.route("/api/files")
def api_files():
    filter_type = request.args.get("filter", "all")

    pc_files = _cached_scan_pc()
    tablet_files = {}
    if _device_available():
        try:
            tablet_files = _cached_scan_tablet()
        except Exception:
            pass

    sync_state = SyncState(Path(STATE_PATH_TEMPLATE.format(vault=state.vault)))
    has_state = sync_state.load()
    actions = state.sync_engine._compare(pc_files, tablet_files, sync_state, has_state)

    action_map: dict[str, str] = {}
    for a in actions:
        action_map[a.path] = a.kind

    all_keys = set(pc_files) | set(tablet_files)
    if has_state:
        all_keys |= set(sync_state.files)

    files = []
    for key in sorted(all_keys):
        status = action_map.get(key, "synced")
        if filter_type != "all":
            if filter_type == "pending" and status in ("synced",):
                continue
            if filter_type == "conflict" and status != "conflict":
                continue

        pc_info = pc_files.get(key)
        tab_info = tablet_files.get(key)

        files.append({
            "path": key,
            "status": status,
            "pc_mtime": _iso(pc_info.mtime) if pc_info else None,
            "tablet_mtime": _iso(tab_info.mtime) if tab_info else None,
        })

    return jsonify({"files": files})


# ── API: Sync ─────────────────────────────────────────────────────────────────

def _run_sync_in_thread(job_id, dry_run, filter_paths, was_staged):
    """Execute sync in a background thread, updating _sync_jobs as it progresses.

    Uses prepare() → check deletes → (maybe wait for confirm) → execute() flow.
    """
    global _staged_files
    buf = io.StringIO()

    def _finish(ok, output, **extra):
        logger.write(output)
        logger.finish(ok)
        _clear_scan_cache()
        if was_staged and not dry_run:
            _staged_files.clear()
        with _sync_lock:
            _sync_jobs[job_id] = {
                "ok": ok, "job_id": job_id, "output": output,
                "staged_cleared": was_staged and not dry_run,
                "status": "done", **extra,
            }

    try:
        with contextlib.redirect_stdout(buf):
            # Step 1: prepare — scan + compare
            result = state.sync_engine.prepare(paths=filter_paths)
        if result is None:
            return _finish(False, buf.getvalue(), error="Prepare failed")

        actions, _pc, _tab, _st = result

        # Step 2: check for delete actions that need confirmation
        del_pc = [a for a in actions if a.kind == "delete_pc"]
        del_tab = [a for a in actions if a.kind == "delete_tablet"]
        all_dels = del_pc + del_tab

        if all_dels and not dry_run:
            # Pause and ask user to confirm
            del_info = []
            for a in del_pc:
                del_info.append({"path": a.path, "side": "pc", "reason": a.reason})
            for a in del_tab:
                del_info.append({"path": a.path, "side": "tablet", "reason": a.reason})

            confirm_event = threading.Event()
            confirm_decision = {"proceed": False, "push_instead": False}

            with _sync_lock:
                _sync_jobs[job_id] = {
                    "status": "confirm_delete",
                    "job_id": job_id,
                    "output": buf.getvalue(),
                    "deletes": del_info,
                    "_event": confirm_event,
                    "_decision": confirm_decision,
                }

            # Wait for user confirmation (max 5 minutes)
            confirmed = confirm_event.wait(timeout=300)

            if confirmed and confirm_decision["proceed"]:
                # User confirmed — execute all actions including deletes
                pass  # actions unchanged
            elif confirmed and confirm_decision["push_instead"]:
                # Convert delete_pc → push (re-push files to tablet)
                restored = 0
                for a in actions:
                    if a.kind == "delete_pc":
                        a.kind = "push"
                        a.reason = "re-push to tablet"
                        restored += 1
                actions = [a for a in actions if a.kind != "delete_tablet"]
                buf.write(f"\nℹ 已将 {restored} 个删除操作转为推送，文件将重新同步到平板。\n")
            else:
                # User skipped or timed out — remove delete actions
                actions = [a for a in actions if a.kind not in ("delete_pc", "delete_tablet")]
                if not confirmed:
                    buf.write("\n⚠ 删除确认超时，已跳过删除操作。\n")
                else:
                    buf.write(f"\nℹ 已跳过 {len(all_dels)} 个删除操作。\n")

        elif all_dels and dry_run:
            buf.write(f"\n⚠ 预览: 检测到 {len(all_dels)} 个删除操作，正式同步时需确认。\n")

        if not actions:
            buf.write("\n没有需要执行的操作。\n")
            return _finish(True, buf.getvalue())

        # Step 3: execute
        with contextlib.redirect_stdout(buf):
            state.sync_engine.execute(actions, dry_run=dry_run)

        return _finish(True, buf.getvalue())

    except Exception as e:
        err_output = buf.getvalue()
        log_error(f"Sync job {job_id} failed: {e}", exc_info=__import__("sys").exc_info())
        with _sync_lock:
            _sync_jobs[job_id] = {
                "ok": False, "error": str(e), "job_id": job_id,
                "output": err_output, "status": "error",
            }
        logger.write(err_output)
        logger.write(f"\n[ERROR] {e}\n")
        logger.finish(False)


@app.route("/api/sync/confirm/<job_id>", methods=["POST"])
def api_sync_confirm(job_id):
    """User confirms, rejects, or re-pushes pending delete operations."""
    data = request.get_json(silent=True) or {}
    action = data.get("action", "skip")  # "proceed" | "skip" | "push_instead"

    with _sync_lock:
        job = _sync_jobs.get(job_id)

    if job is None or job.get("status") != "confirm_delete":
        return jsonify({"ok": False, "error": "No pending confirmation for this job"}), 400

    event = job.get("_event")
    decision = job.get("_decision")
    if event is None or decision is None:
        return jsonify({"ok": False, "error": "Job in unexpected state"}), 500

    decision["proceed"] = (action == "proceed")
    decision["push_instead"] = (action == "push_instead")
    event.set()

    with _sync_lock:
        _sync_jobs[job_id]["status"] = "running"

    return jsonify({"ok": True, "action": action})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    global _staged_files
    dry_run = request.args.get("dry_run") == "1"
    body = request.get_json(silent=True) or {}
    paths = body.get("paths")
    filter_paths = set(paths) if paths else None
    is_staged = len(_staged_files) > 0

    job_id = logger.start(dry_run=dry_run)

    with _sync_lock:
        _sync_jobs[job_id] = {"status": "running", "job_id": job_id}

    # Spawn background thread — return immediately so the dashboard stays responsive
    thread = threading.Thread(
        target=_run_sync_in_thread,
        args=(job_id, dry_run, filter_paths, is_staged),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "job_id": job_id, "status": "started"})


@app.route("/api/sync/status/<job_id>")
def api_sync_status(job_id: str):
    """Poll for sync progress. Returns job state including output when complete."""
    with _sync_lock:
        job = _sync_jobs.get(job_id)
    if job is None:
        return jsonify({"status": "unknown", "error": "Job not found"}), 404
    # Strip internal objects that can't be JSON serialized
    safe = {k: v for k, v in job.items() if not k.startswith("_")}
    return jsonify(safe)


# ── API: Staging ──────────────────────────────────────────────────────────────

@app.route("/api/staged", methods=["GET", "POST", "DELETE"])
def api_staged():
    global _staged_files

    if request.method == "GET":
        return jsonify({"files": sorted(_staged_files), "count": len(_staged_files)})

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        paths = data.get("paths", [])
        _staged_files.update(paths)
        return jsonify({"ok": True, "count": len(_staged_files)})

    # DELETE
    data = request.get_json(silent=True) or {}
    paths = data.get("paths")
    if paths:
        _staged_files.difference_update(paths)
    else:
        _staged_files.clear()
    return jsonify({"ok": True, "count": len(_staged_files)})


# ── API: Logs ─────────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    limit = int(request.args.get("limit", 30))
    return jsonify({"logs": logger.get_all(limit)})


@app.route("/api/logs/<job_id>")
def api_log_detail(job_id: str):
    entry = logger.get_one(job_id)
    if entry is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(entry)


# ── API: History ──────────────────────────────────────────────────────────────

@app.route("/api/history")
def api_history():
    page = int(request.args.get("page", 0))
    per_page = int(request.args.get("per_page", 30))

    if not state.git.is_repo():
        return jsonify({"commits": [], "total": 0})

    # Get total count
    total_str = _run_git("rev-list", "--count", "HEAD").strip()
    total = int(total_str) if total_str.isdigit() else 0

    # Get paginated log with stats
    skip = page * per_page
    output = _run_git(
        "log",
        f"--skip={skip}",
        f"-{per_page}",
        "--format=__COMMIT__%n%H%n%ai%n%s%n%b",
        "--shortstat",
    )

    commits = []
    current = None
    for line in output.splitlines():
        if line == "__COMMIT__":
            if current:
                commits.append(current)
            current = {"hash": None, "date": None, "message": "", "stats": ""}
        elif current is not None:
            if current["hash"] is None:
                current["hash"] = line.strip()
            elif current["date"] is None:
                current["date"] = line.strip()
            elif current["message"] == "":
                current["message"] = line.strip()
            elif line.strip():
                current["stats"] = line.strip()

    if current:
        commits.append(current)

    return jsonify({"commits": commits, "total": total})


# ── API: Diff ─────────────────────────────────────────────────────────────────

@app.route("/api/diff/<commit_hash>")
def api_diff(commit_hash: str):
    if not state.git.is_repo():
        return jsonify({"error": "Not a git repository"}), 400

    # Get commit info
    info = _run_git("log", "-1", "--format=%ai%n%s", commit_hash).strip().splitlines()
    commit_date = info[0] if len(info) > 0 else ""
    commit_msg = info[1] if len(info) > 1 else ""

    # Get the diff (compare with parent)
    diff_text = _run_git("diff", f"{commit_hash}~1", commit_hash)

    # Get file list changed
    files_changed = _run_git(
        "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash
    ).strip().splitlines()

    return jsonify({
        "hash": commit_hash,
        "date": commit_date,
        "message": commit_msg,
        "files": [f for f in files_changed if f and ".syncstate.json" not in f],
        "diff": diff_text,
    })


# ── API: File Content ─────────────────────────────────────────────────────────

@app.route("/api/file/content")
def api_file_content():
    path = request.args.get("path", "")
    if not path:
        return jsonify({"error": "Missing path"}), 400

    file_path = state.vault / path
    if not file_path.exists():
        return jsonify({"error": "File not found"}), 404

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = f"[Binary file: {file_path.stat().st_size} bytes]"

    return jsonify({"path": path, "content": content})


# ── API: Conflict Resolution ─────────────────────────────────────────────────

@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    data = request.get_json()
    path = data.get("path")
    action = data.get("action")  # "keep_pc" or "keep_tablet"

    if not path or action not in ("keep_pc", "keep_tablet"):
        return jsonify({"ok": False, "error": "Invalid params"}), 400

    if action == "keep_tablet":
        # Overwrite PC file with tablet version
        tablet_file = f"{state.cfg.tablet_vault}/{path}"
        pc_file = state.vault / path
        ok = state.adb.pull(tablet_file, pc_file)
    else:
        # keep_pc: just remove the .conflict.md if it exists
        ok = True

    # Remove conflict marker file if exists
    conflict_file = state.vault / f"{Path(path).stem}.conflict{Path(path).suffix}"
    try:
        conflict_file.unlink()
    except OSError:
        pass

    return jsonify({"ok": ok})


# ── API: Search ───────────────────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"results": []})

    if not state.git.is_repo():
        return jsonify({"results": []})

    # git log -S searches for commits that changed the number of occurrences of a string
    output = _run_git(
        "log",
        "--oneline",
        "--all",
        f"-S{q}",
        "-30",
    )

    results = []
    for line in output.strip().splitlines():
        if line.strip():
            parts = line.split(" ", 1)
            results.append({
                "hash": parts[0],
                "message": parts[1] if len(parts) > 1 else "",
            })

    return jsonify({"results": results, "query": q})


# ── API: Health Check ─────────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    if not state.vault.is_dir():
        return jsonify({"error": "Vault not found"}), 404

    # Health check is expensive (reads every .md file). Cache aggressively.
    cache_key = "health"
    now = datetime.now().timestamp()
    if cache_key in _scan_cache:
        ts, data = _scan_cache[cache_key]
        if now - ts < 60.0:  # 1 minute cache
            return jsonify(data)

    issues = []
    md_files: list[Path] = []
    all_refs: set[str] = set()
    non_md_files: list[Path] = []
    large_files: list[dict] = []

    # Single pass: classify all files, collect large files
    for f in state.vault.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(state.vault))
        if ".git/" in rel or ".syncstate.json" in rel:
            continue

        size = f.stat().st_size
        if size > 5 * 1024 * 1024 and ".git/" not in rel:
            large_files.append({"path": rel.replace("\\", "/"), "size": size})

        if f.suffix.lower() == ".md":
            md_files.append(f)
        else:
            non_md_files.append(f)

    # Process .md files for wikilinks (file contents read once per file, released after)
    md_names: set[str] = set()
    md_paths: set[str] = set()
    stem_to_paths: dict[str, list[str]] = {}
    broken_links: list[dict] = []

    for f in md_files:
        rel = str(f.relative_to(state.vault)).replace("\\", "/")
        stem = f.stem
        md_names.add(stem)
        md_paths.add(rel)
        stem_to_paths.setdefault(stem, []).append(rel)

        try:
            content = f.read_text(encoding="utf-8")
            for m in re.finditer(r"\[\[([^\]|#]+)", content):
                target = m.group(1).strip()
                all_refs.add(target)
                target_clean = target.split("/")[-1] if "/" in target else target
                found = (
                    target in md_paths
                    or target + ".md" in md_paths
                    or target_clean in md_names
                    or target_clean + ".md" in md_names
                )
                if not found:
                    broken_links.append({"source": rel, "target": target})
            for m in re.finditer(r"\]\(([^)]+)\)", content):
                ref = m.group(1).strip()
                if not ref.startswith("http"):
                    all_refs.add(ref)
            for m in re.finditer(r"!\[\[([^\]|#]+)", content):
                all_refs.add(m.group(1).strip())
        except Exception:
            pass

    # Check orphaned attachments
    orphans = []
    md_paths_for_orphan = {str(f.relative_to(state.vault)).replace("\\", "/") for f in md_files}
    for f in non_md_files:
        rel = str(f.relative_to(state.vault)).replace("\\", "/")
        name = f.name
        stem = f.stem
        is_referenced = (
            rel in all_refs
            or name in all_refs
            or stem in all_refs
            or any(ref in rel for ref in all_refs)
        )
        if not is_referenced:
            if (not rel.startswith(".obsidian/") and not rel.startswith(".trash/")
                    and not rel.startswith(".git/") and rel != ".gitignore"):
                orphans.append({"path": rel, "size": f.stat().st_size})

    if orphans:
        issues.append({
            "type": "orphaned_attachments",
            "label": f"{len(orphans)} orphaned attachments",
            "severity": "warning",
            "files": orphans[:20],
        })

    if broken_links:
        issues.append({
            "type": "broken_links",
            "label": f"{len(broken_links)} broken wikilinks",
            "severity": "error",
            "links": broken_links[:20],
        })

    if large_files:
        issues.append({
            "type": "large_files",
            "label": f"{len(large_files)} large files (>5MB)",
            "severity": "warning",
            "files": large_files,
        })

    result = {
        "healthy": len([i for i in issues if i["severity"] == "error"]) == 0,
        "issues": issues,
    }
    _scan_cache[cache_key] = (now, result)
    return jsonify(result)


# ── API: Error Log ─────────────────────────────────────────────────────────────

@app.route("/api/errors")
def api_errors():
    """Return recent error log entries from .sync_errors.log."""
    limit = int(request.args.get("limit", 30))
    try:
        if not ERROR_LOG_PATH.exists():
            return jsonify({"errors": [], "count": 0})
        with open(ERROR_LOG_PATH, encoding="utf-8") as f:
            raw = f.read()
        # Parse entries: split by "---" separator
        entries = [e.strip() for e in raw.split("-" * 60) if e.strip()]
        entries = entries[-limit:]  # most recent last
        return jsonify({"errors": entries, "count": len(entries), "total": len(entries)})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


# ── API: Config ───────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.get_json()
        new_path = data.get("pc_vault_path", "").strip()
        if not new_path:
            return jsonify({"ok": False, "error": "Path is required"}), 400

        p = Path(new_path)
        if not p.is_absolute():
            return jsonify({"ok": False, "error": "Must be an absolute path"}), 400

        # Switch vault, reload all state
        old_vault = str(state.vault)
        state.reload(new_path)
        _clear_scan_cache()

        # Initialize the new vault
        p.mkdir(parents=True, exist_ok=True)
        if not state.git.is_repo():
            state.git.init()
        state.sync_engine.init()

        return jsonify({
            "ok": True,
            "pc_vault_path": str(state.vault),
            "previous_path": old_vault,
        })

    # GET: return current config
    return jsonify({
        "pc_vault_path": str(state.vault),
        "tablet_vault_path": state.cfg.tablet_vault,
        "pc_vault_exists": state.vault.is_dir(),
    })


# ── API: Browse ───────────────────────────────────────────────────────────────

@app.route("/api/browse")
def api_browse():
    """List subdirectories of a given path for folder navigation."""
    base = request.args.get("path", "").strip()

    # Resolve the path: if empty, list drives; otherwise list subdirs
    if not base:
        drives = []
        for letter in "CDEFGH":
            p = Path(f"{letter}:/")
            if p.exists():
                drives.append({"path": str(p), "name": f"{letter}:\\", "type": "drive"})
        return jsonify({"path": "", "entries": drives})

    p = Path(base)
    if not p.exists():
        return jsonify({"path": base, "entries": [], "error": "Path not found"})
    if not p.is_dir():
        return jsonify({"path": base, "entries": [], "error": "Not a directory"})

    # Use os.scandir() — DirEntry.is_dir() is free (comes from FindNextFile on Windows),
    # whereas Path.is_dir() triggers a separate stat() call per item, which is
    # catastrophically slow on drive roots with hundreds of entries.
    dirs = []
    try:
        with os.scandir(p) as it:
            for entry in it:
                if entry.name.startswith("$") or entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        dirs.append({
                            "path": entry.path.replace("\\", "/"),
                            "name": entry.name,
                            "type": "dir",
                        })
                except OSError:
                    pass  # skip entries we can't stat (e.g. broken junctions)
    except (PermissionError, OSError):
        pass

    dirs.sort(key=lambda d: d["name"].lower())

    # Add parent navigation (only if we're not at a drive root)
    parent_path = str(p.parent).replace("\\", "/")
    # A drive root like "C:/" has parent "C:" which means we should show "this PC"
    if parent_path.rstrip("/") == base.rstrip("/").split("/")[0]:
        parent_path = ""  # navigate back to drive list

    return jsonify({
        "path": base.replace("\\", "/"),
        "parent": parent_path if parent_path != base.replace("\\", "/") else "",
        "entries": dirs,
    })


# ── Main Page ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"PC vault: {state.vault}")
    print(f"Dashboard: http://localhost:8820")
    app.run(host="127.0.0.1", port=8820, debug=True)
