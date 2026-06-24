#!/usr/bin/env python3
"""
Obsidian Sync Dashboard — Web UI for the sync engine.
Start with: python dashboard.py
Then visit: http://localhost:8820
"""

import contextlib
import io
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# Reuse the existing sync engine
from sync import Config, ADB, ObsidianSync, GitManager, SyncState, STATE_PATH_TEMPLATE

app = Flask(__name__)
app.secret_key = "obsidian-sync-dashboard"

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
            tab_count = -1  # error

    # Determine sync status by running a dry comparison
    sync_state = SyncState(Path(STATE_PATH_TEMPLATE.format(vault=state.vault)))
    has_state = sync_state.load()

    sync_status = "unknown"
    if tab_available and has_state:
        pc_files = state.sync_engine._scan_pc()
        tablet_files = state.sync_engine._scan_tablet()
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

    pc_files = state.sync_engine._scan_pc()
    tablet_files = {}
    if _device_available():
        try:
            tablet_files = state.sync_engine._scan_tablet()
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

@app.route("/api/sync", methods=["POST"])
def api_sync():
    dry_run = request.args.get("dry_run") == "1"
    job_id = logger.start(dry_run=dry_run)

    # Capture all print() output from the sync engine so we can show it in the UI
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            success = state.sync_engine.sync(dry_run=dry_run)
        output = buf.getvalue()
        logger.write(output)
        logger.finish(success)
        return jsonify({"ok": success, "job_id": job_id, "output": output})
    except Exception as e:
        err_output = buf.getvalue()
        logger.write(err_output)
        logger.write(f"\n[ERROR] {e}\n")
        logger.finish(False)
        return jsonify({"ok": False, "error": str(e), "job_id": job_id}), 500


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

    issues = []

    # 1. Find orphaned attachments (non-md files not referenced by any md)
    md_files: list[Path] = []
    all_refs: set[str] = set()
    non_md_files: list[Path] = []

    for f in state.vault.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(state.vault))
        if ".git/" in rel or ".syncstate.json" in rel:
            continue
        if f.suffix.lower() == ".md":
            md_files.append(f)
            try:
                content = f.read_text(encoding="utf-8")
                # Find wikilinks [[xxx]]
                for m in re.finditer(r"\[\[([^\]|#]+)", content):
                    all_refs.add(m.group(1).strip())
                # Find markdown links [text](path)
                for m in re.finditer(r"\]\(([^)]+)\)", content):
                    ref = m.group(1).strip()
                    if not ref.startswith("http"):
                        all_refs.add(ref)
                # Find embedded files ![[xxx]]
                for m in re.finditer(r"!\[\[([^\]|#]+)", content):
                    all_refs.add(m.group(1).strip())
            except Exception:
                pass
        else:
            non_md_files.append(f)

    # Check which non-md files are orphaned
    md_names = {f.stem for f in md_files}
    md_paths = {str(f.relative_to(state.vault)).replace("\\", "/") for f in md_files}
    # Also map stems to paths for loose matching
    stem_to_paths: dict[str, list[str]] = {}
    for f in md_files:
        stem = f.stem
        rel = str(f.relative_to(state.vault)).replace("\\", "/")
        stem_to_paths.setdefault(stem, []).append(rel)

    orphans = []
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
            # Check if it's a common Obsidian config file
            if (not rel.startswith(".obsidian/") and not rel.startswith(".trash/")
                    and not rel.startswith(".git/") and rel != ".gitignore"):
                orphans.append({
                    "path": rel,
                    "size": f.stat().st_size,
                })

    if orphans:
        issues.append({
            "type": "orphaned_attachments",
            "label": f"{len(orphans)} orphaned attachments",
            "severity": "warning",
            "files": orphans[:20],  # cap at 20
        })

    # 2. Find broken wikilinks
    broken_links: list[dict] = []
    for f in md_files:
        try:
            content = f.read_text(encoding="utf-8")
            for m in re.finditer(r"\[\[([^\]|#]+)", content):
                target = m.group(1).strip()
                # Check if target exists as a file
                target_clean = target.split("/")[-1] if "/" in target else target
                found = (
                    target in md_paths
                    or target + ".md" in md_paths
                    or target_clean in md_names
                    or target_clean + ".md" in md_names
                )
                if not found:
                    broken_links.append({
                        "source": str(f.relative_to(state.vault)).replace("\\", "/"),
                        "target": target,
                    })
        except Exception:
            pass

    if broken_links:
        issues.append({
            "type": "broken_links",
            "label": f"{len(broken_links)} broken wikilinks",
            "severity": "error",
            "links": broken_links[:20],
        })

    # 3. Large files (>5MB)
    large_files = []
    for f in state.vault.rglob("*"):
        if not f.is_file():
            continue
        size = f.stat().st_size
        if size > 5 * 1024 * 1024:
            rel = str(f.relative_to(state.vault)).replace("\\", "/")
            if ".git/" not in rel:
                large_files.append({"path": rel, "size": size})

    if large_files:
        issues.append({
            "type": "large_files",
            "label": f"{len(large_files)} large files (>5MB)",
            "severity": "warning",
            "files": large_files,
        })

    return jsonify({
        "healthy": len([i for i in issues if i["severity"] == "error"]) == 0,
        "issues": issues,
    })


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

    entries = []
    try:
        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.name.startswith("$") or item.name.startswith("."):
                continue
            if item.is_dir():
                entries.append({
                    "path": str(item).replace("\\", "/"),
                    "name": item.name,
                    "type": "dir",
                })
    except PermissionError:
        pass

    # Add parent navigation
    parent = str(p.parent).replace("\\", "/") if p.parent != p else ""

    return jsonify({
        "path": base.replace("\\", "/"),
        "parent": parent,
        "entries": entries,
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
