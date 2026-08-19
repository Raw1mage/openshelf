// openshelf 繁體中文前端控制器（全自動統一聚合檢索、斷點續傳背景監控與動態最小化）

const getBasePath = () => {
  const path = window.location.pathname;
  if (path.startsWith("/libgen")) {
    return "/libgen";
  }
  return "";
};

const BASE_PATH = getBasePath();
let currentFilters = {
  format: "all",
  language: "all"
};
let currentSort = "relevance";
let currentPage = 1;
let currentResults = [];
let selectedMd5s = new Set();
let queuePollInterval = null;
let cachedJobsByMd5 = new Map();

document.addEventListener("DOMContentLoaded", () => {
  initEventListeners();
  startQueuePolling();
});

function initEventListeners() {
  const searchInput = document.getElementById("searchInput");
  const searchBtn = document.getElementById("searchBtn");

  searchBtn.addEventListener("click", () => {
    currentPage = 1;
    handleSearch();
  });

  searchInput.addEventListener("keyup", (e) => {
    if (e.key === "Enter") {
      currentPage = 1;
      handleSearch();
    }
  });

  // 排序下拉選單
  const sortSelect = document.getElementById("sortSelect");
  if (sortSelect) {
    sortSelect.addEventListener("change", (e) => {
      currentSort = e.target.value;
      applySortAndRender();
    });
  }

  // 篩選按鈕群
  document.querySelectorAll(".filter-chip").forEach(chip => {
    chip.addEventListener("click", (e) => {
      const type = chip.dataset.type;
      const val = chip.dataset.val;

      document.querySelectorAll(`.filter-chip[data-type="${type}"]`).forEach(c => c.classList.remove("active"));
      chip.classList.add("active");

      currentFilters[type] = val;
      currentPage = 1;
      
      const query = searchInput.value.trim();
      if (query) {
        handleSearch();
      }
    });
  });

  // 全選 Checkbox
  const selectAllCheckbox = document.getElementById("selectAllCheckbox");
  selectAllCheckbox.addEventListener("change", (e) => {
    const checked = e.target.checked;
    selectedMd5s.clear();
    document.querySelectorAll(".book-select-checkbox").forEach(cb => {
      cb.checked = checked;
      if (checked) {
        selectedMd5s.add(cb.dataset.md5);
      }
    });
    updateBatchBar();
  });

  // 批次下載按鈕
  document.getElementById("batchDownloadBtn").addEventListener("click", triggerBatchDownload);

  // 下載佇列 Modal 事件（展開、關閉、縮小在背景運作）
  const queueModal = document.getElementById("queueModal");
  const openQueueBtn = document.getElementById("openQueueBtn");
  const minimizeQueueBtn = document.getElementById("minimizeQueueBtn");

  openQueueBtn.addEventListener("click", () => {
    queueModal.classList.add("active");
    refreshQueueModal();
  });
  if (minimizeQueueBtn) {
    minimizeQueueBtn.addEventListener("click", () => queueModal.classList.remove("active"));
  }

  // 點選 modal 以外任何地方自動縮小
  document.addEventListener("mousedown", (e) => {
    if (!queueModal.classList.contains("active")) return;
    const card = document.getElementById("queueModalCard");
    if (card && !card.contains(e.target) && !openQueueBtn.contains(e.target)) {
      queueModal.classList.remove("active");
    }
  });

  // 初始化收書佇列拖曳、拉伸與 LocalStorage 偏好記憶
  initQueueModalDragAndResize();

  const clearCompletedBtn = document.getElementById("clearCompletedBtn");
  if (clearCompletedBtn) {
    clearCompletedBtn.addEventListener("click", async () => {
      try {
        const res = await fetch(`${BASE_PATH}/api/crawler/jobs/clear-completed`, { method: "POST" });
        if (res.ok) {
          refreshQueueModal();
        }
      } catch (err) {
        console.error("清理失敗:", err);
      }
    });
  }

  // 手動上傳 Modal
  const uploadModal = document.getElementById("uploadModal");
  const openUploadBtn = document.getElementById("openUploadBtn");
  const closeUploadBtn = document.getElementById("closeUploadBtn");
  const uploadForm = document.getElementById("uploadForm");
  const fileInput = document.getElementById("fileInput");
  const dropZone = document.getElementById("dropZone");

  openUploadBtn.addEventListener("click", () => uploadModal.classList.add("active"));
  closeUploadBtn.addEventListener("click", () => uploadModal.classList.remove("active"));

  // 個人書單 Modal 事件
  const openCollectionsBtn = document.getElementById("openCollectionsBtn");
  const closeCollectionsBtn = document.getElementById("closeCollectionsBtn");
  const newCollectionBtn = document.getElementById("newCollectionBtn");
  if (openCollectionsBtn) openCollectionsBtn.addEventListener("click", () => openCollectionsModal());
  if (closeCollectionsBtn) closeCollectionsBtn.addEventListener("click", closeCollectionsModal);
  if (newCollectionBtn) newCollectionBtn.addEventListener("click", createNewCollectionPrompt);

  // 快速收藏 Modal 事件
  const closeQuickCollectionBtn = document.getElementById("closeQuickCollectionBtn");
  const saveQuickCollectionBtn = document.getElementById("saveQuickCollectionBtn");
  if (closeQuickCollectionBtn) closeQuickCollectionBtn.addEventListener("click", () => document.getElementById("quickCollectionModal").classList.remove("active"));
  if (saveQuickCollectionBtn) saveQuickCollectionBtn.addEventListener("click", saveQuickCollections);

  // 逛線上書攤 Modal 事件
  const openBookstallBtn = document.getElementById("openBookstallBtn");
  const closeBookstallBtn = document.getElementById("closeBookstallBtn");
  if (openBookstallBtn) openBookstallBtn.addEventListener("click", openBookstallModal);
  if (closeBookstallBtn) closeBookstallBtn.addEventListener("click", closeBookstallModal);

  dropZone.addEventListener("click", () => fileInput.click());
  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    if (e.dataTransfer.files.length > 0) {
      fileInput.files = e.dataTransfer.files;
      document.getElementById("fileSelectNotice").innerText = `已選擇: ${fileInput.files[0].name}`;
    }
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files.length > 0) {
      document.getElementById("fileSelectNotice").innerText = `已選擇: ${fileInput.files[0].name}`;
    }
  });

  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!fileInput.files.length) {
      alert("請先選擇要上傳的 PDF 或 EPUB 檔案！");
      return;
    }

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    const titleVal = document.getElementById("uploadTitle").value.trim();
    const authorVal = document.getElementById("uploadAuthor").value.trim();
    if (titleVal) formData.append("custom_title", titleVal);
    if (authorVal) formData.append("custom_author", authorVal);

    const submitBtn = document.getElementById("uploadSubmitBtn");
    submitBtn.innerText = "解析與入庫中...";
    submitBtn.disabled = true;

    try {
      const res = await fetch(`${BASE_PATH}/api/upload`, {
        method: "POST",
        body: formData
      });
      if (!res.ok) throw new Error("上傳失敗");
      const data = await res.json();
      alert(`《${data.title}》已成功入庫！`);
      uploadModal.classList.remove("active");
      uploadForm.reset();
      document.getElementById("fileSelectNotice").innerText = "點選或將檔案拖曳至此處";
      handleSearch();
    } catch (err) {
      alert("上傳解析失敗: " + err.message);
    } finally {
      submitBtn.innerText = "開始上傳與自動解析";
      submitBtn.disabled = false;
    }
  });

  // 詳情 Modal
  document.getElementById("closeDetailBtn").addEventListener("click", () => {
    document.getElementById("detailModal").classList.remove("active");
  });
}

// 統一聚合搜尋核心（同時並行查詢 本地書庫 + 全網公網鏡像）
async function handleSearch() {
  const query = document.getElementById("searchInput").value.trim();
  const bookList = document.getElementById("bookList");
  const totalCountEl = document.getElementById("totalCount");
  const resultsHeader = document.getElementById("resultsHeader");
  const selectAllCheckbox = document.getElementById("selectAllCheckbox");

  if (!query) {
    alert("請輸入欲搜尋的書名、作者、ISBN、DOI 或關鍵字！");
    return;
  }

  resultsHeader.style.display = "none";
  selectAllCheckbox.style.display = "none";
  selectAllCheckbox.checked = false;
  selectedMd5s.clear();
  updateBatchBar();

  bookList.innerHTML = `
    <div class="search-loading-box">
      <div class="spinner-ring"></div>
      <p class="search-loading-text">🔍 正在統一檢索本地書庫與全網公網鏡像...</p>
    </div>
  `;

  let localItems = [];
  let remoteItems = [];

  const localPromise = (async () => {
    try {
      const params = new URLSearchParams({
        q: query,
        format: currentFilters.format,
        language: currentFilters.language,
        page: currentPage,
        page_size: 50
      });
      const res = await fetch(`${BASE_PATH}/api/search?${params}`);
      if (res.ok) {
        const data = await res.json();
        localItems = data.items || [];
      }
    } catch (e) {}
  })();

  const remotePromise = (async () => {
    try {
      const res = await fetch(`${BASE_PATH}/api/crawler/search?q=${encodeURIComponent(query)}`);
      if (res.ok) {
        const data = await res.json();
        remoteItems = data.items || [];
      }
    } catch (e) {}
  })();

  await Promise.all([localPromise, remotePromise]);

  // 合併與去重：若公網資源中已有本地收錄 (以 MD5 比對)，優先以本地狀態呈現
  const localMd5s = new Set(localItems.map(i => (i.md5 || "").toLowerCase()).filter(Boolean));
  const filteredRemote = [];

  for (const r of remoteItems) {
    const md5 = (r.md5 || "").toLowerCase();
    if (md5 && localMd5s.has(md5)) {
      continue; // 已在本地項目中，免重複顯示
    }
    if (currentFilters.format !== "all" && r.format !== currentFilters.format) {
      continue;
    }
    if (currentFilters.language !== "all") {
      if (currentFilters.language === "zh" && !r.language.toLowerCase().includes("zh") && !r.language.toLowerCase().includes("chinese")) {
        continue;
      }
      if (currentFilters.language === "en" && !r.language.toLowerCase().includes("en") && !r.language.toLowerCase().includes("english")) {
        continue;
      }
    }
    filteredRemote.push(r);
  }

  const combinedItems = [...localItems, ...filteredRemote];
  currentResults = combinedItems;
  resultsHeader.style.display = "flex";

  const totalCount = combinedItems.length;
  const localCount = localItems.length;
  const remoteCount = filteredRemote.length;

  if (totalCount === 0) {
    totalCountEl.innerText = `查無符合書籍`;
    bookList.innerHTML = `
      <div style="text-align:center; padding: 3rem; background: var(--bg-secondary); border-radius: var(--radius);">
        <p style="font-size: 1.1rem; color: var(--text-secondary); margin-bottom: 1rem;">📭 本地與公網鏡像均未找到符合之書籍</p>
        <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1.5rem;">建議嘗試更換關鍵字、英文書名或 ISBN 再次檢索，或直接手動上傳檔案。</p>
        <button class="btn btn-primary" onclick="document.getElementById('openUploadBtn').click()">➕ 手動上傳檔案入庫</button>
      </div>
    `;
    return;
  }

  totalCountEl.innerText = `聚合找到 ${totalCount} 本書籍（💾 本地已收錄 ${localCount} 本，🌐 公網可收書 ${remoteCount} 本）`;
  
  if (remoteCount > 0) {
    selectAllCheckbox.style.display = "inline-block";
  }

  applySortAndRender();
}

function applySortAndRender() {
  const bookList = document.getElementById("bookList");
  if (!currentResults || currentResults.length === 0) return;

  const sortedItems = [...currentResults].sort((a, b) => {
    const isALocal = a.availability_tier === 0 || (a.work_id && !a.work_id.startsWith("libgen_"));
    const isBLocal = b.availability_tier === 0 || (b.work_id && !b.work_id.startsWith("libgen_"));

    if (currentSort === "year_desc") {
      const yearA = parseInt(a.publication_year, 10) || 0;
      const yearB = parseInt(b.publication_year, 10) || 0;
      if (yearA !== yearB) return yearB - yearA;
      if (isALocal !== isBLocal) return isALocal ? -1 : 1;
      return 0;
    } else if (currentSort === "year_asc") {
      const yearA = parseInt(a.publication_year, 10) || 9999;
      const yearB = parseInt(b.publication_year, 10) || 9999;
      if (yearA !== yearB) return yearA - yearB;
      if (isALocal !== isBLocal) return isALocal ? -1 : 1;
      return 0;
    } else if (currentSort === "size_desc") {
      const sizeA = a.size_bytes || 0;
      const sizeB = b.size_bytes || 0;
      return sizeB - sizeA;
    } else if (currentSort === "title_asc") {
      return (a.title || "").localeCompare(b.title || "");
    } else {
      // relevance (預設：本地落地優先，維持關聯度)
      if (isALocal !== isBLocal) return isALocal ? -1 : 1;
      return 0;
    }
  });

  bookList.innerHTML = sortedItems.map(item => {
    if (item.availability_tier === 0 || (item.work_id && !item.work_id.startsWith("libgen_"))) {
      return renderLocalBookCard(item);
    } else {
      return renderLiveBookCard(item);
    }
  }).join("");

  bindCheckboxEvents();
}

function renderLocalBookCard(item) {
  const formatTag = getFormatTag(item.format);
  const langTag = item.language ? `<span class="tag tag-lang">${item.language.toUpperCase()}</span>` : "";
  const progressPercent = item.progress_ratio ? Math.round(item.progress_ratio * 100) : 0;
  const sizeMb = item.size_bytes ? (item.size_bytes / (1024 * 1024)).toFixed(1) + " MB" : "";
  const yearText = item.publication_year ? `• ${item.publication_year}年` : "";

  return `
    <div class="book-card">
      <div style="display: flex; align-items: center; padding-right: 0.5rem;">
        <span style="font-size: 1.25rem;" title="本地已落地">💾</span>
      </div>
      <div class="book-main">
        <div class="book-title">${escapeHtml(item.title)}</div>
        <div class="book-meta">
          <span class="tag tag-local">💾 本地已落地</span>
          ${formatTag}
          ${langTag}
          <span>✍️ ${escapeHtml(item.authors_display || "未知作者")}</span>
          <span>${yearText}</span>
          <span>💾 ${sizeMb}</span>
        </div>
        ${item.snippet ? `<div class="snippet-box">...${item.snippet}...</div>` : ""}
        ${progressPercent > 0 ? `
          <div style="margin-top: 0.5rem; font-size: 0.78rem; color: var(--accent);">已閱讀 ${progressPercent}%</div>
          <div class="progress-bar-bg"><div class="progress-bar-fill" style="width: ${progressPercent}%;"></div></div>
        ` : ""}
      </div>
      <div class="book-actions">
        <button class="btn btn-primary" onclick="openReader('${item.work_id}')" title="線上閱讀" style="padding: 0.4rem 0.75rem; font-size: 1.1rem;">📖</button>
        <button class="btn btn-secondary" onclick="openQuickCollection('${item.work_id}', '${escapeHtml(item.title)}')" title="加入個人書單" style="padding: 0.4rem 0.75rem; font-size: 1.1rem;">⭐</button>
        <a class="btn btn-secondary" href="${BASE_PATH}/api/files/${item.work_id}/raw" download title="下載原檔" style="padding: 0.4rem 0.75rem; font-size: 1.1rem; text-decoration: none; display: inline-flex; align-items: center;">📥</a>
        <button class="btn btn-outline" onclick="openDetail('${item.work_id}')" title="書目詳情" style="padding: 0.4rem 0.75rem; font-size: 1.1rem;">ℹ️</button>
      </div>
    </div>
  `;
}

function renderLiveBookCard(item) {
  const formatTag = getFormatTag(item.format);
  const langTag = item.language ? `<span class="tag tag-lang">${item.language.toUpperCase()}</span>` : "";
  const sizeMb = item.size_bytes ? (item.size_bytes / (1024 * 1024)).toFixed(1) + " MB" : "";
  const yearText = item.publication_year ? `• ${item.publication_year}年` : "";
  
  const md5Key = (item.md5 || "").toLowerCase();
  const queueJob = cachedJobsByMd5.get(md5Key) || (item.queue_status ? {
    status: item.queue_status,
    progress_percent: item.queue_progress || 0,
    job_id: item.queue_job_id,
    work_id: item.local_work_id
  } : null);

  const isCompleted = (item.availability_tier === 0 && item.local_work_id) || (queueJob && queueJob.status === "completed");
  const isDownloading = queueJob && queueJob.status === "downloading";
  const isQueued = queueJob && queueJob.status === "queued";
  const isPaused = queueJob && queueJob.status === "paused";
  const isFailed = queueJob && queueJob.status === "failed";

  let leftIndicatorHtml = "";
  let statusBadgeHtml = "";
  let actionButtonsHtml = "";

  if (isCompleted) {
    const targetWorkId = item.local_work_id || (queueJob && queueJob.work_id);
    leftIndicatorHtml = `<span style="font-size: 1.25rem;" title="本地已收錄">💾</span>`;
    statusBadgeHtml = `<span class="tag tag-local">💾 本地已收錄</span>`;
    actionButtonsHtml = `
      <button class="btn btn-primary" onclick="openReader('${targetWorkId}')" title="立即閱讀" style="padding: 0.4rem 0.75rem; font-size: 1.1rem;">📖</button>
      <button class="btn btn-secondary" onclick="openQuickCollection('${targetWorkId}', '${escapeHtml(item.title)}')" title="加入個人書單" style="padding: 0.4rem 0.75rem; font-size: 1.1rem;">⭐</button>
      <button class="btn btn-outline" onclick="openDetail('${targetWorkId}')" title="詳情" style="padding: 0.4rem 0.75rem; font-size: 1.1rem;">ℹ️</button>
    `;
  } else if (isDownloading) {
    leftIndicatorHtml = `<span class="pulse-anim" style="font-size: 1.15rem;" title="下載中">⏳</span>`;
    statusBadgeHtml = `<span class="tag" style="background: rgba(56, 189, 248, 0.18); color: var(--accent); border: 1px solid var(--accent);"><span class="pulse-anim">⏳</span> 正在鏡像 (${queueJob.progress_percent}%)</span>`;
    actionButtonsHtml = `
      <button class="btn btn-secondary" onclick="document.getElementById('openQueueBtn').click()" title="點擊查看收書佇列">📥 正在鏡像 (${queueJob.progress_percent}%)</button>
      <button class="btn btn-outline" onclick="previewLiveDetail('${item.md5}')">ℹ️ 詳情</button>
    `;
  } else if (isQueued) {
    leftIndicatorHtml = `<span style="font-size: 1.15rem;" title="排隊中">⏳</span>`;
    statusBadgeHtml = `<span class="tag" style="background: rgba(245, 158, 11, 0.18); color: #f59e0b; border: 1px solid rgba(245,158,11,0.4);">⏳ 排隊收書中</span>`;
    actionButtonsHtml = `
      <button class="btn btn-secondary" onclick="document.getElementById('openQueueBtn').click()" title="點擊查看收書佇列">⏳ 排隊中</button>
      <button class="btn btn-outline" onclick="previewLiveDetail('${item.md5}')">ℹ️ 詳情</button>
    `;
  } else if (isPaused) {
    leftIndicatorHtml = `<span style="font-size: 1.15rem;" title="已暫停">⏸️</span>`;
    statusBadgeHtml = `<span class="tag" style="background: rgba(148, 163, 184, 0.18); color: var(--text-muted);">⏸️ 暫停收書中</span>`;
    actionButtonsHtml = `
      <button class="btn btn-outline" onclick="resumeJob('${queueJob.job_id}')" title="繼續收書">▶️ 繼續收書</button>
      <button class="btn btn-outline" onclick="previewLiveDetail('${item.md5}')">ℹ️ 詳情</button>
    `;
  } else if (isFailed) {
    leftIndicatorHtml = `<span style="font-size: 1.15rem;" title="下載失敗">❌</span>`;
    statusBadgeHtml = `<span class="tag" style="background: rgba(239, 68, 68, 0.18); color: #ef4444;">❌ 收書失敗</span>`;
    actionButtonsHtml = `
      <button class="btn btn-primary" onclick="retryJob('${queueJob.job_id}')" title="重新續傳">🔄 重新收書</button>
      <button class="btn btn-outline" onclick="previewLiveDetail('${item.md5}')">ℹ️ 詳情</button>
    `;
  } else {
    leftIndicatorHtml = item.md5 ? `
      <input type="checkbox" class="book-select-checkbox" data-md5="${item.md5}" style="cursor: pointer; width: 18px; height: 18px;">
    ` : `<span style="font-size: 1.25rem;">🌐</span>`;
    statusBadgeHtml = `<span class="tag tag-remote">🌐 公網資源</span>`;
    actionButtonsHtml = `
      <button class="btn btn-primary" id="btn-dl-${item.md5}" onclick="triggerSingleDownload('${item.md5}')">📥 鏡像收書</button>
      <button class="btn btn-outline" onclick="previewLiveDetail('${item.md5}')">ℹ️ 詳情</button>
    `;
  }

  return `
    <div class="book-card" style="align-items: center;">
      <div style="display: flex; align-items: center; padding-right: 0.5rem;">
        ${leftIndicatorHtml}
      </div>
      <div class="book-main">
        <div class="book-title">${escapeHtml(item.title)}</div>
        <div class="book-meta">
          ${statusBadgeHtml}
          ${formatTag}
          ${langTag}
          <span>✍️ ${escapeHtml(item.authors_display || "未知作者")}</span>
          <span>${yearText}</span>
          <span>💾 ${sizeMb}</span>
          ${item.publisher ? `<span>🏢 ${escapeHtml(item.publisher)}</span>` : ""}
          ${item.md5 ? `<span style="font-family:monospace; font-size:0.75rem; color:var(--text-muted);">MD5: ${item.md5.substring(0, 8)}...</span>` : ""}
        </div>
      </div>
      <div class="book-actions">
        ${actionButtonsHtml}
      </div>
    </div>
  `;
}

function bindCheckboxEvents() {
  document.querySelectorAll(".book-select-checkbox").forEach(cb => {
    cb.addEventListener("change", (e) => {
      const md5 = cb.dataset.md5;
      if (cb.checked) {
        selectedMd5s.add(md5);
      } else {
        selectedMd5s.delete(md5);
      }
      updateBatchBar();
    });
  });
}

function updateBatchBar() {
  const batchBar = document.getElementById("batchBar");
  const countEl = document.getElementById("selectedCount");
  countEl.innerText = selectedMd5s.size;
  if (selectedMd5s.size > 0) {
    batchBar.style.display = "flex";
  } else {
    batchBar.style.display = "none";
  }
}

async function triggerSingleDownload(md5) {
  const item = currentResults.find(r => r.md5 === md5);
  if (!item) return;

  const btn = document.getElementById(`btn-dl-${md5}`);
  if (btn) {
    btn.innerText = "加入佇列中...";
    btn.disabled = true;
  }

  try {
    const res = await fetch(`${BASE_PATH}/api/crawler/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        md5: item.md5,
        title: item.title,
        authors: item.authors_display,
        extension: item.extension || "pdf",
        mirror_links: item.mirror_links || []
      })
    });
    if (!res.ok) throw new Error("加入下載失敗");
    const job = await res.json();
    if (btn) {
      btn.innerText = "⏳ 正在鏡像...";
    }
    openQueueModal();
  } catch (err) {
    alert("鏡像下載啟動失敗: " + err.message);
    if (btn) {
      btn.innerText = "📥 鏡像收書";
      btn.disabled = false;
    }
  }
}

async function triggerBatchDownload() {
  if (selectedMd5s.size === 0) return;

  const itemsToDownload = [];
  for (const md5 of selectedMd5s) {
    const item = currentResults.find(r => r.md5 === md5);
    if (item) {
      itemsToDownload.push({
        md5: item.md5,
        title: item.title,
        authors: item.authors_display,
        extension: item.extension || "pdf",
        mirror_links: item.mirror_links || []
      });
    }
  }

  try {
    const res = await fetch(`${BASE_PATH}/api/crawler/batch-download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: itemsToDownload })
    });
    if (!res.ok) throw new Error("批次下載失敗");
    const data = await res.json();
    alert(`已成功將 ${data.enqueued_count} 本書籍加入本地鏡像下載佇列！`);
    selectedMd5s.clear();
    updateBatchBar();
    openQueueModal();
  } catch (err) {
    alert("批次下載失敗: " + err.message);
  }
}

async function retryJob(jobId) {
  try {
    const res = await fetch(`${BASE_PATH}/api/crawler/jobs/${jobId}/retry`, { method: "POST" });
    if (!res.ok) throw new Error("重試失敗");
    refreshQueueModal();
  } catch (e) {
    alert("重試任務失敗: " + e.message);
  }
}

function openQueueModal() {
  document.getElementById("queueModal").classList.add("active");
  refreshQueueModal();
}

async function refreshQueueModal() {
  const queueList = document.getElementById("queueList");
  const openQueueBtn = document.getElementById("openQueueBtn");
  const queueBadge = document.getElementById("queueBadge");
  const queueIcon = document.getElementById("queueIcon");
  const queueLabel = document.getElementById("queueLabel");
  const queueMiniBar = document.getElementById("queueMiniBar");
  const queueMiniProgress = document.getElementById("queueMiniProgress");

  try {
    const res = await fetch(`${BASE_PATH}/api/crawler/jobs`);
    const jobs = await res.json();

    cachedJobsByMd5.clear();
    for (const j of jobs || []) {
      if (j.md5) {
        cachedJobsByMd5.set(j.md5.toLowerCase(), j);
      }
    }

    const allJobs = jobs || [];
    const totalCount = allJobs.length;
    const activeJobs = allJobs.filter(j => j.status === "queued" || j.status === "downloading" || j.status === "ingesting");
    const activeCount = activeJobs.length;
    queueBadge.innerText = totalCount;

    // 動態頂部 Header 微進度指示器
    let avgProgress = 0;
    let statusText = "收書佇列";
    if (activeCount > 0) {
      openQueueBtn.classList.add("downloading");
      queueIcon.classList.add("pulse-anim");
      queueMiniBar.style.display = "block";
      
      const downloadingJob = activeJobs.find(j => j.status === "downloading") || activeJobs[0];
      avgProgress = downloadingJob ? downloadingJob.progress_percent : 0;
      queueMiniProgress.style.width = `${avgProgress}%`;
      statusText = downloadingJob.status === "downloading" ? `下載中 (${avgProgress}%)` : `排隊中`;
      openQueueBtn.title = `收書佇列：${statusText}`;
      if (queueLabel) queueLabel.innerText = statusText;
    } else {
      openQueueBtn.classList.remove("downloading");
      queueIcon.classList.remove("pulse-anim");
      queueMiniBar.style.display = "none";
      openQueueBtn.title = `收書佇列 (${totalCount})`;
      if (queueLabel) queueLabel.innerText = `收書佇列`;
    }

    try {
      localStorage.setItem("cms_queue_count", totalCount);
      localStorage.setItem("cms_queue_active_count", activeCount);
      localStorage.setItem("cms_queue_label", statusText);
      localStorage.setItem("cms_queue_progress", avgProgress);
    } catch (e) {}

    // 若首頁當前正在呈現檢索結果，即時同步更新各卡片按鈕與狀態
    if (currentResults && currentResults.length > 0 && document.getElementById("resultsHeader").style.display !== "none") {
      applySortAndRender();
    }

    if (!jobs || jobs.length === 0) {
      queueList.innerHTML = '<p style="color: var(--text-secondary); text-align: center; padding: 1.5rem;">目前無下載任務</p>';
      return;
    }

    queueList.innerHTML = jobs.map(j => {
      let statusIconHtml = "";
      if (j.status === "downloading") {
        statusIconHtml = `<span class="queue-status-downloading" style="font-size: 0.95rem; font-weight: 700; color: var(--accent);">${j.progress_percent}%</span>`;
      } else if (j.status === "paused") {
        statusIconHtml = `<span class="queue-status-paused" style="font-size: 0.9rem; font-weight: 600; color: var(--text-muted);">${j.progress_percent}%</span>`;
      } else if (j.status === "ingesting") {
        statusIconHtml = `<span class="queue-status-ingesting pulse-anim" style="font-size: 1.05rem;" title="落地入庫中">⚙️</span>`;
      } else if (j.status === "completed") {
        statusIconHtml = `<span class="queue-status-completed" style="font-size: 1.05rem;" title="已完成並落地本地">✅</span>`;
      } else if (j.status === "failed") {
        statusIconHtml = `<span class="queue-status-failed" style="font-size: 1.05rem;" title="${escapeHtml(j.error_message || '下載失敗')}">❌</span>`;
      } else {
        // queued 狀態保持空白無多餘圖示
        statusIconHtml = "";
      }

      const sizeInfo = j.total_bytes > 0 ? `${(j.downloaded_bytes / (1024 * 1024)).toFixed(1)} / ${(j.total_bytes / (1024 * 1024)).toFixed(1)} MB` : "";

      return `
        <div class="queue-item" style="padding: 0.85rem 1rem;">
          <div style="display: flex; justify-content: space-between; align-items: center; font-weight: 700; margin-bottom: 0.25rem;">
            <span style="max-width: 72%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(j.title)}">《${escapeHtml(j.title)}》</span>
            ${statusIconHtml}
          </div>
          <div style="font-size: 0.8rem; color: var(--text-secondary); display: flex; justify-content: space-between; margin-bottom: 0.5rem;">
            <span>MD5: ${j.md5}</span>
            <span>${sizeInfo}</span>
          </div>
          <div style="display: flex; align-items: center; gap: 0.75rem;">
            <div class="progress-bar-bg" style="flex: 1; margin: 0;">
              <div class="progress-bar-fill" style="width: ${j.progress_percent}%;"></div>
            </div>
            <div style="display: flex; align-items: center; gap: 0.35rem; flex-shrink: 0;">
              ${j.status === "queued" ? `
                <button class="btn btn-outline" style="padding: 0.25rem 0.55rem; font-size: 0.95rem; line-height: 1;" onclick="startJob('${j.job_id}')" title="開始下載">▶️</button>
              ` : ""}
              ${j.status === "downloading" ? `
                <button class="btn btn-outline" style="padding: 0.25rem 0.55rem; font-size: 0.95rem; line-height: 1;" onclick="pauseJob('${j.job_id}')" title="暫停">⏸️</button>
              ` : ""}
              ${j.status === "paused" ? `
                <button class="btn btn-outline" style="padding: 0.25rem 0.55rem; font-size: 0.95rem; line-height: 1;" onclick="resumeJob('${j.job_id}')" title="繼續">▶️</button>
              ` : ""}
              ${j.status === "failed" ? `
                <button class="btn btn-outline" style="padding: 0.25rem 0.55rem; font-size: 0.95rem; line-height: 1;" onclick="retryJob('${j.job_id}')" title="重試">🔄</button>
              ` : ""}
              ${j.status !== "completed" ? `
                <button class="btn btn-outline" style="padding: 0.25rem 0.55rem; font-size: 0.95rem; line-height: 1; color: #ef4444; border-color: rgba(239,68,68,0.4);" onclick="deleteJob('${j.job_id}')" title="刪除">🗑️</button>
              ` : ""}
              ${j.status === "completed" && j.work_id ? `
                <button class="btn btn-primary" style="padding: 0.25rem 0.65rem; font-size: 0.95rem; line-height: 1;" onclick="openReader('${j.work_id}')" title="線上閱讀">📖</button>
              ` : ""}
            </div>
          </div>
        </div>
      `;
    }).join("");
  } catch (err) {
    queueList.innerHTML = `<div style="color:#ef4444;">更新任務失敗: ${err.message}</div>`;
  }
}

function startQueuePolling() {
  if (queuePollInterval) clearInterval(queuePollInterval);
  queuePollInterval = setInterval(async () => {
    try {
      await refreshQueueModal();
    } catch (e) {}
  }, 2500);
}

function getFormatTag(format) {
  if (format === "pdf_born_digital") return '<span class="tag tag-pdf-born">原生 PDF</span>';
  if (format === "pdf_scanned") return '<span class="tag tag-pdf-scan">掃描件 PDF</span>';
  if (format === "epub") return '<span class="tag tag-epub">EPUB</span>';
  return '<span class="tag tag-pdf-born">PDF</span>';
}

async function openDetail(workId) {
  const modal = document.getElementById("detailModal");
  const content = document.getElementById("detailModalContent");
  content.innerHTML = '<div style="padding:2rem; text-align:center;">載入中...</div>';
  modal.classList.add("active");

  try {
    const res = await fetch(`${BASE_PATH}/api/works/${workId}`);
    const data = await res.json();

    const idsHtml = data.identifiers.map(id => `
      <div style="font-size:0.85rem; margin-bottom: 0.25rem;">
        <strong style="color:var(--accent);">${id.scheme.toUpperCase()}:</strong> 
        <span style="font-family:monospace;">${id.value}</span> (${id.confidence})
      </div>
    `).join("");

    content.innerHTML = `
      <h3 style="font-size:1.35rem; margin-bottom:0.75rem;">${escapeHtml(data.title)}</h3>
      <p style="color:var(--text-secondary); margin-bottom:1rem;">作者: ${escapeHtml(data.authors_display || "未知")}</p>
      
      <div style="background:var(--bg-primary); padding:1rem; border-radius:8px; margin-bottom:1rem;">
        <h4 style="font-size:0.95rem; margin-bottom:0.5rem; color:var(--text-primary);">識別碼 (Identifiers)</h4>
        ${idsHtml || '<p style="color:var(--text-muted);">無外部識別碼</p>'}
      </div>

      <div style="display:flex; gap:0.75rem; margin-top:1.5rem;">
        <button class="btn btn-primary" onclick="openReader('${data.work_id}')">開啟閱讀器</button>
        <button class="btn btn-secondary" onclick="viewPureText('${data.work_id}')">檢視純文字</button>
      </div>
    `;
  } catch (err) {
    content.innerHTML = `<p style="color:#ef4444;">載入失敗: ${err.message}</p>`;
  }
}

function previewLiveDetail(md5) {
  const item = currentResults.find(r => r.md5 === md5);
  if (!item) return;

  const modal = document.getElementById("detailModal");
  const content = document.getElementById("detailModalContent");
  modal.classList.add("active");

  const mirrorsHtml = (item.mirror_links || []).map(link => `
    <li style="margin-bottom: 0.35rem;"><a href="${link}" target="_blank" style="color: var(--accent);">${escapeHtml(link)}</a></li>
  `).join("");

  content.innerHTML = `
    <h3 style="font-size:1.35rem; margin-bottom:0.75rem;">${escapeHtml(item.title)}</h3>
    <p style="color:var(--text-secondary); margin-bottom:0.5rem;">作者: ${escapeHtml(item.authors_display || "未知")}</p>
    <p style="color:var(--text-secondary); margin-bottom:1rem;">出版社: ${escapeHtml(item.publisher || "未知")} (${item.publication_year || "未知年份"})</p>
    
    <div style="background:var(--bg-primary); padding:1rem; border-radius:8px; margin-bottom:1rem;">
      <h4 style="font-size:0.95rem; margin-bottom:0.5rem; color:var(--text-primary);">MD5 指紋</h4>
      <p style="font-family:monospace; color:var(--text-muted);">${item.md5}</p>
    </div>

    <div style="background:var(--bg-primary); padding:1rem; border-radius:8px; margin-bottom:1.5rem;">
      <h4 style="font-size:0.95rem; margin-bottom:0.5rem; color:var(--text-primary);">公網鏡像來源列表</h4>
      <ul style="padding-left: 1.25rem; font-size: 0.85rem;">
        ${mirrorsHtml || '<li style="color:var(--text-muted);">無公網鏡像標註</li>'}
      </ul>
    </div>

    <button class="btn btn-primary" style="width: 100%; justify-content: center;" onclick="triggerSingleDownload('${item.md5}'); document.getElementById('detailModal').classList.remove('active');">📥 立即鏡像下載至本地</button>
  `;
}

async function viewPureText(workId) {
  const res = await fetch(`${BASE_PATH}/api/works/${workId}/content`);
  const text = await res.text();
  const content = document.getElementById("detailModalContent");
  content.innerHTML = `
    <h4 style="margin-bottom:0.75rem;">抽取的純文字 Markdown</h4>
    <pre style="background:var(--bg-primary); padding:1rem; border-radius:8px; max-height:400px; overflow:auto; font-size:0.85rem; white-space:pre-wrap;">${escapeHtml(text || "（尚無純文字）")}</pre>
  `;
}

async function startJob(jobId) {
  try {
    await fetch(`${BASE_PATH}/api/crawler/jobs/${jobId}/start`, { method: "POST" });
    refreshQueueModal();
  } catch (err) {
    console.error("啟動失敗:", err);
  }
}

async function pauseJob(jobId) {
  try {
    await fetch(`${BASE_PATH}/api/crawler/jobs/${jobId}/pause`, { method: "POST" });
    refreshQueueModal();
  } catch (err) {
    console.error("暫停失敗:", err);
  }
}

async function resumeJob(jobId) {
  try {
    await fetch(`${BASE_PATH}/api/crawler/jobs/${jobId}/resume`, { method: "POST" });
    refreshQueueModal();
  } catch (err) {
    console.error("繼續失敗:", err);
  }
}

async function deleteJob(jobId) {
  try {
    await fetch(`${BASE_PATH}/api/crawler/jobs/${jobId}/delete`, { method: "POST" });
    refreshQueueModal();
  } catch (err) {
    console.error("刪除失敗:", err);
  }
}

async function retryJob(jobId) {
  try {
    await fetch(`${BASE_PATH}/api/crawler/jobs/${jobId}/retry`, { method: "POST" });
    refreshQueueModal();
  } catch (err) {
    console.error("重試失敗:", err);
  }
}

function openReader(workId) {
  window.open(`${BASE_PATH}/reader?work_id=${workId}`, "_blank");
}

function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function initQueueModalDragAndResize() {
  const card = document.getElementById("queueModalCard");
  const header = document.getElementById("queueModalHeader");
  if (!card || !header) return;

  const STORAGE_KEY = "cms_queue_modal_pos";

  function loadBounds() {
    try {
      const savedStr = localStorage.getItem(STORAGE_KEY);
      if (!savedStr) return;
      const saved = JSON.parse(savedStr);
      if (saved && typeof saved.left === "number" && typeof saved.top === "number") {
        const left = Math.max(10, Math.min(window.innerWidth - 120, saved.left));
        const top = Math.max(10, Math.min(window.innerHeight - 100, saved.top));
        card.style.position = "fixed";
        card.style.left = left + "px";
        card.style.top = top + "px";
        card.style.margin = "0";

        if (saved.width && saved.height) {
          const width = Math.max(380, Math.min(window.innerWidth - 20, saved.width));
          const height = Math.max(250, Math.min(window.innerHeight - 20, saved.height));
          card.style.width = width + "px";
          card.style.height = height + "px";
        }
      }
    } catch (e) {}
  }

  function saveBounds() {
    const rect = card.getBoundingClientRect();
    const data = {
      left: rect.left,
      top: rect.top,
      width: rect.width,
      height: rect.height
    };
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    } catch (e) {}
  }

  // 載入既有記憶偏好
  loadBounds();

  // 拖曳功能
  let isDragging = false;
  let startX = 0, startY = 0;
  let initialLeft = 0, initialTop = 0;

  header.addEventListener("mousedown", (e) => {
    if (e.target.closest("button")) return;

    isDragging = true;
    const rect = card.getBoundingClientRect();
    startX = e.clientX;
    startY = e.clientY;
    initialLeft = rect.left;
    initialTop = rect.top;

    card.style.position = "fixed";
    card.style.left = initialLeft + "px";
    card.style.top = initialTop + "px";
    card.style.margin = "0";

    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    e.preventDefault();
  });

  function onMouseMove(e) {
    if (!isDragging) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;

    const newLeft = Math.max(0, Math.min(window.innerWidth - card.offsetWidth, initialLeft + dx));
    const newTop = Math.max(0, Math.min(window.innerHeight - card.offsetHeight, initialTop + dy));

    card.style.left = newLeft + "px";
    card.style.top = newTop + "px";
  }

  function onMouseUp() {
    if (!isDragging) return;
    isDragging = false;
    document.removeEventListener("mousemove", onMouseMove);
    document.removeEventListener("mouseup", onMouseUp);
    saveBounds();
  }

  // 視窗尺寸拉伸監聽
  if (window.ResizeObserver) {
    let resizeTimer = null;
    const ro = new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        saveBounds();
      }, 250);
    });
    ro.observe(card);
  }
}

// === Chrome 擴充套件橋樑 (Chrome Extension Bridge) ===
let isChromeExtensionAvailable = false;
const extensionCallbacks = new Map();

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data || data.source !== "CMS_EXTENSION") return;

  if (data.action === "READY" || data.action === "PING_RESPONSE") {
    isChromeExtensionAvailable = true;
    console.log("[CMS Extension] 原生 Chrome 書籤擴充套件已連線 (v" + (data.version || "1.0.0") + ")");
    updateExtensionStatusIndicator();
  }

  if (data.requestId && extensionCallbacks.has(data.requestId)) {
    const resolve = extensionCallbacks.get(data.requestId);
    extensionCallbacks.delete(data.requestId);
    resolve(data.response);
  }
});

function callExtension(action, payload = {}) {
  return new Promise((resolve) => {
    if (!isChromeExtensionAvailable && action !== "PING") {
      resolve({ success: false, error: "Extension not available" });
      return;
    }
    const requestId = "req_" + Date.now() + "_" + Math.random().toString(36).substr(2, 6);
    extensionCallbacks.set(requestId, resolve);
    window.postMessage({
      source: "CMS_WEB_APP",
      action: action,
      requestId: requestId,
      payload: payload
    }, "*");

    setTimeout(() => {
      if (extensionCallbacks.has(requestId)) {
        extensionCallbacks.delete(requestId);
        resolve({ success: false, error: "Timeout" });
      }
    }, 3000);
  });
}

function updateExtensionStatusIndicator() {
  const badge = document.getElementById("extStatusBadge");
  if (badge) {
    badge.innerHTML = `<span style="color: #34d399; font-weight: 600;">🟢 Chrome 原生書籤已連線同步</span>`;
  }
}

// 頁面載入時自動探測擴充套件
setTimeout(() => {
  callExtension("PING");
}, 150);

// === 個人書單 (Personal Collections) ===
let currentActiveCollectionId = null;
let quickTargetWorkId = null;
let quickTargetWorkTitle = null;

async function openCollectionsModal(targetColId = null) {
  const modal = document.getElementById("collectionsModal");
  modal.classList.add("active");
  await loadCollectionsList(targetColId);
}

function closeCollectionsModal() {
  document.getElementById("collectionsModal").classList.remove("active");
}

async function loadCollectionsList(selectColId = null) {
  const sidebar = document.getElementById("collectionsSidebar");

  // 若已安裝 Chrome 擴充套件，優先走 Chrome 原生書籤樹
  if (isChromeExtensionAvailable) {
    try {
      const extRes = await callExtension("GET_TREE");
      if (extRes.success && extRes.data) {
        const rootTree = extRes.data;
        const folders = (rootTree.children || []).filter(node => !node.url);

        if (folders.length === 0) {
          sidebar.innerHTML = `<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem;">Chrome「CMS圖書館」書籤資料夾為空</p>`;
          return;
        }

        const activeId = selectColId || currentActiveCollectionId || folders[0].id;
        currentActiveCollectionId = activeId;

        sidebar.innerHTML = folders.map(f => {
          const count = (f.children || []).length;
          return `
            <div class="collection-sidebar-item ${f.id === activeId ? 'active' : ''}" onclick="selectCollection('${f.id}')">
              <span style="display: flex; align-items: center; gap: 0.4rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                <span>📁</span>
                <span>${escapeHtml(f.title)}</span>
              </span>
              <span class="collection-count-badge">${count}</span>
            </div>
          `;
        }).join("");

        renderChromeFolderDetail(folders.find(f => f.id === activeId) || folders[0]);
        return;
      }
    } catch (e) {
      console.warn("讀取 Chrome 書籤樹失敗，切換為後端/本地模式:", e);
    }
  }

  // 預設後端/本地資料庫模式
  try {
    const res = await fetch(`${BASE_PATH}/api/collections`);
    if (!res.ok) return;
    const collections = await res.json();
    
    if (collections.length === 0) {
      sidebar.innerHTML = `<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem;">尚未建立自訂書單</p>`;
      document.getElementById("collectionMainView").innerHTML = `
        <div style="text-align: center; color: var(--text-muted); padding: 3rem;">
          <p style="font-size: 1.2rem;">點擊右上角 ➕ 建立您的第一個書單</p>
        </div>
      `;
      return;
    }

    const activeId = selectColId || currentActiveCollectionId || collections[0].collection_id;
    currentActiveCollectionId = activeId;

    sidebar.innerHTML = collections.map(c => `
      <div class="collection-sidebar-item ${c.collection_id === activeId ? 'active' : ''}" onclick="selectCollection('${c.collection_id}')">
        <span style="display: flex; align-items: center; gap: 0.4rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
          <span>${c.icon || '📚'}</span>
          <span>${escapeHtml(c.name)}</span>
        </span>
        <span class="collection-count-badge">${c.items_count}</span>
      </div>
    `).join("");

    await loadCollectionDetail(activeId);
  } catch (err) {
    console.error("載入書單失敗:", err);
  }
}

function renderChromeFolderDetail(folderNode) {
  const mainView = document.getElementById("collectionMainView");
  if (!folderNode) return;
  const items = folderNode.children || [];

  mainView.innerHTML = `
    <div style="display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--border); padding-bottom: 0.75rem; margin-bottom: 1rem;">
      <div>
        <h3 style="font-size: 1.3rem; display: flex; align-items: center; gap: 0.4rem;">
          <span>📁</span>
          <span>${escapeHtml(folderNode.title)}</span>
        </h3>
        <p style="font-size: 0.85rem; color: var(--text-muted); margin-top: 0.25rem;">Chrome 原生書籤資料夾 • 共 ${items.length} 筆書籤</p>
      </div>
    </div>

    <div style="display: flex; flex-direction: column; gap: 0.75rem;">
      ${items.length === 0 ? `
        <div style="text-align: center; color: var(--text-muted); padding: 3rem;">
          <p>此 Chrome 資料夾目前為空</p>
          <p style="font-size: 0.85rem; margin-top: 0.5rem;">在搜尋或逛書架時，點擊書籍卡片上的 ⭐ 即可加入</p>
        </div>
      ` : items.map(it => `
        <div class="book-card" style="margin-bottom: 0; align-items: center;">
          <div class="book-main">
            <div class="book-title">${escapeHtml(it.title)}</div>
            <div class="book-meta">
              <span class="tag tag-local">🌐 Chrome 原生書籤</span>
              <span style="font-size: 0.75rem; color: var(--text-muted);">${escapeHtml(it.url)}</span>
            </div>
          </div>
          <div class="book-actions">
            <a class="btn btn-primary" href="${it.url}" target="_blank" title="立即閱讀" style="padding: 0.4rem 0.75rem; font-size: 1.1rem; text-decoration: none;">📖</a>
            <button class="btn btn-outline" onclick="removeChromeBookmark('${it.id}')" title="刪除書籤" style="padding: 0.4rem 0.75rem; font-size: 1.1rem; color: #ef4444;">❌</button>
          </div>
        </div>
      `).join("")}
    </div>
  `;
}

async function removeChromeBookmark(bookmarkId) {
  await callExtension("REMOVE_BOOKMARK", { bookmarkId });
  await loadCollectionsList(currentActiveCollectionId);
}

async function selectCollection(collectionId) {
  currentActiveCollectionId = collectionId;
  const items = document.querySelectorAll(".collection-sidebar-item");
  items.forEach(el => el.classList.remove("active"));
  await loadCollectionsList(collectionId);
}

async function loadCollectionDetail(collectionId) {
  const mainView = document.getElementById("collectionMainView");
  try {
    const res = await fetch(`${BASE_PATH}/api/collections/${collectionId}`);
    if (!res.ok) return;
    const col = await res.json();

    const isSystem = col.is_system === 1;
    mainView.innerHTML = `
      <div style="display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--border); padding-bottom: 0.75rem; margin-bottom: 1rem;">
        <div>
          <h3 style="font-size: 1.3rem; display: flex; align-items: center; gap: 0.4rem;">
            <span>${col.icon || '📚'}</span>
            <span>${escapeHtml(col.name)}</span>
          </h3>
          <p style="font-size: 0.85rem; color: var(--text-muted); margin-top: 0.25rem;">${escapeHtml(col.description || '自訂書單')} • 共 ${col.items.length} 本書籍</p>
        </div>
        <div style="display: flex; gap: 0.5rem;">
          <button class="btn btn-outline" onclick="renameCollectionPrompt('${col.collection_id}', '${escapeHtml(col.name)}')" title="重命名書單" style="padding: 0.35rem 0.65rem;">✏️</button>
          ${!isSystem ? `<button class="btn btn-outline" onclick="deleteCollectionPrompt('${col.collection_id}')" title="刪除此書單" style="padding: 0.35rem 0.65rem; color: #ef4444;">🗑️</button>` : ''}
        </div>
      </div>

      <div style="display: flex; flex-direction: column; gap: 0.75rem;">
        ${col.items.length === 0 ? `
          <div style="text-align: center; color: var(--text-muted); padding: 3rem;">
            <p>書單目前為空</p>
            <p style="font-size: 0.85rem; margin-top: 0.5rem;">在搜尋或逛書架時，點擊書籍卡片上的 ⭐ 即可加入</p>
          </div>
        ` : col.items.map(it => `
          <div class="book-card" style="margin-bottom: 0;">
            <div class="book-main">
              <div class="book-title">${escapeHtml(it.work.title)}</div>
              <div class="book-meta">
                <span class="tag tag-local">💾 本地</span>
                ${getFormatTag(it.work.format)}
                <span>✍️ ${escapeHtml(it.work.authors_display || "未知作者")}</span>
                ${it.work.publication_year ? `<span>• ${it.work.publication_year}年</span>` : ''}
              </div>
            </div>
            <div class="book-actions">
              <button class="btn btn-primary" onclick="openReader('${it.work_id}')" title="立即閱讀" style="padding: 0.4rem 0.75rem; font-size: 1.1rem;">📖</button>
              <button class="btn btn-outline" onclick="removeBookFromCollection('${col.collection_id}', '${it.work_id}')" title="從書單移除" style="padding: 0.4rem 0.75rem; font-size: 1.1rem; color: #ef4444;">❌</button>
            </div>
          </div>
        `).join("")}
      </div>
    `;
  } catch (err) {
    console.error("載入書單詳情失敗:", err);
  }
}

async function createNewCollectionPrompt() {
  const name = prompt("請輸入新書單名稱（例如：科幻經典、待讀清單）：");
  if (!name || !name.trim()) return;
  try {
    const res = await fetch(`${BASE_PATH}/api/collections`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), icon: "📚" })
    });
    if (res.ok) {
      const data = await res.json();
      await loadCollectionsList(data.collection_id);
    }
  } catch (err) {
    console.error("建立書單失敗:", err);
  }
}

async function renameCollectionPrompt(colId, currentName) {
  const newName = prompt("請輸入書單新名稱：", currentName);
  if (!newName || !newName.trim() || newName.trim() === currentName) return;
  try {
    const res = await fetch(`${BASE_PATH}/api/collections/${colId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName.trim() })
    });
    if (res.ok) {
      await loadCollectionsList(colId);
    }
  } catch (err) {
    console.error("重命名失敗:", err);
  }
}

async function deleteCollectionPrompt(colId) {
  if (!confirm("確定要刪除此書單嗎？（不會刪除書籍本體）")) return;
  try {
    const res = await fetch(`${BASE_PATH}/api/collections/${colId}`, {
      method: "DELETE"
    });
    if (res.ok) {
      currentActiveCollectionId = null;
      await loadCollectionsList();
    }
  } catch (err) {
    console.error("刪除失敗:", err);
  }
}

async function removeBookFromCollection(colId, workId) {
  try {
    const res = await fetch(`${BASE_PATH}/api/collections/${colId}/items/${workId}`, {
      method: "DELETE"
    });
    if (res.ok) {
      await loadCollectionsList(colId);
    }
  } catch (err) {
    console.error("移除書籍失敗:", err);
  }
}

// === 快速加入書單 Popover ===
async function openQuickCollection(workId, title) {
  quickTargetWorkId = workId;
  quickTargetWorkTitle = title;
  const modal = document.getElementById("quickCollectionModal");
  modal.classList.add("active");
  const listEl = document.getElementById("quickCollectionList");
  listEl.innerHTML = `<p style="color: var(--text-muted); text-align: center; padding: 1rem;">載入書單中...</p>`;

  // 1. 若 Chrome 擴充套件已連線，讀取 Chrome 資料夾
  if (isChromeExtensionAvailable) {
    try {
      const extRes = await callExtension("GET_TREE");
      if (extRes.success && extRes.data) {
        const rootTree = extRes.data;
        const folders = (rootTree.children || []).filter(node => !node.url);
        const workUrl = `${window.location.origin}${BASE_PATH}/reader?work_id=${workId}`;

        listEl.innerHTML = folders.map(f => {
          const isChecked = (f.children || []).some(b => b.url && b.url.includes(workId));
          return `
            <label class="quick-col-row">
              <span style="display: flex; align-items: center; gap: 0.5rem;">
                <span>📁</span>
                <span style="font-weight: 600;">${escapeHtml(f.title)}</span>
              </span>
              <input type="checkbox" class="quick-col-checkbox" data-folder-id="${f.id}" data-folder-name="${escapeHtml(f.title)}" ${isChecked ? 'checked' : ''} style="width: 18px; height: 18px; cursor: pointer;">
            </label>
          `;
        }).join("");
        return;
      }
    } catch (e) {
      console.warn("讀取 Chrome 資料夾失敗，切換後端模式:", e);
    }
  }

  // 2. 預設後端/本地資料庫模式
  try {
    const [colsRes, statusRes] = await Promise.all([
      fetch(`${BASE_PATH}/api/collections`),
      fetch(`${BASE_PATH}/api/collections/work/${workId}/status`)
    ]);

    const collections = await colsRes.json();
    const joinedIds = new Set(await statusRes.json());

    listEl.innerHTML = collections.map(c => {
      const isChecked = joinedIds.has(c.collection_id);
      return `
        <label class="quick-col-row">
          <span style="display: flex; align-items: center; gap: 0.5rem;">
            <span>${c.icon || '📚'}</span>
            <span style="font-weight: 600;">${escapeHtml(c.name)}</span>
          </span>
          <input type="checkbox" class="quick-col-checkbox" data-col-id="${c.collection_id}" ${isChecked ? 'checked' : ''} style="width: 18px; height: 18px; cursor: pointer;">
        </label>
      `;
    }).join("");
  } catch (err) {
    console.error("載入快速收藏失敗:", err);
    listEl.innerHTML = `<p style="color: #ef4444;">載入失敗</p>`;
  }
}

async function saveQuickCollections() {
  if (!quickTargetWorkId) return;
  const checkboxes = document.querySelectorAll(".quick-col-checkbox");

  if (isChromeExtensionAvailable) {
    const workUrl = `${window.location.origin}${BASE_PATH}/reader?work_id=${quickTargetWorkId}`;
    for (const cb of checkboxes) {
      const folderId = cb.dataset.folderId;
      const folderName = cb.dataset.folderName;
      if (cb.checked) {
        await callExtension("ADD_BOOKMARK", {
          title: quickTargetWorkTitle || "書籍",
          url: workUrl,
          folderId: folderId,
          folderName: folderName
        });
      } else {
        await callExtension("REMOVE_BOOKMARK", {
          url: workUrl
        });
      }
    }
  } else {
    const promises = [];
    for (const cb of checkboxes) {
      const colId = cb.dataset.colId;
      if (cb.checked) {
        promises.push(fetch(`${BASE_PATH}/api/collections/${colId}/items`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ work_id: quickTargetWorkId })
        }));
      } else {
        promises.push(fetch(`${BASE_PATH}/api/collections/${colId}/items/${quickTargetWorkId}`, {
          method: "DELETE"
        }));
      }
    }
    await Promise.all(promises);
  }

  document.getElementById("quickCollectionModal").classList.remove("active");
  quickTargetWorkId = null;
  quickTargetWorkTitle = null;
}

// === 逛線上書攤 (Online Bookstalls & Tree Browsing) ===
let currentActiveCategoryId = "cat_800";

async function openBookstallModal() {
  const modal = document.getElementById("bookstallModal");
  modal.classList.add("active");
  await loadCategoryTree();
  await loadShelfWorks(currentActiveCategoryId, "文學與小說", "📚", "文學與小說");
}

function closeBookstallModal() {
  document.getElementById("bookstallModal").classList.remove("active");
}

async function loadCategoryTree() {
  const rootEl = document.getElementById("categoryTreeRoot");
  try {
    const res = await fetch(`${BASE_PATH}/api/categories/tree`);
    if (!res.ok) return;
    const tree = await res.json();

    rootEl.innerHTML = tree.map(node => renderTreeNode(node)).join("");
  } catch (err) {
    console.error("載入分類樹失敗:", err);
  }
}

function renderTreeNode(node, parentPath = "") {
  const hasChildren = node.children && node.children.length > 0;
  const currentPath = parentPath ? `${parentPath} > ${node.name}` : node.name;

  return `
    <div class="tree-node" id="node_${node.category_id}">
      <div class="tree-header ${node.category_id === currentActiveCategoryId ? 'active' : ''}" 
           onclick="handleCategoryClick('${node.category_id}', '${escapeHtml(node.name)}', '${node.icon}', '${escapeHtml(currentPath)}')">
        <div style="display: flex; align-items: center; gap: 0.35rem; overflow: hidden;">
          ${hasChildren ? `<span class="tree-expander expanded" onclick="event.stopPropagation(); toggleTreeNode('${node.category_id}')">▶</span>` : `<span style="width: 1.2rem;"></span>`}
          <span>${node.icon || '📖'}</span>
          <span style="font-size: 0.9rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(node.name)}</span>
        </div>
        <span class="tree-badge">${node.works_count}</span>
      </div>
      ${hasChildren ? `
        <div class="tree-children" id="children_${node.category_id}">
          ${node.children.map(child => renderTreeNode(child, currentPath)).join("")}
        </div>
      ` : ''}
    </div>
  `;
}

function toggleTreeNode(catId) {
  const childrenEl = document.getElementById(`children_${catId}`);
  const nodeEl = document.getElementById(`node_${catId}`);
  const expander = nodeEl.querySelector(".tree-expander");
  if (!childrenEl || !expander) return;

  if (childrenEl.style.display === "none") {
    childrenEl.style.display = "block";
    expander.classList.add("expanded");
  } else {
    childrenEl.style.display = "none";
    expander.classList.remove("expanded");
  }
}

async function handleCategoryClick(catId, name, icon, breadcrumbs) {
  currentActiveCategoryId = catId;
  document.querySelectorAll(".tree-header").forEach(el => el.classList.remove("active"));
  const activeHeader = document.querySelector(`#node_${catId} > .tree-header`);
  if (activeHeader) activeHeader.classList.add("active");

  await loadShelfWorks(catId, name, icon, breadcrumbs);
}

async function loadShelfWorks(catId, name, icon, breadcrumbs) {
  document.getElementById("shelfBreadcrumbs").innerText = breadcrumbs;
  document.getElementById("shelfTitle").innerHTML = `${icon || '📖'} ${escapeHtml(name)}`;
  const shelfGrid = document.getElementById("shelfGrid");
  shelfGrid.innerHTML = `<p style="color: var(--text-muted); padding: 2rem; grid-column: 1 / -1; text-align: center;">載入書架藏書中...</p>`;

  try {
    const res = await fetch(`${BASE_PATH}/api/categories/${catId}/works?page=1&page_size=50`);
    if (!res.ok) return;
    const data = await res.json();

    if (data.items.length === 0) {
      shelfGrid.innerHTML = `
        <div style="grid-column: 1 / -1; text-align: center; color: var(--text-muted); padding: 3rem;">
          <p style="font-size: 1.1rem;">此書架目前尚無藏書</p>
          <p style="font-size: 0.85rem; margin-top: 0.5rem;">您可以透過手動上傳或鏡像收書充實此架位典藏</p>
        </div>
      `;
      return;
    }

    shelfGrid.innerHTML = data.items.map(w => `
      <div class="shelf-book-card">
        <div>
          <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.5rem;">
            ${getFormatTag(w.format)}
            <span style="font-size: 0.8rem; color: var(--text-muted);">${w.publication_year ? `${w.publication_year}年` : ''}</span>
          </div>
          <div style="font-weight: 700; font-size: 1rem; line-height: 1.4; color: var(--text-primary); margin-bottom: 0.35rem; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">
            ${escapeHtml(w.title)}
          </div>
          <div style="font-size: 0.82rem; color: var(--text-secondary); margin-bottom: 0.75rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
            ✍️ ${escapeHtml(w.authors_display || "未知作者")}
          </div>
        </div>
        <div style="display: flex; gap: 0.4rem; justify-content: flex-end; border-top: 1px solid var(--border); padding-top: 0.5rem;">
          <button class="btn btn-primary" onclick="openReader('${w.work_id}')" title="線上閱讀" style="padding: 0.35rem 0.65rem; font-size: 1rem;">📖</button>
          <button class="btn btn-secondary" onclick="openQuickCollection('${w.work_id}', '${escapeHtml(w.title)}')" title="加入書單" style="padding: 0.35rem 0.65rem; font-size: 1rem;">⭐</button>
          <button class="btn btn-outline" onclick="openDetail('${w.work_id}')" title="書目詳情" style="padding: 0.35rem 0.65rem; font-size: 1rem;">ℹ️</button>
        </div>
      </div>
    `).join("");
  } catch (err) {
    console.error("載入架位書籍失敗:", err);
    shelfGrid.innerHTML = `<p style="color: #ef4444; padding: 2rem; grid-column: 1 / -1;">載入失敗</p>`;
  }
}

