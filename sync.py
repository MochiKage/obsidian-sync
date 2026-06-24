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
import threading
import traceback

# Windows 中文版默认用 GBK 编码输出，但脚本内使用 emoji 字符（⬆⬇⚡ 等），
# GBK 无法编码这些字符会导致 UnicodeEncodeError。强制切换到 UTF-8。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
            pass

    # Also print to stderr
    print(entry, file=sys.stderr, end="")

# ── Constants ────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH_TEMPLATE = "{vault}/.syncstate.json"

# ADB 路径自动检测，按优先级依次尝试：
#   1. winget 安装的 Google PlatformTools（含具体版本目录）
#   2. C:\platform-tools\（常见手动安装路径）
#   3. Android SDK 默认路径
#   4. 系统 PATH 中的 adb（兜底）
# 每个候选是 lambda，只有前一个检测不到时才尝试下一个。
ADB_CANDIDATES = [
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
    """文件快照：记录路径、修改时间和 MD5 哈希。

    mtime 来自 stat 的 st_mtime（Unix 时间戳），PC 和平板可跨平台直接比较。
    hash 用于冲突检测的辅助判断——mtime 相同但 hash 不同 = 内容确实变了。
    """
    rel_path: str
    mtime: float
    hash: str


@dataclass
class Action:
    """同步操作单元，由 _compare 生成，_execute 消费。"""
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
        """Return ADB executable path, auto-detecting if not set in config.

        Priority: explicit config value → ADB_CANDIDATES probe → PATH fallback.
        """
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
        """检查文件是否匹配任一忽略模式。

        目录模式（以 / 结尾）：匹配该目录下的所有子孙文件。
        例如 ".trash/" 可同时匹配 ".trash/file.md" 和 ".trash/sub/x.md"。
        """
        for pat in self.ignore_patterns:
            if fnmatch.fnmatch(rel_path, pat):
                return True
            # 目录模式：路径以该前缀开头 或 路径+/ 匹配 模式+*
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

    def _run(self, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
        """执行 adb 命令，统一处理编码。

        必须指定 encoding='utf-8'：Windows 中文版 subprocess 默认用 GBK 解码，
        而 adb 输出（尤其是平板上的中文文件名）是 UTF-8，不指定会 UnicodeDecodeError。

        timeout 默认 30 秒，防止 ADB 命令无限卡死阻塞整个同步流程。
        """
        try:
            return subprocess.run(
                [self._adb] + list(args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=check,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # Return a failed-like result instead of crashing
            return subprocess.CompletedProcess(
                args=[self._adb] + list(args),
                returncode=-1,
                stdout="",
                stderr=f"ADB command timed out after {timeout}s",
            )

    def check_device(self, fast: bool = False) -> bool:
        """Return True if at least one authorized device is connected.

        fast=True: use a short timeout (5s) to quickly detect disconnection.
        """
        timeout = 5 if fast else 30
        r = self._run("devices", check=False, timeout=timeout)
        lines = r.stdout.strip().splitlines()
        devices = [l for l in lines[1:] if l.strip() and "\tdevice" in l]
        return len(devices) >= 1

    def get_files(self, vault_path: str) -> dict[str, FileInfo]:
        """扫描平板 vault，返回 {相对路径: FileInfo}。

        用 find + sh -c 一次拿到所有文件的 mtime 和 md5，只需 1 次 adb shell 调用。
        原来逐个 md5sum 需要 N+1 次 ADB 往返，这是最大的延迟来源。
        """
        # 单个 adb 命令：find 列出文件，while read 逐行处理 stat + md5sum
        # 输出格式：mtime\thash\trelpath
        # 这样只需 1 次 adb shell 调用，而非原来的 N+1 次
        shell_cmd = (
            "cd '%s' 2>/dev/null && "
            "find . -type f 2>/dev/null | while IFS= read -r f; do "
            "rel=\"${f#./}\"; "
            "mtime=$(stat -c %%Y \"$f\" 2>/dev/null || echo 0); "
            "hash=$(md5sum \"$f\" 2>/dev/null | { read h _; echo ${h:- };}); "
            "printf \"%%s\\t%%s\\t%%s\\n\" \"$mtime\" \"${hash:- }\" \"$rel\"; "
            "done || true"
        ) % vault_path
        r = self._run("shell", shell_cmd, timeout=120)
        result: dict[str, FileInfo] = {}
        for line in r.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            try:
                mtime = float(parts[0])
            except ValueError:
                mtime = 0.0
            file_hash = parts[1].strip() or "-"
            rel_path = parts[2].replace("\\", "/")
            if not rel_path:
                continue
            result[rel_path] = FileInfo(rel_path=rel_path, mtime=mtime, hash=file_hash)
        return result

    def pull(self, tablet_file: str, pc_file: Path) -> bool:
        """Pull a single file from tablet to PC."""
        pc_file.parent.mkdir(parents=True, exist_ok=True)
        r = self._run("pull", tablet_file, str(pc_file), check=False)
        return r.returncode == 0

    def push(self, pc_file: Path, tablet_file: str) -> bool:
        """推送单个文件到平板。

        adb push 不会自动创建目标目录，需要先 mkdir -p 确保父目录存在。
        """
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
        """Load state from disk. Returns False if no previous state.

        If .syncstate.json is corrupted (e.g., process killed mid-write on old
        non-atomic save), tries to recover from .syncstate.tmp.
        """
        if not self._path.exists():
            return False

        def _try_load(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                self.last_sync = data.get("last_sync", "")
                self.files = data.get("files", {})
                return True
            except (json.JSONDecodeError, KeyError, OSError):
                return False

        if _try_load(self._path):
            return True

        # Main file corrupted — try recover from tmp
        tmp_path = self._path.with_suffix(".tmp")
        if tmp_path.exists():
            print(f"Warning: {self._path.name} was corrupted. Recovering from .tmp backup.")
            if _try_load(tmp_path):
                # Restore the main file
                self.save()
                return True

        return False

    def save(self):
        """Write state to disk atomically.

        先写临时文件，成功后再 rename。这样即使写入中途进程被杀，
        .syncstate.json 也不会处于半写崩坏状态。
        """
        self.last_sync = datetime.now(timezone.utc).isoformat()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {"last_sync": self.last_sync, "files": self.files},
                f,
                indent=2,
                ensure_ascii=False,
            )
        tmp_path.replace(self._path)  # atomic rename on same filesystem

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
    """双向同步引擎，核心算法是三向比对（three-way comparison）。

    三向 = PC 当前状态 + 平板当前状态 + 上次同步后的记录状态。
    通过比较每侧当前 mtime/hash 与记录的 mtime/hash，判断谁变了。
    """

    def __init__(self, config: Config, adb: ADB):
        self._cfg = config
        self._adb = adb
        self._state = SyncState(Path(STATE_PATH_TEMPLATE.format(vault=config.pc_vault)))
        self._git = GitManager(config.pc_vault)

    # ── Scanning ──────────────────────────────────────────────────────────

    def _scan_pc(self, compute_hash: bool = True) -> dict[str, FileInfo]:
        """遍历 PC vault，返回所有非忽略文件的快照。

        compute_hash=True（默认）：对每个文件计算 MD5，用于冲突检测。
        compute_hash=False：跳过哈希，仅记录 mtime，大幅降低内存和磁盘 I/O。
        面板状态查询应使用 compute_hash=False，只有实际执行 sync 时才需要哈希。
        """
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
            if ".git/" in rel:
                continue
            try:
                st = entry.stat()
                if compute_hash:
                    fhash = hashlib.md5(entry.read_bytes()).hexdigest()
                else:
                    fhash = str(int(st.st_mtime))  # placeholder, not used for comparison
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
        """三向比对：PC 快照 vs 平板快照 vs 上次同步状态 → 操作列表。

        对每个文件路径，分四种场景：

        【两边都有】
          - 两边都没变 → SKIP
          - 只有 PC 变 → PUSH
          - 只有平板变 → PULL
          - 两边都变 → CONFLICT（保留两份）

        【只有 PC 有】
          - 状态记录显示平板曾有此文件 → 平板端被删了
            - PC 也改了 → 冲突，恢复推回平板
            - PC 没改 → propagate: 删 PC / ignore: 略过
          - 状态无记录 → 纯新增 → PUSH

        【只有平板有】
          - 状态记录显示 PC 曾有此文件 → PC 端被删了（同上逻辑镜像）

        【都没有，但状态有记录】→ 两边都删了，从状态中移除，不产生操作
        """
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

            # ── 场景1：两边都有 ──
            if in_pc and in_tab:
                if not in_state:
                    # 首次同步前两边独立创建了同名文件 → 冲突
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

            # ── 场景2：仅 PC 有 ──
            elif in_pc and not in_tab:
                if in_state and state.tablet_mtime(key) is not None:
                    pc_changed = (
                        pc_info.mtime != state.pc_mtime(key)
                        or pc_info.hash != state.pc_hash(key)
                    )
                    if pc_changed:
                        actions.append(Action("push", key, "tablet deleted, pc modified"))
                    elif self._cfg.delete_strategy == "propagate":
                        actions.append(Action("delete_pc", key, "propagating tablet deletion"))
                else:
                    actions.append(Action("push", key, "new on pc"))

            # ── 场景3：仅平板有 ──
            elif not in_pc and in_tab:
                if in_state and state.pc_mtime(key) is not None:
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

            # 都不存在：曾同步过的文件被两边都删了 → 静默从状态中移除

        # ── 安全阀：区分「用户主动删除」和「扫描不完整」 ──
        # 原理：如果平板剩余文件的 mtime 与基线匹配，说明扫描准确、缺失即真删除；
        # 如果连剩余文件都对不上基线，说明扫描有问题（目录错误/连接中断），阻止删除。
        DEL_SAFETY_MIN_MATCH_RATE = 0.7   # 剩余文件 mtime 匹配率低于此值 → 阻止
        DEL_SAFETY_MIN_COUNT = 5           # 删除数少于此 → 不触发检查（日常删几个无需验证）

        del_pc_actions = [a for a in actions if a.kind == "delete_pc"]
        del_tab_actions = [a for a in actions if a.kind == "delete_tablet"]

        if del_pc_actions and len(del_pc_actions) >= DEL_SAFETY_MIN_COUNT and has_prev_state:
            # 统计：平板当前存在的文件中，有多少 mtime 与基线一致
            matched = 0
            total_on_tablet = 0
            for key, tab_info in tablet.items():
                entry = state.files.get(key)
                if entry and entry.get("tablet_mtime") is not None:
                    total_on_tablet += 1
                    if abs(tab_info.mtime - entry["tablet_mtime"]) < 1.0:
                        matched += 1

            if total_on_tablet > 0:
                match_rate = matched / total_on_tablet
                if match_rate < DEL_SAFETY_MIN_MATCH_RATE:
                    # 剩余文件的 mtime 大面积不匹配 → 扫描可能有问题
                    names = ", ".join(a.path for a in del_pc_actions[:5])
                    if len(del_pc_actions) > 5:
                        names += f" ... 及其他 {len(del_pc_actions) - 5} 个文件"
                    print(
                        f"  ⚠ 安全阀触发：{len(del_pc_actions)} 个文件将被删除，"
                        f"但平板剩余文件 mtime 匹配率仅 {match_rate:.0%} "
                        f"（{matched}/{total_on_tablet}），可能是扫描不完整。已跳过删除。"
                    )
                    print(f"     跳过：{names}")
                    actions = [a for a in actions if a.kind != "delete_pc"]
                else:
                    # mtime 匹配率高 → 扫描准确，缺失是真删除
                    print(
                        f"  ℹ 平板剩余文件 mtime 匹配率 {match_rate:.0%}，"
                        f"确认 {len(del_pc_actions)} 个文件已被手动删除，将同步到 PC。"
                    )

        if del_tab_actions and len(del_tab_actions) >= DEL_SAFETY_MIN_COUNT and has_prev_state:
            matched = 0
            total_on_pc = 0
            for key, pc_info in pc.items():
                entry = state.files.get(key)
                if entry and entry.get("pc_mtime") is not None:
                    total_on_pc += 1
                    if abs(pc_info.mtime - entry["pc_mtime"]) < 1.0:
                        matched += 1

            if total_on_pc > 0:
                match_rate = matched / total_on_pc
                if match_rate < DEL_SAFETY_MIN_MATCH_RATE:
                    print(f"  ⚠ 安全阀触发：跳过 {len(del_tab_actions)} 个平板端删除操作 "
                          f"（PC mtime 匹配率仅 {match_rate:.0%}）")
                    actions = [a for a in actions if a.kind != "delete_tablet"]
                else:
                    print(f"  ℹ PC 剩余文件 mtime 匹配率 {match_rate:.0%}，"
                          f"确认 {len(del_tab_actions)} 个文件已被手动删除，将同步到平板。")

        return actions

    # ── Execution ─────────────────────────────────────────────────────────

    def _execute(self, actions: list[Action]) -> tuple[int, int, int, int, int]:
        """Run actions. Returns (pushed, pulled, conflicts, deleted_pc, deleted_tablet).

        If consecutive ADB failures are detected (USB disconnect), remaining ADB
        operations are skipped to avoid piling up timeouts.
        """
        pushed = pulled = conflicts = del_pc = del_tab = 0
        adb_failures = 0  # consecutive ADB operation failures

        for act in actions:
            # After 2 consecutive ADB failures, assume device is gone
            if adb_failures >= 2:
                if act.kind in ("push", "pull", "conflict", "delete_tablet"):
                    print(f"  ⊘ SKIP {act.kind.upper()} {act.path} (device disconnected)")
                    continue

            pc_file = self._cfg.pc_vault / act.path
            tablet_file = f"{self._cfg.tablet_vault}/{act.path}"

            if act.kind == "push":
                ok = self._adb.push(pc_file, tablet_file)
                if ok:
                    pushed += 1; adb_failures = 0
                    print(f"  ⬆  PUSH  {act.path}")
                else:
                    adb_failures += 1
                    print(f"  ✗ FAIL PUSH {act.path}")

            elif act.kind == "pull":
                ok = self._adb.pull(tablet_file, pc_file)
                if ok:
                    pulled += 1; adb_failures = 0
                    print(f"  ⬇  PULL  {act.path}")
                else:
                    adb_failures += 1
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
                    del_tab += 1; adb_failures = 0
                    print(f"  ✕ DEL TAB {act.path}")
                else:
                    adb_failures += 1
                    print(f"  ✗ FAIL DEL TAB {act.path}")

        return pushed, pulled, conflicts, del_pc, del_tab

    # ── Main Flow ─────────────────────────────────────────────────────────

    def prepare(self, paths: set[str] | None = None) -> tuple[list[Action], dict, dict, bool] | None:
        """扫描 + 比对，返回 (actions, pc_files, tablet_files, has_state) 或 None（错误）。

        将 sync 拆为 prepare/execute 两步，让调用方可以在执行前介入确认删除等操作。
        """
        # 1-4. 前置检查
        if not self._adb.check_device():
            print("Error: No ADB device connected. Check USB cable and USB debugging.")
            log_error("No ADB device connected")
            return None
        vault = self._cfg.pc_vault
        if not vault.is_dir():
            print(f"Error: PC vault not found at '{vault}'")
            log_error(f"PC vault not found: {vault}")
            return None
        if not self._git.is_repo():
            print(f"Warning: '{vault}' is not a git repo. Run --init first.")
            log_error(f"Not a git repo: {vault}")
            return None

        has_state = self._state.load()

        # 5. 前置快照
        if self._git.has_changes():
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._git.commit(f"pre-sync snapshot: {ts}")
            print(f"📸 Pre-sync snapshot committed")

        # 6. 扫描（先快速复检 ADB，断开则立即失败不浪费时间）
        print("Scanning...")
        if not self._adb.check_device(fast=True):
            print("Error: ADB device disconnected before scan.")
            log_error("ADB device disconnected before scan")
            return None
        pc_files = self._scan_pc()
        tablet_files = self._scan_tablet()
        print(f"  PC:      {len(pc_files)} files")
        print(f"  Tablet:  {len(tablet_files)} files")

        # 7. 比对
        actions = self._compare(pc_files, tablet_files, self._state, has_state)
        if paths is not None:
            actions = [a for a in actions if a.path in paths]

        if not actions:
            print("Already in sync.")
            self._save_post_state(
                self._scan_pc(), self._scan_tablet()
            )
            return [], pc_files, tablet_files, has_state

        # 8. 展示计划
        counts = {"push": 0, "pull": 0, "conflict": 0, "delete_pc": 0, "delete_tablet": 0}
        for a in actions:
            counts[a.kind] += 1
        print(f"\nChanges detected:")
        if counts["push"]:    print(f"  ⬆  Push:    {counts['push']}")
        if counts["pull"]:    print(f"  ⬇  Pull:    {counts['pull']}")
        if counts["conflict"]: print(f"  ⚡ Conflict: {counts['conflict']}")
        if counts["delete_pc"]: print(f"  ✕  Del PC:   {counts['delete_pc']}")
        if counts["delete_tablet"]: print(f"  ✕  Del Tab:  {counts['delete_tablet']}")

        return actions, pc_files, tablet_files, has_state

    def execute(self, actions: list[Action], dry_run: bool = False):
        """执行给定的 action 列表并完成状态保存和提交。"""
        if dry_run:
            print("\n[Dry run — no changes made]")
            return 0, 0, 0, 0, 0

        # 快速复检 ADB，断开则立即失败
        if not self._adb.check_device(fast=True):
            print("Error: ADB device disconnected before executing sync actions.")
            log_error("ADB device disconnected before executing")
            return 0, 0, 0, 0, 0

        print()
        pushed, pulled, conflicts, del_pc, del_tab = self._execute(actions)

        # 重新扫描 + 保存状态
        pc_files = self._scan_pc()
        tablet_files = self._scan_tablet()
        self._save_post_state(pc_files, tablet_files)

        # 后置提交
        parts = []
        if pulled:    parts.append(f"pulled {pulled}")
        if pushed:    parts.append(f"pushed {pushed}")
        if conflicts: parts.append(f"{conflicts} conflicts")
        if del_pc:    parts.append(f"del-pc {del_pc}")
        if del_tab:   parts.append(f"del-tab {del_tab}")
        msg = "sync: " + ", ".join(parts) if parts else "sync: no changes"
        if self._git.has_changes():
            self._git.commit(msg)
            print(f"\n📝 Git: {msg}")

        print("\n✔ Sync complete.")
        return pushed, pulled, conflicts, del_pc, del_tab

    def sync(self, dry_run: bool = False, paths: set[str] | None = None) -> bool:
        """CLI 便捷方法：prepare + execute 一气呵成（不暂停确认删除）。"""
        result = self.prepare(paths=paths)
        if result is None:
            return False
        actions, _pc, _tab, _st = result
        if not actions:
            return True
        self.execute(actions, dry_run=dry_run)
        return True

    def _save_post_state(self, pc: dict[str, FileInfo], tablet: dict[str, FileInfo]):
        """用当前扫描结果重建同步状态。

        必须在同步操作执行后调用，且传入的 pc/tablet 必须是最新扫描结果。
        旧状态完全被替换——因为它是基线，不是日志。
        """
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
