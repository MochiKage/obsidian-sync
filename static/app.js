// ── State ────────────────────────────────────────────────────────────────────
let currentFile = null;
let currentCommit = null;

// ── API Helpers ──────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  return res.json();
}

// ── Page Load ────────────────────────────────────────────────────────────────

async function loadAll() {
  await Promise.all([
    loadStatus(),
    loadFiles(),
    loadHistory(),
    loadHealth(),
  ]);
}

async function loadStatus() {
  const data = await api("/api/status");
  const badge = document.getElementById("status-badge");
  badge.textContent = data.status === "synced" ? "已同步" :
    data.status === "pending" ? "有变更" :
    data.status === "conflict" ? "有冲突" : "状态未知";
  badge.className = "badge " + data.status;

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

  const icons = { push: "⬆", pull: "⬇", conflict: "⚡", synced: "✅" };
  const labels = { push: "status-push", pull: "status-pull", conflict: "status-conflict", synced: "status-synced" };

  for (const f of data.files) {
    const div = document.createElement("div");
    div.className = `file-item ${labels[f.status]}`;
    if (currentFile === f.path) div.classList.add("active");
    div.innerHTML = `<span class="file-status">${icons[f.status]}</span><span class="file-path">${esc(f.path)}</span>`;
    div.addEventListener("click", () => showFileDetail(f));
    list.appendChild(div);
  }
}

document.getElementById("filter-select").addEventListener("change", loadFiles);

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

async function doSync(dryRun = false) {
  const btn = document.getElementById("btn-sync");
  btn.textContent = dryRun ? "预览中..." : "同步中...";
  btn.disabled = true;

  // Show a sync log area
  const content = document.getElementById("detail-content");
  content.innerHTML = `<div id="sync-log" class="dim">${dryRun ? '预览变更...' : '正在同步...'}</div>`;
  document.getElementById("detail-title").textContent = dryRun ? "预览" : "同步";

  try {
    const res = await api(`/api/sync?dry_run=${dryRun ? "1" : "0"}`, { method: "POST" });
    if (res.ok) {
      await loadAll();
      if (!dryRun) {
        loadHistory(); // refresh history after real sync
      }
    } else {
      document.getElementById("sync-log").textContent += "\n错误: " + (res.error || "未知");
    }
  } catch (e) {
    document.getElementById("sync-log").textContent += "\n请求失败: " + e;
  }

  btn.textContent = "立即同步";
  btn.disabled = false;
}

document.getElementById("btn-sync").addEventListener("click", () => doSync(false));
document.getElementById("btn-dry-run").addEventListener("click", () => doSync(true));

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

document.querySelectorAll(".modal-close").forEach(el => {
  el.addEventListener("click", closeSearch);
});

document.getElementById("search-modal").addEventListener("click", (e) => {
  if (e.target === document.getElementById("search-modal")) closeSearch();
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
      return;
    }
    if (data.healthy && data.issues.length === 0) {
      el.innerHTML = '健康: ✅ 一切正常';
    } else {
      const parts = data.issues.map(i => {
        const cls = i.severity === "error" ? "severity-error" : "severity-warning";
        return `<span class="health-issue ${cls}">${i.label}</span>`;
      });
      el.innerHTML = "健康: " + parts.join(" · ");
    }
  } catch (e) {
    document.getElementById("health-summary").textContent = "健康检查: 未运行";
  }
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
    closeSearch();
  }
});

// ── Init ─────────────────────────────────────────────────────────────────────
loadAll();
