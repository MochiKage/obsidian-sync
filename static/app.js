// ── State ────────────────────────────────────────────────────────────────────
console.log("Obsidian Sync Dashboard v2 — background sync + polling");
let currentFile = null;
let currentCommit = null;
let activeTab = "detail";
let stagedFiles = new Set();    // files currently in staging area
let vaultChecked = new Set();   // checked paths in vault tree
let stagingChecked = new Set(); // checked paths in staging area
let expandedDirs = new Set();   // expanded directory paths in file tree

// ── API Helpers ──────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const method = opts.method || "GET";
  console.debug("[api] " + method + " " + path + (opts.body ? " body:" + opts.body.substring(0,60) : ""));
  const res = await fetch(path, opts);
  const json = await res.json();
  if (!res.ok) {
    console.warn("[api] " + method + " " + path + " -> HTTP " + res.status, json);
  }
  return json;
}

// ── Tabs ─────────────────────────────────────────────────────────────────────

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    const tabName = tab.dataset.tab;
    switchTab(tabName);
    if (tabName === "log") loadLogs();
  });
});

function switchTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-content").forEach(c => c.classList.toggle("active", c.dataset.tab === name));
}

// ── Page Load ────────────────────────────────────────────────────────────────

async function loadAll() {
  // Sequential: avoids concurrent full-vault scans competing for disk I/O
  await loadStatus();
  await loadFiles();
  await loadStaging();
  await loadHistory();
  await loadHealth();
}

async function loadStatus() {
  const data = await api("/api/status");
  const badge = document.getElementById("status-badge");
  badge.textContent = data.status === "synced" ? "已同步" :
    data.status === "pending" ? "有变更" :
    data.status === "conflict" ? "有冲突" : "状态未知";
  badge.className = "badge " + data.status;

  // Make badge clickable when there are pending changes or conflicts
  if (data.status === "pending" || data.status === "conflict") {
    badge.style.cursor = "pointer";
    badge.title = "点击查看变更详情";
    badge.onclick = () => showPendingModal();
  } else {
    badge.style.cursor = "";
    badge.title = "";
    badge.onclick = null;
  }

  document.getElementById("file-count").textContent =
    `PC: ${data.pc_files} | 平板: ${data.tablet_files !== -1 ? data.tablet_files : "未连接"}`;

  const lastSync = document.getElementById("last-sync-time");
  if (data.last_sync) {
    const d = new Date(data.last_sync);
    lastSync.textContent = `上次同步: ${d.toLocaleString("zh-CN")}`;
  } else {
    lastSync.textContent = "";
  }
}

// ── File List ────────────────────────────────────────────────────────────────

async function loadFiles() {
  const filter = document.getElementById("filter-select").value;
  const data = await api(`/api/files?filter=${filter}`);
  const list = document.getElementById("file-list");
  list.innerHTML = "";

  const root = buildFileTree(data.files);
  renderFileTree(list, root, 0);
}

function buildFileTree(files) {
  // { dirs: { name: { dirs:{}, files:[], aggregate, path } }, files: [{...}] }
  const root = { dirs: {}, files: [] };

  for (const f of files) {
    const parts = f.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const name = parts[i];
      if (!node.dirs[name]) {
        node.dirs[name] = {
          name,
          dirs: {},
          files: [],
          path: parts.slice(0, i + 1).join("/"),
        };
      }
      node = node.dirs[name];
    }
    node.files.push(f);
  }

  // Compute aggregate status for all dirs (post-order)
  function setAggregate(node) {
    let agg = "synced";
    for (const d of Object.values(node.dirs)) {
      const child = setAggregate(d);
      if (child === "conflict") agg = "conflict";
      else if (child === "pending" && agg !== "conflict") agg = "pending";
    }
    for (const f of node.files) {
      if (f.status === "conflict") agg = "conflict";
      else if (f.status !== "synced" && agg !== "conflict") agg = "pending";
    }
    node.aggregate = agg;
    return agg;
  }
  setAggregate(root);

  return root;
}

function renderFileTree(container, node, depth) {
  const icons = { push: "⬆", pull: "⬇", conflict: "⚡", synced: "✅" };
  const labels = { push: "status-push", pull: "status-pull", conflict: "status-conflict", synced: "status-synced" };

  // Directories (sorted)
  const dirNames = Object.keys(node.dirs).sort((a, b) => a.localeCompare(b));
  for (const name of dirNames) {
    const dir = node.dirs[name];
    const wrapper = document.createElement("div");
    wrapper.className = "tree-node";

    const row = document.createElement("div");
    row.className = "tree-row";
    if (dir.aggregate === "conflict") row.classList.add("file-dir-conflict");
    else if (dir.aggregate === "pending") row.classList.add("file-dir-pending");
    row.style.paddingLeft = (depth * 16 + 8) + "px";

    const isExpanded = expandedDirs.has(dir.path);

    // Directory checkbox — selects all files recursively under this dir
    const dirCb = document.createElement("input");
    dirCb.type = "checkbox";
    dirCb.className = "tree-check";
    dirCb.dataset.dirpath = dir.path;
    dirCb.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleDirCheck(dirCb, wrapper);
    });
    row.appendChild(dirCb);

    const arrow = document.createElement("span");
    arrow.className = "tree-arrow";
    arrow.textContent = isExpanded ? "▼" : "▶";
    row.appendChild(arrow);

    const icon = document.createElement("span");
    icon.className = "tree-icon";
    icon.textContent = "📁";
    row.appendChild(icon);

    const label = document.createElement("span");
    label.className = "tree-name";
    label.textContent = name;
    row.appendChild(label);

    arrow.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleFileDir(wrapper);
    });

    wrapper.appendChild(row);

    const children = document.createElement("div");
    children.className = "tree-children";
    children.dataset.dirPath = dir.path;
    if (!isExpanded) children.classList.add("hidden");
    renderFileTree(children, dir, depth + 1);

    // Files directly in this directory
    renderFileRows(children, dir.files, depth + 1, icons, labels);

    wrapper.appendChild(children);
    container.appendChild(wrapper);
  }

  // Root-level files (only at depth 0)
  if (depth === 0) {
    renderFileRows(container, node.files, 0, icons, labels);
  }
}

function renderFileRows(container, files, depth, icons, labels) {
  const sorted = [...files].sort((a, b) => {
    const na = a.path.split("/").pop();
    const nb = b.path.split("/").pop();
    return na.localeCompare(nb);
  });

  for (const f of sorted) {
    const row = document.createElement("div");
    row.className = "file-tree-file " + labels[f.status];
    if (currentFile === f.path) row.classList.add("active");
    row.style.paddingLeft = (depth * 16 + 8) + "px";

    // Checkbox (stopPropagation so it doesn't trigger showFileDetail)
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "tree-check";
    cb.dataset.path = f.path;
    cb.checked = vaultChecked.has(f.path);
    cb.addEventListener("click", (e) => {
      e.stopPropagation();
      if (cb.checked) vaultChecked.add(f.path);
      else vaultChecked.delete(f.path);
      updateStageButtons();
      updateVaultCheckAll();
    });
    // When vaultChecked is restored from outside, keep checkbox in sync
    if (vaultChecked.has(f.path)) cb.checked = true;
    row.appendChild(cb);

    const arrow = document.createElement("span");
    arrow.className = "tree-arrow";
    arrow.style.visibility = "hidden";
    row.appendChild(arrow);

    const dirLabels = { push: "推送到平板 ⬆", pull: "从平板拉取 ⬇", conflict: "冲突 ⚡", synced: "已同步 ✅",
                        delete_pc: "将删除PC ✕", delete_tablet: "将删除平板 ✕" };

    const status = document.createElement("span");
    status.className = "tree-icon";
    status.textContent = icons[f.status];
    // Tooltip: direction + modification times
    let tip = dirLabels[f.status] || f.status;
    if (f.pc_mtime) tip += "\nPC: " + new Date(f.pc_mtime).toLocaleString("zh-CN");
    if (f.tablet_mtime) tip += "\n平板: " + new Date(f.tablet_mtime).toLocaleString("zh-CN");
    status.title = tip;
    row.appendChild(status);

    const name = document.createElement("span");
    name.className = "tree-name";
    name.textContent = f.path.split("/").pop();
    name.title = f.path + "\n" + tip;
    row.appendChild(name);

    row.addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT") return;
      showFileDetail(f);
    });
    container.appendChild(row);
  }
}

function toggleFileDir(wrapper) {
  const children = wrapper.querySelector(".tree-children");
  const arrow = wrapper.querySelector(".tree-arrow");
  const dirPath = children ? children.dataset.dirPath : null;
  if (!children) return;
  if (children.classList.contains("hidden")) {
    children.classList.remove("hidden");
    arrow.textContent = "▼";
    if (dirPath) expandedDirs.add(dirPath);
  } else {
    children.classList.add("hidden");
    arrow.textContent = "▶";
    if (dirPath) expandedDirs.delete(dirPath);
  }
}

document.getElementById("filter-select").addEventListener("change", () => { vaultChecked.clear(); expandedDirs.clear(); loadFiles(); });

// ── Staging Area ──────────────────────────────────────────────────────────────

async function loadStaging() {
  const data = await api("/api/staged");
  stagedFiles = new Set(data.files || []);
  stagingChecked.clear();
  renderStagingList();
  updateSyncButton();
}

function renderStagingList() {
  const list = document.getElementById("staging-list");
  const count = document.getElementById("staging-count");
  count.textContent = `暂存区 (${stagedFiles.size})`;

  if (stagedFiles.size === 0) {
    list.innerHTML = '<div class="placeholder">选择文件后点击「↓ 暂存」<br>添加到此处</div>';
    document.getElementById("btn-stage-clear").style.display = "none";
  } else {
    document.getElementById("btn-stage-clear").style.display = "";
  }

  // We need file status info from the last file API response
  fetchStagedFileInfo().then(files => {
    const sorted = [...files].sort((a, b) => a.path.localeCompare(b.path));
    list.innerHTML = "";
    const icons = { push: "⬆", pull: "⬇", conflict: "⚡", synced: "✅" };
    const labels = { push: "status-push", pull: "status-pull", conflict: "status-conflict", synced: "status-synced" };

    for (const f of sorted) {
      const row = document.createElement("div");
      row.className = "file-tree-file " + (labels[f.status] || "");
      row.style.paddingLeft = "8px";

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "tree-check";
      cb.dataset.path = f.path;
      cb.checked = stagingChecked.has(f.path);
      cb.addEventListener("click", (e) => {
        e.stopPropagation();
        if (cb.checked) stagingChecked.add(f.path);
        else stagingChecked.delete(f.path);
        updateStageButtons();
        updateStagingCheckAll();
      });
      row.appendChild(cb);

      const arrow = document.createElement("span");
      arrow.className = "tree-arrow";
      arrow.style.visibility = "hidden";
      row.appendChild(arrow);

      const dirLabels = { push: "推送到平板 ⬆", pull: "从平板拉取 ⬇", conflict: "冲突 ⚡", synced: "已同步 ✅" };

      const status = document.createElement("span");
      status.className = "tree-icon";
      status.textContent = icons[f.status] || "✅";
      let tip = dirLabels[f.status] || f.status;
      if (f.pc_mtime) tip += "\nPC: " + new Date(f.pc_mtime).toLocaleString("zh-CN");
      if (f.tablet_mtime) tip += "\n平板: " + new Date(f.tablet_mtime).toLocaleString("zh-CN");
      status.title = tip;
      row.appendChild(status);

      const name = document.createElement("span");
      name.className = "tree-name";
      name.textContent = f.path;
      name.title = f.path + "\n" + tip;
      row.appendChild(name);

      row.addEventListener("click", (e) => {
        if (e.target.tagName === "INPUT") return;
        showFileDetail(f);
      });
      list.appendChild(row);
    }
  });
}

async function fetchStagedFileInfo() {
  // Get current file info for staged paths from the /api/files endpoint
  const data = await api("/api/files?filter=all");
  const fileMap = {};
  for (const f of (data.files || [])) {
    fileMap[f.path] = f;
  }
  return [...stagedFiles].map(p => fileMap[p] || { path: p, status: "synced" });
}

async function stageAdd() {
  if (vaultChecked.size === 0) return;
  const paths = [...vaultChecked];
  await api("/api/staged", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths }),
  });
  vaultChecked.clear();
  updateStageButtons();
  updateVaultCheckAll();
  await loadStaging();
  // Note: skip loadFiles() — staging doesn't change file content, only selection state
}

async function stageRemove() {
  if (stagingChecked.size === 0) return;
  const paths = [...stagingChecked];
  await api("/api/staged", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths }),
  });
  stagingChecked.clear();
  updateStageButtons();
  updateStagingCheckAll();
  await loadStaging();
  // Note: skip loadFiles() — staging doesn't change file content
}

async function stageClear() {
  await api("/api/staged", { method: "DELETE" });
  stagingChecked.clear();
  vaultChecked.clear();
  await loadStaging();
  updateSyncButton();
}

function updateStageButtons() {
  document.getElementById("btn-stage-add").disabled = vaultChecked.size === 0;
  document.getElementById("btn-stage-add").textContent =
    vaultChecked.size > 0 ? `↓ 暂存 (${vaultChecked.size})` : "↓ 暂存";
  document.getElementById("btn-stage-remove").disabled = stagingChecked.size === 0;
  document.getElementById("btn-stage-remove").textContent =
    stagingChecked.size > 0 ? `↑ 移除 (${stagingChecked.size})` : "↑ 移除";
}

function toggleDirCheck(dirCb, wrapper) {
  const checked = dirCb.checked;
  dirCb.indeterminate = false;
  const children = wrapper.querySelector(".tree-children");
  if (!children) return;

  // Check/uncheck all file checkboxes in subtree
  children.querySelectorAll(".tree-check[data-path]").forEach(cb => {
    cb.checked = checked;
    if (checked) vaultChecked.add(cb.dataset.path);
    else vaultChecked.delete(cb.dataset.path);
  });

  // Check/uncheck nested directory checkboxes
  children.querySelectorAll(".tree-check[data-dirpath]").forEach(cb => {
    cb.checked = checked;
    cb.indeterminate = false;
  });

  updateStageButtons();
  updateVaultCheckAll();
}

function checkAllVault(checked) {
  vaultChecked.clear();
  if (checked) {
    document.querySelectorAll("#file-list .tree-check[data-path]").forEach(cb => {
      cb.checked = true;
      vaultChecked.add(cb.dataset.path);
    });
    document.querySelectorAll("#file-list .tree-check[data-dirpath]").forEach(cb => {
      cb.checked = true;
      cb.indeterminate = false;
    });
  } else {
    document.querySelectorAll("#file-list .tree-check").forEach(cb => {
      cb.checked = false;
      cb.indeterminate = false;
    });
  }
  updateStageButtons();
}

function checkAllStaging(checked) {
  stagingChecked.clear();
  if (checked) {
    document.querySelectorAll("#staging-list .tree-check[data-path]").forEach(cb => {
      cb.checked = true;
      stagingChecked.add(cb.dataset.path);
    });
  } else {
    document.querySelectorAll("#staging-list .tree-check").forEach(cb => { cb.checked = false; });
  }
  updateStageButtons();
}

function updateVaultCheckAll() {
  const all = document.querySelectorAll("#file-list .tree-check[data-path]");
  const checked = document.querySelectorAll("#file-list .tree-check[data-path]:checked");
  const cb = document.getElementById("vault-check-all");
  cb.checked = all.length > 0 && checked.length === all.length;
  cb.indeterminate = checked.length > 0 && checked.length < all.length;
  updateDirCheckStates();
}

function updateDirCheckStates() {
  document.querySelectorAll("#file-list .tree-check[data-dirpath]").forEach(dirCb => {
    const wrapper = dirCb.closest(".tree-node");
    if (!wrapper) return;
    const children = wrapper.querySelector(".tree-children");
    if (!children) return;
    const all = children.querySelectorAll(".tree-check[data-path]");
    const sel = children.querySelectorAll(".tree-check[data-path]:checked");
    if (sel.length === 0 && all.length > 0) {
      dirCb.checked = false;
      dirCb.indeterminate = false;
    } else if (sel.length === all.length && all.length > 0) {
      dirCb.checked = true;
      dirCb.indeterminate = false;
    } else if (sel.length > 0) {
      dirCb.checked = false;
      dirCb.indeterminate = true;
    } else {
      dirCb.checked = false;
      dirCb.indeterminate = false;
    }
  });
}

function updateStagingCheckAll() {
  const all = document.querySelectorAll("#staging-list .tree-check");
  const checked = document.querySelectorAll("#staging-list .tree-check:checked");
  const cb = document.getElementById("staging-check-all");
  cb.checked = all.length > 0 && checked.length === all.length;
  cb.indeterminate = checked.length > 0 && checked.length < all.length;
}

// Button event listeners
document.getElementById("btn-stage-add").addEventListener("click", stageAdd);
document.getElementById("btn-stage-remove").addEventListener("click", stageRemove);
document.getElementById("btn-stage-clear").addEventListener("click", stageClear);
document.getElementById("vault-check-all").addEventListener("click", function() { checkAllVault(this.checked); });
document.getElementById("staging-check-all").addEventListener("click", function() { checkAllStaging(this.checked); });

async function showFileDetail(file) {
  currentFile = file.path;
  currentCommit = null;
  loadFiles();

  const title = document.getElementById("detail-title");
  title.textContent = file.path;

  const actions = document.getElementById("detail-actions");
  actions.innerHTML = "";

  const content = document.getElementById("detail-content");
  content.innerHTML = '<div class="dim" style="padding:14px;">加载中...</div>';

  // Load file content
  const fileData = await api(`/api/file/content?path=${encodeURIComponent(file.path)}`);

  if (file.status === "conflict") {
    // Show conflict resolution UI
    content.innerHTML = `
      <div class="conflict-banner">
        <h3>⚡ 冲突文件</h3>
        <p class="dim">此文件在 PC 和平板上都被修改了。</p>
        <p class="dim">PC 版本已保留在 ${esc(file.path)}，平板版本可用下方按钮查看。</p>
        <div class="conflict-buttons">
          <button class="btn-resolve btn-outline" onclick="resolveConflict('${esc(file.path)}', 'keep_pc')">✅ 保留 PC 版本</button>
          <button class="btn-resolve btn-outline" onclick="resolveConflict('${esc(file.path)}', 'keep_tablet')">📱 保留平板版本</button>
        </div>
      </div>
      <div class="detail-section">
        <h3>当前内容 (PC)</h3>
        <pre style="white-space:pre-wrap;font-size:13px;">${esc(fileData.content || '')}</pre>
      </div>
    `;
  } else {
    content.innerHTML = `
      <div class="detail-section">
        <div class="dim" style="margin-bottom:8px;">
          ${file.pc_mtime ? 'PC: ' + new Date(file.pc_mtime).toLocaleString("zh-CN") : ''}
          ${file.tablet_mtime ? ' | 平板: ' + new Date(file.tablet_mtime).toLocaleString("zh-CN") : ''}
        </div>
        <pre style="white-space:pre-wrap;font-size:13px;">${esc(fileData.content || '（空文件）')}</pre>
      </div>
    `;
  }
}

async function resolveConflict(path, action) {
  const res = await api("/api/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, action }),
  });
  if (res.ok) {
    loadFiles();
    loadStatus();
    document.getElementById("detail-content").innerHTML =
      '<div class="placeholder" style="color:var(--green);">冲突已解决，下次同步生效</div>';
  }
}

// ── History ──────────────────────────────────────────────────────────────────

async function loadHistory(page = 0) {
  const data = await api(`/api/history?page=${page}`);
  const list = document.getElementById("history-list");
  list.innerHTML = "";

  for (const c of data.commits) {
    const div = document.createElement("div");
    div.className = "history-item";
    if (currentCommit === c.hash) div.classList.add("active");

    const statsHtml = c.stats ? c.stats.replace(/(\d+) insertion[^,]*/g, '<span class="add">+$1</span>').replace(/(\d+) deletion[^,]*/g, '<span class="del">-$1</span>') : "";

    const date = new Date(c.date);
    div.innerHTML = `
      <div class="history-msg">${esc(c.message)}</div>
      <div class="history-meta">${date.toLocaleString("zh-CN")} · ${c.hash.substring(0, 7)}</div>
      <div class="history-stats">${statsHtml}</div>
    `;
    div.addEventListener("click", () => showDiff(c.hash));
    list.appendChild(div);
  }
}

async function showDiff(hash) {
  currentCommit = hash;
  currentFile = null;
  loadHistory();
  loadFiles();

  const title = document.getElementById("detail-title");
  title.textContent = hash.substring(0, 7);

  const content = document.getElementById("detail-content");
  content.innerHTML = '<div class="dim" style="padding:14px;">加载 diff...</div>';

  const data = await api(`/api/diff/${hash}`);

  if (data.error) {
    content.innerHTML = `<div class="dim" style="padding:14px;">${data.error}</div>`;
    return;
  }

  content.innerHTML = `
    <div class="detail-section">
      <div class="dim" style="margin-bottom:4px;">${esc(data.message)} · ${esc(data.date)}</div>
      <div class="dim">${data.files.length} files changed</div>
    </div>
  `;

  // Render diff with diff2html
  if (data.diff && data.diff.trim()) {
    const diffContainer = document.createElement("div");
    diffContainer.id = "diff-render";
    content.appendChild(diffContainer);

    const d2h = Diff2HtmlUI;
    const config = {
      drawFileList: false,
      matching: "lines",
      outputFormat: "side-by-side",
    };
    const target = document.getElementById("diff-render");
    d2h.html(target, data.diff, config);
  } else {
    content.innerHTML += '<div class="dim" style="padding:14px;">无变更内容</div>';
  }
}

// ── Sync ─────────────────────────────────────────────────────────────────────

let _syncRunning = false;  // guard against double-click

async function doSync(dryRun = false, skipStagingCheck = false) {
  if (_syncRunning) { console.warn("[doSync] already running, ignoring"); return; }
  _syncRunning = true;
  console.log("[doSync] START dryRun=" + dryRun + " stagedFiles=" + stagedFiles.size + " skipCheck=" + skipStagingCheck);

  try {

  const btn = document.getElementById("btn-sync");
  const allBtn = document.getElementById("btn-sync-all");
  const dryBtn = document.getElementById("btn-dry-run");
  if (!btn) { console.error("[doSync] btn-sync not found!"); return; }

  const hasStaged = stagedFiles.size > 0;

  // Warn if staging area is empty (not for dry-run, not for sync-all)
  if (!hasStaged && !dryRun && !skipStagingCheck) {
    const go = await showEmptyStagingWarning();
    if (!go) {
      console.log("[doSync] user cancelled empty staging sync");
      btn.disabled = false;
      dryBtn.disabled = false;
      if (allBtn) allBtn.disabled = false;
      _syncRunning = false;
      return;
    }
  }

  const label = hasStaged ? `暂存区 (${stagedFiles.size})` : (dryRun ? "预览中..." : "同步中...");
  btn.textContent = label;
  dryBtn.textContent = dryRun ? "预览中..." : "预览";
  btn.disabled = true;
  dryBtn.disabled = true;
  if (allBtn) allBtn.disabled = true;
  console.log("[doSync] button disabled, label=" + label);

  // Show live output in detail tab
  switchTab("detail");
  const content = document.getElementById("detail-content");
  const logEl = document.createElement("div");
  logEl.id = "sync-log";
  logEl.className = "dim";
  content.innerHTML = "";
  content.appendChild(logEl);
  logEl.textContent = dryRun ? '🔍 预览变更...' : '⏳ 正在同步 (准备发送请求)...';
  document.getElementById("detail-title").textContent = dryRun ? "预览" : "同步";
  console.log("[doSync] UI updated, about to send POST /api/sync");

  try {
    // 1. Start the sync job
    const body = hasStaged ? JSON.stringify({ paths: [...stagedFiles] }) : undefined;
    const headers = body ? { "Content-Type": "application/json" } : undefined;
    const url = `/api/sync?dry_run=${dryRun ? "1" : "0"}`;
    console.log("[doSync] POST " + url + " body=" + (body ? body.substring(0, 80) : "none"));

    const startRes = await api(url, { method: "POST", headers, body });
    console.log("[doSync] POST response:", JSON.stringify(startRes));

    if (!startRes.ok) {
      logEl.textContent += "\n\n❌ 启动失败: " + (startRes.error || "未知");
      console.error("[doSync] start failed:", startRes);
      btn.disabled = false;
      dryBtn.disabled = false;
      if (allBtn) allBtn.disabled = false;
      updateSyncButton();
      return;
    }

    const jobId = startRes.job_id;
    console.log("[doSync] job started, jobId=" + jobId + ", beginning poll loop");

    // 2. Poll until done
    const spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
    const MAX_WAIT = 120;
    let tick = 0;
    let elapsed = 0;

    const poll = async () => {
      let statusRes;
      const statusUrl = `/api/sync/status/${jobId}`;
      try {
        statusRes = await api(statusUrl);
      } catch (e) {
        console.warn("[doSync] poll fetch error:", e);
        tick++;
        elapsed++;
        if (elapsed > 10) {
          logEl.textContent += `\n\n❌ 轮询失败: 无法连接到服务器`;
          btn.disabled = false; dryBtn.disabled = false; if (allBtn) allBtn.disabled = false; updateSyncButton();
          return;
        }
        logEl.textContent = spinner[tick % spinner.length] + " 连接中断，重试... (" + elapsed + "s)";
        await new Promise(r => setTimeout(r, 1000));
        return poll();
      }

      tick++;
      elapsed++;
      console.log("[doSync] poll #" + tick + " status=" + statusRes.status);

      if (elapsed > MAX_WAIT) {
        logEl.textContent += "\n\n⚠️ 同步超时 (" + MAX_WAIT + "s)，可能仍在后台运行。稍后查看日志标签页。";
        btn.disabled = false; dryBtn.disabled = false; if (allBtn) allBtn.disabled = false; updateSyncButton();
        return;
      }

      if (statusRes.status === "running") {
        logEl.textContent = spinner[tick % spinner.length] + " " + (dryRun ? '预览中' : '同步中') + "... (" + elapsed + "s)";
        await new Promise(r => setTimeout(r, 1000));
        return poll();
      }

      // Delete confirmation needed
      if (statusRes.status === "confirm_delete") {
        logEl.textContent = statusRes.output || "";
        logEl.textContent += "\n\n⚠️ 等待确认删除操作...";
        document.getElementById("detail-title").textContent = "确认删除";

        const decision = await showDeleteConfirmModal(statusRes.deletes || []);
        const confirmRes = await api(`/api/sync/confirm/${jobId}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: decision }),
        });
        console.log("[doSync] confirm response:", confirmRes);

        if (decision === "proceed") {
          logEl.textContent += "\n✅ 已确认删除，继续同步...";
        } else if (decision === "push_instead") {
          logEl.textContent += "\n📤 已将删除转为推送，文件将重新同步到平板...";
        } else {
          logEl.textContent += "\nℹ️ 已跳过删除，继续同步其余操作...";
        }
        // Resume polling
        await new Promise(r => setTimeout(r, 500));
        return poll();
      }

      // Done or error
      console.log("[doSync] sync finished, status=" + statusRes.status);
      if (statusRes.status === "done" && statusRes.ok !== false) {
        logEl.textContent = statusRes.output || "(无输出)";
        document.getElementById("detail-title").textContent = dryRun ? "预览结果" : "同步结果";
        if (statusRes.staged_cleared) {
          stagedFiles.clear();
          stagingChecked.clear();
        }
        await loadAll();
        if (!dryRun) loadHistory();
        loadLogs();
        updateSyncButton();
      } else {
        logEl.textContent = (statusRes.output || "") + "\n\n❌ 错误: " + (statusRes.error || "未知");
        document.getElementById("detail-title").textContent = dryRun ? "预览失败" : "同步失败";
      }

      btn.disabled = false;
      dryBtn.disabled = false;
      if (allBtn) allBtn.disabled = false;
      updateSyncButton();
    };

    await poll();
  } catch (e) {
    console.error("[doSync] EXCEPTION:", e);
    logEl.textContent += "\n\n❌ JS异常: " + (e.message || e) + "\n查看控制台(F12)获取详情";
    btn.disabled = false;
    dryBtn.disabled = false;
    if (allBtn) allBtn.disabled = false;
    updateSyncButton();
  }

  } finally {
    _syncRunning = false;
  }
}

function updateSyncButton() {
  const btn = document.getElementById("btn-sync");
  if (stagedFiles.size > 0) {
    btn.textContent = `同步暂存区 (${stagedFiles.size})`;
  } else {
    btn.textContent = "立即同步";
  }
}

// ── Delete Confirmation Modal ────────────────────────────────────────────

function showDeleteConfirmModal(deletes) {
  return new Promise((resolve) => {
    const modal = document.getElementById("delete-confirm-modal");
    const list = document.getElementById("delete-confirm-list");
    const btnProceed = document.getElementById("btn-delete-proceed");
    const btnSkip = document.getElementById("btn-delete-skip");
    const btnPushAnyway = document.getElementById("btn-delete-push-anyway");

    // Build file list
    const delPC = deletes.filter(d => d.side === "pc");
    const delTab = deletes.filter(d => d.side === "tablet");
    let html = "";

    if (delPC.length > 0) {
      html += `<div class="pending-group-header" style="color:var(--red);">✕ 将删除 PC 端文件 (${delPC.length})</div>`;
      for (const d of delPC) {
        const name = d.path.split("/").pop();
        const dir = d.path.includes("/") ? d.path.substring(0, d.path.lastIndexOf("/")) + "/" : "";
        html += `<div class="pending-file" title="${esc(d.path)}"><span class="pending-file-dir">${esc(dir)}</span><span class="pending-file-name">${esc(name)}</span></div>`;
      }
    }
    if (delTab.length > 0) {
      html += `<div class="pending-group-header" style="color:var(--red);">✕ 将删除平板端文件 (${delTab.length})</div>`;
      for (const d of delTab) {
        html += `<div class="pending-file" title="${esc(d.path)}"><span class="tree-icon" style="font-size:14px;width:18px;min-width:18px;">📄</span><span>${esc(d.path)}</span></div>`;
      }
    }

    list.innerHTML = html;
    modal.classList.remove("hidden");

    function cleanup(decision) {
      modal.classList.add("hidden");
      btnProceed.onclick = null;
      btnSkip.onclick = null;
      if (btnPushAnyway) btnPushAnyway.onclick = null;
      resolve(decision);
    }

    btnProceed.onclick = () => cleanup("proceed");
    btnSkip.onclick = () => cleanup("skip");
    if (btnPushAnyway) btnPushAnyway.onclick = () => cleanup("push_instead");

    // Background click to close = skip
    modal.onclick = (e) => {
      if (e.target === modal) cleanup("skip");
    };
  });
}

// ── Empty Staging Warning ────────────────────────────────────────────────

function showEmptyStagingWarning() {
  return new Promise((resolve) => {
    const modal = document.getElementById("empty-staging-modal");
    const btnGo = document.getElementById("btn-empty-staging-go");

    modal.classList.remove("hidden");

    function cleanup(go) {
      modal.classList.add("hidden");
      btnGo.onclick = null;
      resolve(go);
    }

    btnGo.onclick = () => cleanup(true);

    // modal-close buttons and background click = cancel
    modal.querySelector(".modal-close").onclick = () => cleanup(false);
    modal.onclick = (e) => {
      if (e.target === modal) cleanup(false);
    };
  });
}

document.getElementById("btn-sync").addEventListener("click", () => doSync(false));
document.getElementById("btn-dry-run").addEventListener("click", () => doSync(true));
document.getElementById("btn-sync-all").addEventListener("click", () => doSync(false, true));

// ── Search ───────────────────────────────────────────────────────────────────

document.getElementById("btn-search").addEventListener("click", () => {
  document.getElementById("search-modal").classList.remove("hidden");
  document.getElementById("search-results").innerHTML = "";
});

document.getElementById("search-submit").addEventListener("click", async () => {
  const q = document.getElementById("search-input").value.trim();
  if (!q) return;

  const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
  const results = document.getElementById("search-results");

  if (data.results.length === 0) {
    results.innerHTML = '<div class="dim">未找到匹配的变更记录</div>';
    return;
  }

  results.innerHTML = data.results.map(r => `
    <div class="search-result" onclick="showDiff('${r.hash}');closeSearch();">
      <span class="hash">${r.hash.substring(0, 7)}</span>
      <span>${esc(r.message)}</span>
    </div>
  `).join("");
});

// Generic: all modal-close buttons close their parent modal
document.querySelectorAll(".modal-close").forEach(el => {
  el.addEventListener("click", () => {
    const modal = el.closest(".modal");
    if (modal) modal.classList.add("hidden");
  });
});

// Background click closes modal
document.querySelectorAll(".modal").forEach(modal => {
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.classList.add("hidden");
  });
});

function closeSearch() {
  document.getElementById("search-modal").classList.add("hidden");
}

// ── Health ───────────────────────────────────────────────────────────────────

async function loadHealth() {
  try {
    const data = await api("/api/health");
    const el = document.getElementById("health-summary");
    if (data.error) {
      el.textContent = "健康检查: 跳过";
      el.style.cursor = "";
      el.onclick = null;
      return;
    }
    if (data.healthy && data.issues.length === 0) {
      el.innerHTML = '健康: ✅ 一切正常';
      el.style.cursor = "";
      el.onclick = null;
    } else {
      const parts = data.issues.map(i => {
        const cls = i.severity === "error" ? "severity-error" : "severity-warning";
        return `<span class="health-issue ${cls}">${i.label}</span>`;
      });
      el.innerHTML = "健康: " + parts.join(" · ");
      el.style.cursor = "pointer";
      el.title = "点击查看详情";
      el.onclick = () => showHealthModal(data);
    }
  } catch (e) {
    document.getElementById("health-summary").textContent = "健康检查: 未运行";
    document.getElementById("health-summary").style.cursor = "";
    document.getElementById("health-summary").onclick = null;
  }
}

// ── Pending Changes Modal ─────────────────────────────────────────────────

async function showPendingModal() {
  const modal = document.getElementById("pending-modal");
  const title = document.getElementById("pending-title");
  const summary = document.getElementById("pending-summary");
  const list = document.getElementById("pending-list");

  title.textContent = "加载中...";
  summary.innerHTML = "";
  list.innerHTML = '<div class="dim">加载变更列表...</div>';
  modal.classList.remove("hidden");

  try {
    const data = await api("/api/files?filter=pending");
    const files = data.files || [];

    // Group by status
    const groups = { push: [], pull: [], conflict: [], delete_pc: [], delete_tablet: [] };
    const icons = { push: "⬆", pull: "⬇", conflict: "⚡", delete_pc: "✕", delete_tablet: "✕" };
    const labels = { push: "待推送 (PC→平板)", pull: "待拉取 (平板→PC)", conflict: "冲突", delete_pc: "将删除 PC", delete_tablet: "将删除平板" };
    const colors = { push: "var(--blue)", pull: "var(--green)", conflict: "var(--red)", delete_pc: "var(--yellow)", delete_tablet: "var(--yellow)" };

    for (const f of files) {
      const s = f.status;
      if (groups[s]) groups[s].push(f);
    }

    // Summary bar
    const total = files.length;
    if (total === 0) {
      title.textContent = "变更详情";
      summary.innerHTML = '<span style="color:var(--green);">✅ 没有待同步的变更</span>';
      list.innerHTML = "";
      return;
    }

    title.textContent = `变更详情 (${total} 个文件)`;
    let summaryHtml = "";
    for (const [key, arr] of Object.entries(groups)) {
      if (arr.length > 0) {
        summaryHtml += `<span class="pending-chip" style="border-color:${colors[key]}">${icons[key]} ${labels[key]}: <strong>${arr.length}</strong></span>`;
      }
    }
    summary.innerHTML = summaryHtml;

    // File list
    let listHtml = "";
    for (const [key, arr] of Object.entries(groups)) {
      if (arr.length === 0) continue;
      listHtml += `<div class="pending-group"><div class="pending-group-header" style="color:${colors[key]}">${icons[key]} ${labels[key]} (${arr.length})</div>`;
      const sorted = [...arr].sort((a, b) => a.path.localeCompare(b.path));
      for (const f of sorted) {
        const name = f.path.split("/").pop();
        const dir = f.path.includes("/") ? f.path.substring(0, f.path.lastIndexOf("/")) + "/" : "";
        listHtml += `<div class="pending-file" title="${esc(f.path)}"><span class="pending-file-dir">${esc(dir)}</span><span class="pending-file-name">${esc(name)}</span></div>`;
      }
      listHtml += "</div>";
    }
    list.innerHTML = listHtml;
  } catch (e) {
    title.textContent = "变更详情";
    list.innerHTML = '<div class="dim">加载失败: ' + esc(String(e)) + '</div>';
  }
}

// ── Health Detail Modal ───────────────────────────────────────────────────

function showHealthModal(data) {
  const modal = document.getElementById("health-modal");
  const content = document.getElementById("health-detail-content");
  modal.classList.remove("hidden");

  if (!data || !data.issues || data.issues.length === 0) {
    content.innerHTML = '<div style="color:var(--green);padding:12px 0;">✅ 一切正常，未发现问题</div>';
    return;
  }

  let html = "";
  for (const issue of data.issues) {
    const sevIcon = issue.severity === "error" ? "🔴" : "🟡";
    const sevLabel = issue.severity === "error" ? "错误" : "警告";
    const sevColor = issue.severity === "error" ? "var(--red)" : "var(--yellow)";

    html += `<div class="health-detail-section">`;
    html += `<h4 style="color:${sevColor};margin-bottom:8px;">${sevIcon} ${issue.label} <span style="font-size:11px;color:var(--text-dim);">(${sevLabel})</span></h4>`;

    if (issue.files) {
      html += `<div class="health-detail-files">`;
      for (const f of issue.files) {
        const sizeStr = f.size ? ` (${formatFileSize(f.size)})` : "";
        html += `<div class="health-detail-file"><span class="tree-icon" style="font-size:14px;width:18px;min-width:18px;">📄</span><span>${esc(f.path)}</span><span class="dim" style="margin-left:auto;font-size:11px;">${sizeStr}</span></div>`;
      }
      if (issue.files.length >= 20) {
        html += `<div class="dim" style="font-size:11px;padding:4px 0;">... 以及其他 ${issue.files.length - 20} 个文件</div>`;
      }
      html += `</div>`;
    }

    if (issue.links) {
      html += `<div class="health-detail-files">`;
      for (const l of issue.links) {
        html += `<div class="health-detail-file"><span class="tree-icon" style="font-size:14px;width:18px;min-width:18px;">🔗</span><span>[[${esc(l.target)}]]</span><span class="dim" style="margin-left:auto;font-size:11px;">在 ${esc(l.source)}</span></div>`;
      }
      if (issue.links.length >= 20) {
        html += `<div class="dim" style="font-size:11px;padding:4px 0;">... 以及其他 ${issue.links.length - 20} 个断链</div>`;
      }
      html += `</div>`;
    }

    html += `</div>`;
  }

  content.innerHTML = html;
}

function formatFileSize(bytes) {
  if (!bytes || bytes < 1024) return (bytes || 0) + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

// ── Utils ────────────────────────────────────────────────────────────────────

function esc(s) {
  if (!s) return "";
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Keyboard Shortcuts ───────────────────────────────────────────────────────

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key === "s") {
    e.preventDefault();
    doSync(false);
  }
  if (e.ctrlKey && e.key === "p") {
    e.preventDefault();
    doSync(true);
  }
  if (e.key === "Escape") {
    // Close any visible modal
    document.querySelectorAll(".modal:not(.hidden)").forEach(m => m.classList.add("hidden"));
  }
});

// ── Sync Logs ────────────────────────────────────────────────────────────────

async function loadLogs() {
  const data = await api("/api/logs");
  const container = document.getElementById("log-content");
  if (!data.logs || data.logs.length === 0) {
    container.innerHTML = '<div class="placeholder">暂无同步日志<br>执行同步后在此显示</div>';
    return;
  }

  container.innerHTML = data.logs.map(log => {
    const statusIcon = log.status === "success" ? "✅" : log.status === "failed" ? "❌" : "🔄";
    const statusClass = log.status === "success" ? "success" : log.status === "failed" ? "failed" : "running";
    const startTime = new Date(log.start).toLocaleString("zh-CN");
    const label = log.dry_run ? "预览" : "同步";
    const duration = log.end ? formatDuration((new Date(log.end) - new Date(log.start)) / 1000) : "";
    // Show first line of output as summary
    const firstLine = (log.output || "").split("\n").find(l => l.trim()) || "";

    return `
      <div class="log-entry" onclick="toggleLog(this)">
        <div class="log-entry-header">
          <span class="log-status ${statusClass}">${statusIcon}</span>
          <span class="log-time">${startTime}</span>
          <span class="log-label">${label}</span>
          <span class="log-duration">${duration}</span>
        </div>
        <div class="log-summary">${esc(firstLine)}</div>
        <div class="log-body">${esc(log.output || "(无输出)")}</div>
      </div>
    `;
  }).join("");
}

function toggleLog(el) {
  el.classList.toggle("expanded");
}

function formatDuration(sec) {
  if (sec < 1) return "<1s";
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m${s}s`;
}

// ── Vault Selector ───────────────────────────────────────────────────────────

async function loadCurrentVault() {
  try {
    const data = await api("/api/config");
    const el = document.getElementById("vault-path");
    el.textContent = "📁 " + data.pc_vault_path;
    el.title = data.pc_vault_path;
  } catch (e) {
    document.getElementById("vault-path").textContent = "📁 (加载失败)";
  }
}

document.getElementById("btn-change-vault").addEventListener("click", openFolderBrowser);
document.getElementById("vault-path").addEventListener("click", openFolderBrowser);

// ── Folder Browser (Tree View) ───────────────────────────────────────────────

let selectedPath = "";
let treeState = {};  // path -> { entries: [], loaded: bool }

async function openFolderBrowser() {
  document.getElementById("browse-modal").classList.remove("hidden");
  selectedPath = "";
  treeState = {};
  document.getElementById("browse-select").disabled = true;
  document.getElementById("browse-select").textContent = "选择此文件夹 (先点击文件夹选中)";
  await renderTreeRoot();
}

async function renderTreeRoot() {
  const list = document.getElementById("browse-list");
  list.innerHTML = '<div class="tree-row"><span class="dim">加载中...</span></div>';

  document.getElementById("browse-breadcrumb").innerHTML =
    '<span class="browse-crumb">此电脑</span>';

  const data = await api("/api/browse?path=");
  list.innerHTML = "";

  for (const entry of (data.entries || [])) {
    renderTreeNode(list, entry, 0);
  }
}

function renderTreeNode(container, entry, depth) {
  const isDir = entry.type === "dir" || entry.type === "drive";

  // Wrapper holds the row + children container
  const nodeWrapper = document.createElement("div");
  nodeWrapper.className = "tree-node";

  const row = document.createElement("div");
  row.className = "tree-row";
  row.style.paddingLeft = (depth * 20 + 8) + "px";

  // Expand/collapse arrow
  const arrow = document.createElement("span");
  arrow.className = "tree-arrow";
  arrow.textContent = isDir ? "▶" : "";
  arrow.style.visibility = isDir ? "visible" : "hidden";
  row.appendChild(arrow);

  // Icon
  const icon = document.createElement("span");
  icon.className = "tree-icon";
  icon.textContent = entry.type === "drive" ? "💽" : "📁";
  row.appendChild(icon);

  // Name
  const name = document.createElement("span");
  name.className = "tree-name";
  name.textContent = entry.name;
  row.appendChild(name);

  // Click row to select (not on arrow)
  row.addEventListener("click", (e) => {
    if (!e.target.classList.contains("tree-arrow")) {
      selectFolder(entry.path, row);
    }
  });

  // Double-click to expand/collapse directories
  if (isDir) {
    row.addEventListener("dblclick", (e) => {
      if (!e.target.classList.contains("tree-arrow")) {
        selectFolder(entry.path, row);
        toggleTreeChildren(entry.path, nodeWrapper, depth);
      }
    });
  }

  nodeWrapper.appendChild(row);

  // Children container (hidden initially, populated on expand)
  const childrenContainer = document.createElement("div");
  childrenContainer.className = "tree-children hidden";
  childrenContainer.setAttribute("data-path", entry.path);
  nodeWrapper.appendChild(childrenContainer);

  container.appendChild(nodeWrapper);

  // Highlight if already selected
  if (entry.path.replace(/\\/g, "/") === selectedPath.replace(/\\/g, "/")) {
    row.classList.add("selected");
  }

  // Arrow click toggles children
  if (isDir) {
    arrow.addEventListener("click", async (e) => {
      e.stopPropagation();
      await toggleTreeChildren(entry.path, nodeWrapper, depth);
    });
  }
}

async function toggleTreeChildren(path, nodeWrapper, depth) {
  const arrow = nodeWrapper.querySelector(".tree-arrow");
  const childrenContainer = nodeWrapper.querySelector(".tree-children");

  // Collapse if already expanded
  if (!childrenContainer.classList.contains("hidden")) {
    childrenContainer.classList.add("hidden");
    arrow.textContent = "▶";
    return;
  }

  // Lazy-load children
  if (!treeState[path] || !treeState[path].loaded) {
    try {
      const data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
      treeState[path] = { entries: data.entries || [], loaded: true };
    } catch (e) {
      treeState[path] = { entries: [], loaded: true };
    }
  }

  // Populate children if first time
  if (childrenContainer.children.length === 0 && treeState[path].entries.length > 0) {
    for (const child of treeState[path].entries) {
      renderTreeNode(childrenContainer, child, depth + 1);
    }
  }

  childrenContainer.classList.remove("hidden");
  arrow.textContent = "▼";
}

function selectFolder(path, row) {
  selectedPath = path;
  document.querySelectorAll("#browse-list .tree-row").forEach(r => r.classList.remove("selected"));
  if (row) row.classList.add("selected");

  // Update breadcrumb to show selected path
  const bread = document.getElementById("browse-breadcrumb");
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  let html = '<span class="browse-crumb">此电脑</span>';
  for (const part of parts) {
    html += `<span class="browse-sep">/</span><span class="browse-crumb">${esc(part)}</span>`;
  }
  bread.innerHTML = html;

  const btn = document.getElementById("browse-select");
  btn.disabled = false;
  btn.textContent = `选择: ${getShortPath(path)}`;
}

function getShortPath(p) {
  const parts = p.replace(/\\/g, "/").replace(/\/$/, "").split("/");
  if (parts.length <= 2) return p;
  return ".../" + parts.slice(-2).join("/");
}

document.getElementById("browse-select").addEventListener("click", async () => {
  if (!selectedPath) return;
  const btn = document.getElementById("browse-select");
  btn.textContent = "切换中...";
  btn.disabled = true;

  try {
    const res = await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pc_vault_path: selectedPath }),
    });
    if (res.ok) {
      document.getElementById("browse-modal").classList.add("hidden");
      loadCurrentVault();
      loadAll();
    } else {
      alert("切换失败: " + (res.error || "未知错误"));
    }
  } catch (e) {
    alert("请求失败: " + e);
  } finally {
    btn.textContent = "选择此文件夹";
    btn.disabled = false;
  }
});

// Close folder browser on background click
document.getElementById("browse-modal").addEventListener("click", (e) => {
  if (e.target === document.getElementById("browse-modal")) {
    document.getElementById("browse-modal").classList.add("hidden");
  }
});

// ── Init ─────────────────────────────────────────────────────────────────────
loadAll();
loadCurrentVault();
