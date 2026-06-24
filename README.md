# Obsidian Sync

PC ↔ Android 平板 Obsidian 库双向同步工具，附带 Git 版本管理和 Web 可视化面板。

## 功能

| 模块 | 说明 |
|------|------|
| **双向同步** | 三向比对算法，自动检测 PC / 平板哪边有变更，决定推拉方向 |
| **冲突检测** | 两边同时修改同一文件时自动标记冲突，保留两份不丢数据 |
| **Git 版本管理** | 每次同步自动 commit，完整记录每条笔记的修改历史 |
| **Web 面板** | 本地浏览器操作面板，查看文件状态、变更历史、diff 对比 |
| **冲突合并** | 面板中可视化查看冲突，一键选择保留 PC 版或平板版 |
| **变更搜索** | 按关键词搜索所有历史 diff，找到「那句话是什麼时候改的」 |
| **健康检查** | 检测孤立附件、断链 wikilink、意外入库的大文件 |
| **文件夹切换** | 面板内自由浏览和切换 PC 端库文件夹 |

## 环境要求

- **Python** 3.8+
- **Git**
- **ADB**（Android Debug Bridge）—— 安装后平板需开启 USB 调试
- **Flask**（`pip install flask`）

## 快速开始

### 1. 克隆或下载项目

```
D:\Sekai\obsidian-sync\
├── sync.py            # 同步引擎
├── dashboard.py       # Web 面板
├── config.json        # 配置文件
├── templates/
│   └── index.html     # 面板页面
└── static/
    ├── style.css      # 面板样式
    └── app.js         # 面板逻辑
```

### 2. 配置

编辑 `config.json`：

```json
{
  "pc_vault_path": "D:/Sekai/obsidian-vault",
  "tablet_vault_path": "/sdcard/Documents/Note",
  "adb_path": "auto",
  "conflict_strategy": "keep_both",
  "delete_strategy": "propagate",
  "ignore_patterns": [
    ".obsidian/workspace*.json",
    ".obsidian/app.json",
    ".obsidian/appearance.json",
    ".trash/",
    ".DS_Store"
  ]
}
```

| 字段 | 说明 |
|------|------|
| `pc_vault_path` | PC 端 Obsidian 库路径，也支持在面板中切换 |
| `tablet_vault_path` | 平板端库路径，通常是 `/sdcard/Documents/Note` |
| `adb_path` | ADB 可执行文件路径，`"auto"` 自动检测 |
| `conflict_strategy` | 冲突策略：`"keep_both"` 保留两份 / `"skip"` 跳过 |
| `delete_strategy` | 删除策略：`"propagate"` 同步删除 / `"ignore"` 忽略 |
| `ignore_patterns` | 不同步的文件模式（glob 格式，`/` 结尾 = 整个目录） |

### 3. 连接平板

1. 平板开启 **USB 调试**（设置 → 开发者选项）
2. 用 USB 线连接电脑
3. 平板上弹出的 RSA 指纹对话框点「允许」
4. 终端验证：`adb devices` 应显示设备

### 4. 初始化

```bash
# 在项目目录下
python sync.py --init
```

这会：
- 创建 PC 端库文件夹
- 初始化 Git 仓库
- 扫描平板端现有文件，建立同步状态基线

### 5. 日常同步

```bash
# 命令行模式
python sync.py              # 执行同步
python sync.py --dry-run    # 预览变更（不执行）

# Web 面板模式
python dashboard.py         # 启动面板，浏览器打开 http://localhost:8820
```

## Web 面板

启动后浏览器访问 `http://localhost:8820`：

```
┌──────────────────────────────────────────────────────────┐
│ Obsidian Sync │ 📁 D:/Sekai/obsidian-vault │ ✅ 已同步   │
├──────────────────┬──────────────────┬────────────────────┤
│   文件状态        │   变更历史         │   详情 / 日志       │
│  → 待推送        │ 17:58 同步       │                    │
│  ← 待拉取        │ +3 -1 行        │   diff 视图        │
│  ⚡ 冲突         │                  │   或日志列表        │
│  ✅ 已同步       │ 🔍 搜索变更      │                    │
├──────────────────┴──────────────────┴────────────────────┤
│  健康: ✅ 一切正常                    │ 上次同步: 2 分钟前 │
└──────────────────────────────────────────────────────────┘
```

### 面板功能

| 区域 | 功能 |
|------|------|
| **顶部栏** | 库路径显示与切换、同步状态徽章、立即同步 / 预览按钮 |
| **左栏 — 文件状态** | 全部文件按状态列出，筛选：全部 / 待同步 / 冲突 |
| **中栏 — 变更历史** | Git 提交时间线，点击查看 diff，🔍 搜索历史变更 |
| **右栏 — 详情** | 文件内容查看，diff2html 渲染差异对比 |
| **右栏 — 日志** | 每次同步的操作日志，可展开查看完整输出 |
| **底部栏** | 健康检查结果 + 上次同步时间 |

### 快捷键

| 快捷键 | 操作 |
|--------|------|
| `Ctrl + S` | 立即同步 |
| `Ctrl + P` | 预览变更 |
| `Esc` | 关闭弹窗 |

## 同步算法

三向比对（three-way comparison）：

```
输入: PC 快照 + 平板快照 + 上次同步状态

对每个文件：
  ├─ 两边都有
  │   ├─ 都不变      → 跳过
  │   ├─ 只有 PC 变  → PUSH（推送到平板）
  │   ├─ 只有平板变  → PULL（拉取到 PC）
  │   └─ 两边都变    → CONFLICT（保留两份）
  │
  ├─ 只有 PC 有
  │   ├─ 平板曾有此文件 → 平板端被删 → 按删除策略处理
  │   └─ 纯新增        → PUSH
  │
  ├─ 只有平板有
  │   ├─ PC 曾有此文件  → PC 端被删  → 按删除策略处理
  │   └─ 纯新增        → PULL
  │
  └─ 都没有但有记录 → 两边都删了 → 静默清除
```

## Git 版本管理

PC 端库自动纳入 Git 管理，每次同步产生 1~2 个 commit：

```
523635c init: initial vault snapshot
ebc11b4 pre-sync snapshot: 2026-06-24 17:57:58   ← 同步前快照
5eb72ba sync: pushed 1                            ← 同步结果
8526745 sync: pulled 1
a6d375c pre-sync snapshot: 2026-06-24 17:58:59
c3266bb sync: 1 conflicts
```

- **pre-sync snapshot**：同步前自动保存当前 PC 状态，就算同步出错也能回滚
- **sync**：记录本次同步的变更概要（推拉数量、冲突数）

在库目录下可以随时查看历史：

```bash
cd D:/Sekai/obsidian-vault
git log --oneline          # 查看历史
git diff HEAD~1            # 查看最近一次同步的变更
git show <commit>          # 查看某次提交的完整 diff
```

## 冲突处理

当 PC 和平板同时修改了同一文件：

1. **命令行模式**：PC 版本保留在原名，平板版本保存为 `文件名.conflict.md`
2. **Web 面板**：冲突文件显示「保留 PC 版本」「保留平板版本」按钮，一键解决

## 项目结构

```
obsidian-sync/
├── sync.py              # 同步引擎（Config / ADB / SyncState / GitManager / ObsidianSync）
├── dashboard.py         # Flask Web 面板（API 路由 + SyncLogger + 文件夹浏览）
├── config.json          # 用户配置
├── .gitignore
├── README.md
├── templates/
│   └── index.html       # 单页面三栏布局
└── static/
    ├── style.css        # 暗色主题样式
    └── app.js           # 前端交互逻辑
```
