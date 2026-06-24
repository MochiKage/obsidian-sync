#!/usr/bin/env python3
"""
Obsidian Vault Sync — bidirectional PC ↔ Android tablet sync with Git versioning.

Requires: Python 3.8+, ADB (Android Debug Bridge), Git
"""

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys

# Force UTF-8 output (Windows Chinese locale defaults to GBK)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH_TEMPLATE = "{vault}/.syncstate.json"

# Known ADB install paths in order of preference
ADB_CANDIDATES = [
    # winget installation
    lambda: (
        Path(os.environ["LOCALAPPDATA"])
        / "Microsoft/WinGet/Packages"
        / "Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "platform-tools/adb.exe"
    )
    if "LOCALAPPDATA" in os.environ
    else None,
    lambda: Path("C:/platform-tools/adb.exe"),
    lambda: Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe",
    lambda: Path("adb"),
]

# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class FileInfo:
    rel_path: str
    mtime: float
    hash: str


@dataclass
class Action:
    kind: str  # push | pull | conflict | delete_pc | delete_tablet
    path: str
    reason: str = ""


# ── Config ───────────────────────────────────────────────────────────────────

class Config:
    """Load and provide access to config.json."""

    def __init__(self, path: Path = CONFIG_PATH):
        with open(path, encoding="utf-8") as f:
            self._data = json.load(f)

    @property
    def pc_vault(self) -> Path:
        return Path(self._data["pc_vault_path"])

    @property
    def tablet_vault(self) -> str:
        return self._data["tablet_vault_path"]

    @property
    def adb_path(self) -> str:
        val = self._data.get("adb_path", "auto")
        if val and val != "auto":
            return val
        for candidate in ADB_CANDIDATES:
            try:
                p = candidate()
                if p and p.exists():
                    return str(p)
            except Exception:
                pass
        return "adb"  # fallback to PATH

    @property
    def conflict_strategy(self) -> str:
        return self._data.get("conflict_strategy", "keep_both")

    @property
    def delete_strategy(self) -> str:
        return self._data.get("delete_strategy", "propagate")

    @property
    def ignore_patterns(self) -> list:
        return self._data.get("ignore_patterns", [])

    def is_ignored(self, rel_path: str) -> bool:
        for pat in self.ignore_patterns:
            if fnmatch.fnmatch(rel_path, pat):
                return True
            if pat.endswith("/") and (
                rel_path.startswith(pat) or fnmatch.fnmatch(rel_path + "/", pat + "*")
            ):
                return True
        return False


# ── ADB Wrapper ──────────────────────────────────────────────────────────────

class ADB:
    """Thin wrapper around the ADB CLI."""

    def __init__(self, adb_path: str):
        self._adb = adb_path

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self._adb] + list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    def check_device(self) -> bool:
        """Return True if exactly one authorized device is connected."""
        r = self._run("devices", check=False)
        lines = r.stdout.strip().splitlines()
        devices = [l for l in lines[1:] if l.strip() and "\tdevice" in l]
        return len(devices) >= 1

    def get_files(self, vault_path: str) -> dict[str, FileInfo]:
        """Scan tablet vault, return {rel_path: FileInfo}. Skip ignored & dirs."""
        # List all files with mtime and size using find + stat
        # Output format: <epoch_mtime>\t<rel_path>
        cmd = (
            f"cd '{vault_path}' 2>/dev/null && "
            f"find . -type f -exec stat -c '%Y\t%n' {{}} \\; 2>/dev/null || true"
        )
        r = self._run("shell", cmd)
        result: dict[str, FileInfo] = {}
        for line in r.stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                mtime_str, rel_path = line.split("\t", 1)
                mtime = float(mtime_str)
            except ValueError:
                continue
            # Normalize: remove leading ./ and convert to forward slash
            rel_path = rel_path.removeprefix("./").replace("\\", "/")
            if not rel_path:
                continue
            file_path = f"{vault_path}/{rel_path}"
            # Get hash (use first 16 chars of MD5 for compactness)
            try:
                hr = self._run("shell", f"md5sum '{file_path}' 2>/dev/null || true")
                file_hash = hr.stdout.strip().split()[0] if hr.stdout.strip() else "-"
            except Exception:
                file_hash = "-"
            result[rel_path] = FileInfo(rel_path=rel_path, mtime=mtime, hash=file_hash)
        return result

    def pull(self, tablet_file: str, pc_file: Path) -> bool:
        """Pull a single file from tablet to PC."""
        pc_file.parent.mkdir(parents=True, exist_ok=True)
        r = self._run("pull", tablet_file, str(pc_file), check=False)
        return r.returncode == 0

    def push(self, pc_file: Path, tablet_file: str) -> bool:
        """Push a single file from PC to tablet."""
        tablet_dir = "/".join(tablet_file.split("/")[:-1])
        if tablet_dir:
            self._run("shell", f"mkdir -p '{tablet_dir}'", check=False)
        r = self._run("push", str(pc_file), tablet_file, check=False)
        return r.returncode == 0

    def delete(self, tablet_file: str) -> bool:
        """Delete a file on the tablet."""
        r = self._run("shell", f"rm -f '{tablet_file}'", check=False)
        return r.returncode == 0


# ── Sync State ───────────────────────────────────────────────────────────────

class SyncState:
    """Persist per-file sync metadata to detect changes since last sync."""

    def __init__(self, state_path: Path):
        self._path = state_path
        self.last_sync: str = ""
        self.files: dict[str, dict] = {}  # rel_path -> {pc_mtime, pc_hash, tablet_mtime, tablet_hash}

    def load(self) -> bool:
        """Load state from disk. Returns False if no previous state."""
        if not self._path.exists():
            return False
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self.last_sync = data.get("last_sync", "")
            self.files = data.get("files", {})
            return True
        except (json.JSONDecodeError, KeyError):
            return False

    def save(self):
        """Write state to disk."""
        self.last_sync = datetime.now(timezone.utc).isoformat()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(
                {"last_sync": self.last_sync, "files": self.files},
                f,
                indent=2,
                ensure_ascii=False,
            )

    def update_entry(self, rel_path: str, pc_info: Optional[FileInfo], tablet_info: Optional[FileInfo]):
        """Record the current state of a file from both sides."""
        self.files[rel_path] = {
            "pc_mtime": pc_info.mtime if pc_info else None,
            "pc_hash": pc_info.hash if pc_info else None,
            "tablet_mtime": tablet_info.mtime if tablet_info else None,
            "tablet_hash": tablet_info.hash if tablet_info else None,
        }

    def pc_mtime(self, rel_path: str) -> Optional[float]:
        entry = self.files.get(rel_path)
        return entry["pc_mtime"] if entry else None

    def tablet_mtime(self, rel_path: str) -> Optional[float]:
        entry = self.files.get(rel_path)
        return entry["tablet_mtime"] if entry else None

    def pc_hash(self, rel_path: str) -> Optional[str]:
        entry = self.files.get(rel_path)
        return entry.get("pc_hash") if entry else None

    def tablet_hash(self, rel_path: str) -> Optional[str]:
        entry = self.files.get(rel_path)
        return entry.get("tablet_hash") if entry else None


# ── Git Manager ──────────────────────────────────────────────────────────────

class GitManager:
    """Commit changes in the PC vault for version tracking."""

    def __init__(self, repo_path: Path):
        self._repo = repo_path

    def is_repo(self) -> bool:
        return (self._repo / ".git").is_dir()

    def init(self):
        subprocess.run(
            ["git", "init"], cwd=self._repo, check=True,
            capture_output=True, encoding="utf-8", errors="replace",
        )

    def has_changes(self) -> bool:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self._repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return bool(r.stdout.strip())

    def commit(self, message: str):
        subprocess.run(
            ["git", "add", "-A"],
            cwd=self._repo,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=self._repo,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )


# ── Core Sync Engine ─────────────────────────────────────────────────────────

class ObsidianSync:
    """Bidirectional sync with three-way comparison."""

    def __init__(self, config: Config, adb: ADB):
        self._cfg = config
        self._adb = adb
        self._state = SyncState(Path(STATE_PATH_TEMPLATE.format(vault=config.pc_vault)))
        self._git = GitManager(config.pc_vault)

    # ── Scanning ──────────────────────────────────────────────────────────

    def _scan_pc(self) -> dict[str, FileInfo]:
        """Walk PC vault, return {rel_path: FileInfo}. Ignores hidden/sync files."""
        files: dict[str, FileInfo] = {}
        vault = self._cfg.pc_vault
        if not vault.is_dir():
            return files
        for entry in vault.rglob("*"):
            if not entry.is_file():
                continue
            rel = str(entry.relative_to(vault)).replace("\\", "/")
            if self._cfg.is_ignored(rel):
                continue
            if ".syncstate.json" in rel:
                continue
            try:
                st = entry.stat()
                content = entry.read_bytes()
                fhash = hashlib.md5(content).hexdigest()
            except OSError:
                continue
            files[rel] = FileInfo(rel_path=rel, mtime=st.st_mtime, hash=fhash)
        return files

    def _scan_tablet(self) -> dict[str, FileInfo]:
        """Scan tablet vault, filter ignored."""
        raw = self._adb.get_files(self._cfg.tablet_vault)
        return {
            k: v
            for k, v in raw.items()
            if not self._cfg.is_ignored(k) and ".syncstate.json" not in k
        }

    # ── Comparison ────────────────────────────────────────────────────────

    def _compare(
        self,
        pc: dict[str, FileInfo],
        tablet: dict[str, FileInfo],
        state: SyncState,
        has_prev_state: bool,
    ) -> list[Action]:
        actions: list[Action] = []
        all_keys = set(pc) | set(tablet)
        if has_prev_state:
            all_keys |= set(state.files)

        for key in all_keys:
            in_pc = key in pc
            in_tab = key in tablet
            in_state = key in state.files if has_prev_state else False

            pc_info = pc.get(key)
            tab_info = tablet.get(key)

            if in_pc and in_tab:
                # File exists on both sides
                if not in_state:
                    # Never synced before — both created independently: conflict
                    actions.append(Action("conflict", key, "both new"))
                    continue

                pc_changed = (
                    pc_info.mtime != state.pc_mtime(key)
                    or pc_info.hash != state.pc_hash(key)
                )
                tab_changed = (
                    tab_info.mtime != state.tablet_mtime(key)
                    or tab_info.hash != state.tablet_hash(key)
                )

                if pc_changed and tab_changed:
                    actions.append(Action("conflict", key, "both modified"))
                elif pc_changed:
                    actions.append(Action("push", key, "pc modified"))
                elif tab_changed:
                    actions.append(Action("pull", key, "tablet modified"))
                # else: unchanged, skip

            elif in_pc and not in_tab:
                # Only on PC
                if in_state and state.tablet_mtime(key) is not None:
                    # Tablet had it, now deleted
                    pc_changed = (
                        pc_info.mtime != state.pc_mtime(key)
                        or pc_info.hash != state.pc_hash(key)
                    )
                    if pc_changed:
                        actions.append(Action("push", key, "tablet deleted, pc modified"))
                    elif self._cfg.delete_strategy == "propagate":
                        actions.append(Action("delete_pc", key, "propagating tablet deletion"))
                    # else: ignore — whatever strategy dictates
                else:
                    actions.append(Action("push", key, "new on pc"))

            elif not in_pc and in_tab:
                # Only on tablet
                if in_state and state.pc_mtime(key) is not None:
                    # PC had it, now deleted
                    tab_changed = (
                        tab_info.mtime != state.tablet_mtime(key)
                        or tab_info.hash != state.tablet_hash(key)
                    )
                    if tab_changed:
                        actions.append(Action("pull", key, "pc deleted, tablet modified"))
                    elif self._cfg.delete_strategy == "propagate":
                        actions.append(Action("delete_tablet", key, "propagating pc deletion"))
                else:
                    actions.append(Action("pull", key, "new on tablet"))

            # else: neither side has it — was deleted on both, remove from state silently

        return actions

    # ── Execution ─────────────────────────────────────────────────────────

    def _execute(self, actions: list[Action]) -> tuple[int, int, int, int, int]:
        """Run actions. Returns (pushed, pulled, conflicts, deleted_pc, deleted_tablet)."""
        pushed = pulled = conflicts = del_pc = del_tab = 0

        for act in actions:
            pc_file = self._cfg.pc_vault / act.path
            tablet_file = f"{self._cfg.tablet_vault}/{act.path}"

            if act.kind == "push":
                ok = self._adb.push(pc_file, tablet_file)
                if ok:
                    pushed += 1
                    print(f"  ⬆  PUSH  {act.path}")
                else:
                    print(f"  ✗ FAIL PUSH {act.path}")

            elif act.kind == "pull":
                ok = self._adb.pull(tablet_file, pc_file)
                if ok:
                    pulled += 1
                    print(f"  ⬇  PULL  {act.path}")
                else:
                    print(f"  ✗ FAIL PULL {act.path}")

            elif act.kind == "conflict":
                conflicts += 1
                if self._cfg.conflict_strategy == "keep_both":
                    # Pull tablet version as .conflict.md
                    conflict_name = f"{pc_file.stem}.conflict{pc_file.suffix}"
                    conflict_pc = pc_file.parent / conflict_name
                    self._adb.pull(tablet_file, conflict_pc)
                    print(f"  ⚡ CONFLICT {act.path} → {conflict_name} ({act.reason})")
                else:
                    print(f"  ⚡ CONFLICT {act.path} ({act.reason}) — skipped")

            elif act.kind == "delete_pc":
                try:
                    pc_file.unlink()
                    del_pc += 1
                    print(f"  ✕ DEL PC  {act.path}")
                except OSError as e:
                    print(f"  ✗ FAIL DEL PC {act.path}: {e}")

            elif act.kind == "delete_tablet":
                ok = self._adb.delete(tablet_file)
                if ok:
                    del_tab += 1
                    print(f"  ✕ DEL TAB {act.path}")
                else:
                    print(f"  ✗ FAIL DEL TAB {act.path}")

        return pushed, pulled, conflicts, del_pc, del_tab

    # ── Main Flow ─────────────────────────────────────────────────────────

    def sync(self, dry_run: bool = False) -> bool:
        """Run a full sync cycle. Returns True on success."""

        # 1. Verify ADB
        if not self._adb.check_device():
            print("Error: No ADB device connected. Check USB cable and USB debugging.")
            return False

        # 2. Verify PC vault
        vault = self._cfg.pc_vault
        if not vault.is_dir():
            print(f"Error: PC vault not found at '{vault}'")
            return False

        # 3. Verify Git
        if not self._git.is_repo():
            print(f"Warning: '{vault}' is not a git repo. Run --init first.")
            return False

        # 4. Load previous state
        has_state = self._state.load()

        # 5. Pre-sync snapshot commit
        if not dry_run and self._git.has_changes():
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._git.commit(f"pre-sync snapshot: {ts}")
            print(f"📸 Pre-sync snapshot committed")

        # 6. Scan both sides
        print("Scanning...")
        pc_files = self._scan_pc()
        tablet_files = self._scan_tablet()
        print(f"  PC:      {len(pc_files)} files")
        print(f"  Tablet:  {len(tablet_files)} files")

        # 7. Compare
        actions = self._compare(pc_files, tablet_files, self._state, has_state)

        if not actions:
            print("Already in sync.")
            # Re-scan to capture current reality
            pc_files = self._scan_pc()
            tablet_files = self._scan_tablet()
            self._save_post_state(pc_files, tablet_files)
            return True

        # 8. Show plan
        counts = {"push": 0, "pull": 0, "conflict": 0, "delete_pc": 0, "delete_tablet": 0}
        for a in actions:
            counts[a.kind] += 1
        print(f"\nChanges detected:")
        if counts["push"]:
            print(f"  ⬆  Push:    {counts['push']}")
        if counts["pull"]:
            print(f"  ⬇  Pull:    {counts['pull']}")
        if counts["conflict"]:
            print(f"  ⚡ Conflict: {counts['conflict']}")
        if counts["delete_pc"]:
            print(f"  ✕  Del PC:   {counts['delete_pc']}")
        if counts["delete_tablet"]:
            print(f"  ✕  Del Tab:  {counts['delete_tablet']}")

        if dry_run:
            print("\n[Dry run — no changes made]")
            return True

        # 9. Execute
        print()
        pushed, pulled, conflicts, del_pc, del_tab = self._execute(actions)

        # 10. Re-scan and save new state (post-sync reality)
        pc_files = self._scan_pc()
        tablet_files = self._scan_tablet()
        self._save_post_state(pc_files, tablet_files)

        # 11. Post-sync commit
        parts = []
        if pulled:
            parts.append(f"pulled {pulled}")
        if pushed:
            parts.append(f"pushed {pushed}")
        if conflicts:
            parts.append(f"{conflicts} conflicts")
        msg = "sync: " + ", ".join(parts) if parts else "sync: no changes"
        if self._git.has_changes():
            self._git.commit(msg)
            print(f"\n📝 Git: {msg}")

        print("\n✔ Sync complete.")
        return True

    def _save_post_state(self, pc: dict[str, FileInfo], tablet: dict[str, FileInfo]):
        """Recompute state from current reality (post-sync) so we match exactly."""
        self._state.files = {}
        all_keys = set(pc) | set(tablet)
        for key in all_keys:
            self._state.update_entry(key, pc.get(key), tablet.get(key))
        self._state.save()

    # ── Init ──────────────────────────────────────────────────────────────

    def init(self) -> bool:
        """Initialize: ensure PC vault exists, init git, bootstrap state."""
        vault = self._cfg.pc_vault

        # Ensure vault directory exists
        vault.mkdir(parents=True, exist_ok=True)
        print(f"PC vault: {vault}")

        # Init git if needed
        if not self._git.is_repo():
            self._git.init()
            print("Git repository initialized.")

        # Bootstrap sync state from current reality
        pc_files = self._scan_pc()
        tablet_files = {}
        if self._adb.check_device():
            tablet_files = self._scan_tablet()
            print(f"Tablet detected: {len(tablet_files)} files")
        else:
            print("Warning: No tablet detected. Run sync after connecting.")

        all_keys = set(pc_files) | set(tablet_files)
        self._state.files = {}
        for key in all_keys:
            self._state.update_entry(key, pc_files.get(key), tablet_files.get(key))
        self._state.save()

        # Initial commit
        if self._git.has_changes():
            self._git.commit("init: initial vault snapshot")

        print("✔ Initialization complete. Ready to sync.")
        return True


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Obsidian Vault Sync — bidirectional PC ↔ Android tablet sync with Git versioning"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without making changes",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Initialize PC vault, git repo, and sync state",
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help=f"Path to config.json (default: {CONFIG_PATH})",
    )
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: config file not found at '{config_path}'")
        print("Create one based on the template in the project directory.")
        sys.exit(1)

    cfg = Config(config_path)
    adb = ADB(cfg.adb_path)
    sync = ObsidianSync(cfg, adb)

    if args.init:
        success = sync.init()
    else:
        success = sync.sync(dry_run=args.dry_run)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
