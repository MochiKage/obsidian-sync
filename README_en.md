# Obsidian Sync

Bidirectional PC ↔ Android tablet Obsidian vault sync with Git versioning and a web dashboard.

> [中文](README.md)

## Features

| Module | Description |
|------|------|
| **Bidirectional Sync** | Three-way comparison engine detects changes on PC/tablet and determines push/pull direction |
| **Conflict Detection** | Auto-detects simultaneous edits on both sides, keeps both versions |
| **Delete Confirmation** | Dashboard modal confirms before any file deletion — choose confirm / keep / re-push |
| **Safety Valve** | Blocks bulk deletions when tablet scan appears incomplete (mtime match rate < 70%) |
| **Git Versioning** | Auto-commits every sync (pre-sync snapshot + sync result), full note history |
| **Web Dashboard** | Local browser UI — file status, change history, diff viewer |
| **Staging Area** | Selective sync — check files, stage them, sync only what you choose |
| **Conflict Resolution** | Visual conflict viewer, one-click keep-PC or keep-tablet |
| **Change Search** | Search all historical diffs by keyword to find when any line changed |
| **Health Check** | Detects orphaned attachments, broken wikilinks, unexpectedly large files |
| **Folder Browser** | Browse and switch PC vault folders from the dashboard |
| **Error Logging** | All exceptions written to `.sync_errors.log` with timestamps and full tracebacks |
| **Resilience** | Fast USB disconnect detection (~5s), consecutive ADB failure skip, atomic state writes |

## Requirements

- **Python** 3.8+
- **Git**
- **ADB** (Android Debug Bridge) — enable USB debugging on tablet
- **Flask** (`pip install flask`)

## Quick Start

### 1. Project Structure

```
obsidian-sync/
├── sync.py            # Sync engine (Config / ADB / SyncState / GitManager / ObsidianSync)
├── dashboard.py       # Flask web dashboard (API routes + background threads + error handling)
├── config.json        # User configuration
├── start.bat          # One-click launcher
├── .sync_errors.log   # Error log (auto-generated)
├── templates/
│   └── index.html     # Single-page three-column layout + modals
└── static/
    ├── style.css      # Dark theme styles
    └── app.js         # Frontend logic
```

### 2. Configuration

Edit `config.json`:

```json
{
  "pc_vault_path": "D:/Documents/Obsidian/MyVault",
  "tablet_vault_path": "/sdcard/Documents/Note",
  "adb_path": "auto",
  "conflict_strategy": "keep_both",
  "delete_strategy": "propagate",
  "ignore_patterns": [
    ".git/",
    ".syncstate.json",
    ".obsidian/workspace*.json",
    ".obsidian/app.json",
    ".obsidian/appearance.json",
    ".trash/",
    ".DS_Store"
  ]
}
```

| Field | Description |
|------|------|
| `pc_vault_path` | PC Obsidian vault path (switchable from dashboard) |
| `tablet_vault_path` | Tablet vault path, typically `/sdcard/Documents/Note` |
| `adb_path` | ADB executable path, `"auto"` for automatic detection |
| `conflict_strategy` | Conflict handling: `"keep_both"` or `"skip"` |
| `delete_strategy` | Delete propagation: `"propagate"` or `"ignore"` |
| `ignore_patterns` | Glob patterns to exclude from sync (`/` suffix = directory) |

> ⚠️ Recommended: set `delete_strategy` to `"ignore"` to prevent accidental PC deletions when tablet files go missing. All deletions require dashboard confirmation.

### 3. Connect Tablet

1. Enable **USB Debugging** on tablet (Settings → Developer Options)
2. Connect via USB cable
3. Accept RSA fingerprint prompt on tablet
4. Verify: `adb devices` should list your device

### 4. Initialize

```bash
python sync.py --init
```

This will:
- Create the PC vault directory
- Initialize a Git repository
- Scan existing tablet files and establish sync baseline

### 5. Daily Use

```bash
# Web dashboard (recommended)
python dashboard.py         # or double-click start.bat
# Open http://localhost:8820

# CLI mode
python sync.py              # Run sync
python sync.py --dry-run    # Preview changes (no execution)
```

## Web Dashboard

Open `http://localhost:8820` after starting the server:

```
┌──────────────────────────────────────────────────────────────────┐
│ Obsidian Sync v2 │ 📁 D:/... │ ✅ Synced │ Preview │ Sync │ ⚡ All│
├──────────────────┬──────────────────┬────────────────────────────┤
│   File Status     │   Change History  │   Detail / Log             │
│  → To Push       │ 17:58 sync       │                            │
│  ← To Pull       │ +3 -1 lines      │   Diff view                │
│  ⚡ Conflict     │                  │   or log list              │
│  ✅ Synced       │ 🔍 Search       │                            │
│                  │                  │                            │
│  [All] [↓ Stage] │                  │                            │
│  ─────────────── │                  │                            │
│  Staging (N)      │                  │                            │
├──────────────────┴──────────────────┴────────────────────────────┤
│  Health: ✅ All clear                   │ Last sync: 2 min ago    │
└──────────────────────────────────────────────────────────────────┘
```

### Top Bar Buttons

| Button | Shortcut | Behavior |
|--------|----------|----------|
| **Preview** | `Ctrl+P` | Preview changes without executing |
| **Sync** | `Ctrl+S` | Staging has files → sync staging only; staging empty → confirm then sync all |
| **⚡ Sync All** | — | Skip staging and confirmation, sync all changes immediately |

### Panel Features

| Panel | Features |
|------|------|
| **Top Bar** | Vault path display & switch, sync status badge (click for details), three sync buttons |
| **Left — File Status** | File tree by status, filter: All / Pending / Conflict. Hover icon for modification times |
| **Left — Staging** | Selective sync: check files → `↓ Stage` → sync only staged files |
| **Middle — History** | Git commit timeline, click for diff, 🔍 search historical changes |
| **Right — Detail** | File content viewer, diff2html side-by-side diff, conflict resolution buttons |
| **Right — Log** | Per-sync operation log, expandable to view full output |
| **Bottom Bar** | Health check summary (click for details) + last sync time |

### Modal Interactions

| Trigger | Modal | Options |
|---------|-------|---------|
| Click "Pending" badge | Change Details | File list grouped by action type |
| Click health summary | Health Details | Orphaned files / broken links / large files |
| Delete operations detected | Delete Confirm | Confirm / Keep Files / **Push Anyway** (re-push to tablet) |
| Empty staging sync | Confirm | Sync Anyway / Cancel |

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl + S` | Sync |
| `Ctrl + P` | Preview |
| `Esc` | Close modal |

## Sync Algorithm

Three-way comparison:

```
Input: PC snapshot + Tablet snapshot + Last sync state (.syncstate.json)

Lifecycle: Pre-sync snapshot → Scan → Compare → [Confirm Deletes] → Execute → Rescan → Post-sync commit

Per file:
  ├─ On both sides
  │   ├─ Neither changed     → SKIP
  │   ├─ Only PC changed     → PUSH
  │   ├─ Only tablet changed → PULL
  │   └─ Both changed        → CONFLICT (keep both)
  │
  ├─ PC only
  │   ├─ Was on tablet       → Tablet deleted → follow delete_strategy
  │   └─ Brand new           → PUSH
  │
  ├─ Tablet only
  │   ├─ Was on PC           → PC deleted → follow delete_strategy
  │   └─ Brand new           → PULL
  │
  └─ Neither side but in state → Both deleted → silently remove from state
```

### Safety Valve

When ≥ 5 files would be deleted, the engine verifies tablet scan integrity:
- Remaining tablet files' mtime match rate ≥ 70% → scan is accurate, deletions are real → **proceed**
- Match rate < 70% → scan is suspect → **block deletions, warn user**

## Git Versioning

The PC vault is automatically managed with Git. Each sync creates 1–2 commits:

```
523635c init: initial vault snapshot
ebc11b4 pre-sync snapshot: 2026-06-25 17:57:58   ← pre-sync state saved
5eb72ba sync: pushed 1, del-pc 3                  ← sync result
```

- **pre-sync snapshot**: saves current PC state before sync; rollback point if anything goes wrong
- **sync**: records what changed (pushes, pulls, conflicts, deletions)

View history:

```bash
cd <your-vault-directory>
git log --oneline          # View history
git diff HEAD~1            # View last sync's changes
git show <commit>          # View full diff of a specific commit
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| **USB disconnect (before scan)** | 5s fast ADB check, immediate error |
| **USB disconnect (during execute)** | 2 consecutive ADB failures → skip remaining operations |
| **Process killed** | `.syncstate.json` written atomically (tmp + rename); auto-recovery from `.tmp` |
| **Incomplete tablet scan** | Safety valve blocks bulk deletions |
| **All exceptions** | Written to `.sync_errors.log` with timestamp and full traceback |

### Error Log

```bash
# View error log
cat .sync_errors.log

# Or via API
curl http://localhost:8820/api/errors
```

Log format:

```
[2026-06-25 00:16:12] ADB device disconnected before scan
  (no traceback)
------------------------------------------------------------
[2026-06-25 00:16:12] Sync job abc123 failed: TimeoutError
Traceback (most recent call last):
  ...
------------------------------------------------------------
```

## Performance

| Optimization | Description |
|-------------|-------------|
| Tablet scan merge | `find` + `stat` + `md5sum` in a single ADB shell call |
| Scan cache | 30s TTL, shared across requests within a page load |
| Staging operations | No file rescan triggered, instant response |
| Health check cache | 60s cache, avoids re-reading all `.md` files |

## Conflict Resolution

When the same file is modified on both PC and tablet:

1. **CLI mode**: PC version keeps original name, tablet version saved as `filename.conflict.md`
2. **Web dashboard**: Conflict files show "Keep PC Version" and "Keep Tablet Version" buttons for one-click resolution
