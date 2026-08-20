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
// 記錄各公網卡片「上一次已渲染」的佇列狀態指紋，供輪詢時做差量比對（避免無條件全量重繪）
let renderedCardSigByMd5 = new Map();

// 全域 Modal / 獨立頁切換管理器（支援頂部導航列直接無縫切換、互斥關閉與高亮）
function closeAllModals() {
  const allModalIds = ["queueModal", "uploadModal", "detailModal", "settingsModal", "collectionsModal", "quickCollectionModal", "bookstallModal"];
  allModalIds.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove("active", "in-detail-view", "in-shelf-view");
  });
  document.querySelectorAll(".header-actions .btn").forEach(b => b.classList.remove("active-nav"));
}

function toggleModal(modalId, openCallback, navBtnId) {
  const modal = document.getElementById(modalId);
  if (!modal) return;
  const isAlreadyActive = modal.classList.contains("active");

  // 1. 關閉所有現有開啟之 Modal
  closeAllModals();

  // 2. 若先前未開啟，則開啟目標 Modal 並啟動對應回呼與高亮
  if (!isAlreadyActive) {
    if (openCallback) openCallback();
    modal.classList.add("active");
    if (navBtnId) {
      const navBtn = document.getElementById(navBtnId);
      if (navBtn) navBtn.classList.add("active-nav");
    }
  }
}

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

  // 點選 Logo 回到首頁並關閉所有 Modal
  const logoContainer = document.querySelector(".logo-container");
  if (logoContainer) {
    logoContainer.addEventListener("click", (e) => {
      e.preventDefault();
      closeAllModals();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  // 下載佇列 Modal 事件（展開、關閉、縮小在背景運作）
  const openQueueBtn = document.getElementById("openQueueBtn");
  const minimizeQueueBtn = document.getElementById("minimizeQueueBtn");
  if (openQueueBtn) {
    openQueueBtn.addEventListener("click", () => toggleModal("queueModal", refreshQueueModal, "openQueueBtn"));
  }
  if (minimizeQueueBtn) {
    minimizeQueueBtn.addEventListener("click", closeAllModals);
  }

  // 點選 modal 以外任何地方自動縮小（僅桌面端有效）
  document.addEventListener("mousedown", (e) => {
    if (window.innerWidth <= 768) return;
    const queueModal = document.getElementById("queueModal");
    if (!queueModal || !queueModal.classList.contains("active")) return;
    const card = document.getElementById("queueModalCard");
    if (card && !card.contains(e.target) && openQueueBtn && !openQueueBtn.contains(e.target)) {
      closeAllModals();
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
  const openUploadBtn = document.getElementById("openUploadBtn");
  const closeUploadBtn = document.getElementById("closeUploadBtn");
  const uploadForm = document.getElementById("uploadForm");
  const fileInput = document.getElementById("fileInput");
  const dropZone = document.getElementById("dropZone");

  if (openUploadBtn) openUploadBtn.addEventListener("click", () => toggleModal("uploadModal", null, "openUploadBtn"));
  if (closeUploadBtn) closeUploadBtn.addEventListener("click", closeAllModals);

  // 系統設定 Modal 事件
  const openSettingsBtn = document.getElementById("openSettingsBtn");
  const closeSettingsBtn = document.getElementById("closeSettingsBtn");
  if (openSettingsBtn) openSettingsBtn.addEventListener("click", () => toggleModal("settingsModal", initSettingsModal, "openSettingsBtn"));
  if (closeSettingsBtn) closeSettingsBtn.addEventListener("click", closeAllModals);

  // 本機下載偏好勾選
  const autoDownloadLocalCheckbox = document.getElementById("autoDownloadLocalCheckbox");
  if (autoDownloadLocalCheckbox) {
    autoDownloadLocalCheckbox.checked = localStorage.getItem("cms_auto_download_local") === "true";
    autoDownloadLocalCheckbox.addEventListener("change", (e) => {
      localStorage.setItem("cms_auto_download_local", e.target.checked);
    });
  }

  // 選取本機儲存資料夾
  const selectLocalDirBtn = document.getElementById("selectLocalDirBtn");
  if (selectLocalDirBtn) {
    selectLocalDirBtn.addEventListener("click", handleSelectLocalDirectory);
  }

  // 自訂 Libgen 來源與預檢驗證按鈕
  const addMirrorBtn = document.getElementById("addMirrorBtn");
  const validateAllMirrorsBtn = document.getElementById("validateAllMirrorsBtn");
  const resetMirrorsBtn = document.getElementById("resetMirrorsBtn");
  if (addMirrorBtn) addMirrorBtn.addEventListener("click", handleAddCustomMirror);
  if (validateAllMirrorsBtn) validateAllMirrorsBtn.addEventListener("click", handleValidateAllMirrors);
  if (resetMirrorsBtn) resetMirrorsBtn.addEventListener("click", handleResetMirrors);

  // 個人書單 Modal 事件
  const openCollectionsBtn = document.getElementById("openCollectionsBtn");
  const closeCollectionsBtn = document.getElementById("closeCollectionsBtn");
  const newCollectionBtn = document.getElementById("newCollectionBtn");
  const exportCollectionsBtn = document.getElementById("exportCollectionsBtn");
  const importCollectionsBtn = document.getElementById("importCollectionsBtn");
  const bookmarkFileInput = document.getElementById("bookmarkFileInput");
  const settingsExportHtmlBtn = document.getElementById("settingsExportHtmlBtn");
  const settingsExportJsonBtn = document.getElementById("settingsExportJsonBtn");
  const settingsImportBtn = document.getElementById("settingsImportBtn");

  if (openCollectionsBtn) openCollectionsBtn.addEventListener("click", () => toggleModal("collectionsModal", openCollectionsModal, "openCollectionsBtn"));
  if (closeCollectionsBtn) closeCollectionsBtn.addEventListener("click", closeAllModals);
  if (newCollectionBtn) newCollectionBtn.addEventListener("click", (e) => createNewCollectionPrompt(e.currentTarget));
  if (exportCollectionsBtn) exportCollectionsBtn.addEventListener("click", (e) => exportCollectionsAsNetscapeHtml(e.currentTarget));
  if (importCollectionsBtn) importCollectionsBtn.addEventListener("click", () => bookmarkFileInput && bookmarkFileInput.click());
  if (settingsExportHtmlBtn) settingsExportHtmlBtn.addEventListener("click", (e) => exportCollectionsAsNetscapeHtml(e.currentTarget));
  if (settingsExportJsonBtn) settingsExportJsonBtn.addEventListener("click", (e) => exportCollectionsAsJson(e.currentTarget));
  if (settingsImportBtn) settingsImportBtn.addEventListener("click", () => bookmarkFileInput && bookmarkFileInput.click());

  if (bookmarkFileInput) {
    bookmarkFileInput.addEventListener("change", async (e) => {
      if (e.target.files && e.target.files.length > 0) {
        await handleImportBookmarkFile(e.target.files[0], importCollectionsBtn);
        e.target.value = "";
      }
    });
  }

  // 快速收藏 Modal 事件
  const closeQuickCollectionBtn = document.getElementById("closeQuickCollectionBtn");
  const saveQuickCollectionBtn = document.getElementById("saveQuickCollectionBtn");
  if (closeQuickCollectionBtn) closeQuickCollectionBtn.addEventListener("click", () => document.getElementById("quickCollectionModal").classList.remove("active"));
  if (saveQuickCollectionBtn) saveQuickCollectionBtn.addEventListener("click", saveQuickCollections);

  // 逛線上書攤 Modal 事件
  const openBookstallBtn = document.getElementById("openBookstallBtn");
  const closeBookstallBtn = document.getElementById("closeBookstallBtn");
  if (openBookstallBtn) openBookstallBtn.addEventListener("click", () => toggleModal("bookstallModal", openBookstallModal, "openBookstallBtn"));
  if (closeBookstallBtn) closeBookstallBtn.addEventListener("click", closeAllModals);

  // 手機端全版獨立頁返回按鈕 (Mobile Back Buttons)
  const bindMobileBack = (btnId, handler) => {
    const btn = document.getElementById(btnId);
    if (btn) btn.addEventListener("click", handler);
  };
  bindMobileBack("queueMobileBackBtn", closeAllModals);
  bindMobileBack("uploadMobileBackBtn", closeAllModals);
  bindMobileBack("detailMobileBackBtn", () => document.getElementById("detailModal").classList.remove("active"));
  bindMobileBack("settingsMobileBackBtn", closeAllModals);
  bindMobileBack("quickMobileBackBtn", () => document.getElementById("quickCollectionModal").classList.remove("active"));
  bindMobileBack("collectionsMobileBackBtn", () => {
    const colModal = document.getElementById("collectionsModal");
    if (colModal.classList.contains("in-detail-view")) {
      colModal.classList.remove("in-detail-view");
    } else {
      closeAllModals();
    }
  });
  bindMobileBack("bookstallMobileBackBtn", () => {
    const bModal = document.getElementById("bookstallModal");
    if (bModal.classList.contains("in-shelf-view")) {
      bModal.classList.remove("in-shelf-view");
    } else {
      closeAllModals();
    }
  });
  bindMobileBack("bookstallBackToTreeBtn", () => {
    document.getElementById("bookstallModal").classList.remove("in-shelf-view");
  });

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
      showCustomAlert({
        title: "提示",
        message: "請先選擇要上傳的 PDF 或 EPUB 檔案！",
        icon: "⚠️",
        type: "warning",
        anchor: document.getElementById("dropZone") || document.getElementById("uploadSubmitBtn")
      });
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
      uploadModal.classList.remove("active");
      uploadForm.reset();
      document.getElementById("fileSelectNotice").innerText = "點選或將檔案拖曳至此處";
      showCustomAlert({
        title: "入庫成功",
        message: `《${data.title}》已成功入庫！`,
        icon: "✅",
        type: "success",
        anchor: document.getElementById("openUploadBtn")
      });
      handleSearch();
    } catch (err) {
      showCustomAlert({
        title: "上傳解析失敗",
        message: `上傳解析失敗: ${err.message}`,
        icon: "❌",
        type: "error",
        anchor: document.getElementById("uploadSubmitBtn")
      });
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

// ===== 統一聚合搜尋核心（漸進式渲染）=====
// 本地與公網兩條非同步流各自獨立落地：本地一回來立刻渲染，公網稍後合併「追加」進列表。
// 硬性要求：公網的三種狀態（檢索中 / 完成但 0 筆 / 檢索失敗）在畫面上必須長得不一樣，
// 不得共用同一個輸出——這正是舊版整頁 spinner + 空 catch 的病灶。
let searchRequestId = 0;
let activeSearch = null;

// 競態守門：使用者連打搜尋時，舊請求的回應一律不得寫入畫面。
function isStaleSearch(state) {
  return !state || state.reqId !== searchRequestId || activeSearch !== state;
}

async function handleSearch() {
  const query = document.getElementById("searchInput").value.trim();
  const bookList = document.getElementById("bookList");
  const resultsHeader = document.getElementById("resultsHeader");
  const selectAllCheckbox = document.getElementById("selectAllCheckbox");

  if (!query) {
    showCustomAlert({
      title: "提示",
      message: "請輸入欲搜尋的書名、作者、ISBN、DOI 或關鍵字！",
      icon: "🔍",
      anchor: document.getElementById("searchBtn") || document.getElementById("searchInput")
    });
    return;
  }

  resultsHeader.style.display = "none";
  selectAllCheckbox.style.display = "none";
  selectAllCheckbox.checked = false;
  selectedMd5s.clear();
  updateBatchBar();
  currentResults = [];
  renderedCardSigByMd5.clear();

  const state = {
    reqId: ++searchRequestId,
    query,
    page: currentPage,
    filters: { format: currentFilters.format, language: currentFilters.language },
    local: { status: "pending", items: [], error: null },
    remote: { status: "pending", items: [], error: null },
    filteredRemote: [],
    localPainted: false
  };
  activeSearch = state;

  bookList.innerHTML = `
    <div class="search-loading-box" data-search-phase="local">
      <div class="spinner-ring"></div>
      <p class="search-loading-text">🔍 正在檢索本地書庫…</p>
    </div>
  `;

  // 兩條流各自獨立推進，刻意不 await——本地不必等公網
  runLocalSearch(state);
  runRemoteSearch(state);
}

async function runLocalSearch(state) {
  try {
    const params = new URLSearchParams({
      q: state.query,
      format: state.filters.format,
      language: state.filters.language,
      page: state.page,
      page_size: 50
    });
    const res = await fetch(`${BASE_PATH}/api/search?${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (isStaleSearch(state)) return;
    state.local.items = data.items || [];
    state.local.status = "done";
  } catch (e) {
    if (isStaleSearch(state)) return;
    state.local.status = "error";
    state.local.error = (e && e.message) ? e.message : String(e);
    state.local.items = [];
  }
  if (isStaleSearch(state)) return;
  renderSearchProgress(state, "local");
}

async function runRemoteSearch(state) {
  try {
    const res = await fetch(`${BASE_PATH}/api/crawler/search?q=${encodeURIComponent(state.query)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (isStaleSearch(state)) return;
    state.remote.items = data.items || [];
    state.remote.status = "done";
  } catch (e) {
    if (isStaleSearch(state)) return;
    state.remote.status = "error";
    state.remote.error = (e && e.message) ? e.message : String(e);
    state.remote.items = [];
  }
  if (isStaleSearch(state)) return;
  state.filteredRemote = filterRemoteItems(state.remote.items, state.local.items, state.filters);
  renderSearchProgress(state, "remote");
}

// 合併與去重：若公網資源中已有本地收錄 (以 MD5 比對)，優先以本地狀態呈現（語意與舊版一致）
function filterRemoteItems(remoteItems, localItems, filters) {
  const localMd5s = new Set((localItems || []).map(i => (i.md5 || "").toLowerCase()).filter(Boolean));
  const filteredRemote = [];

  for (const r of remoteItems || []) {
    const md5 = (r.md5 || "").toLowerCase();
    if (md5 && localMd5s.has(md5)) {
      continue; // 已在本地項目中，免重複顯示
    }
    if (filters.format !== "all" && r.format !== filters.format) {
      continue;
    }
    if (filters.language !== "all") {
      const lang = (r.language || "").toLowerCase();
      if (filters.language === "zh" && !lang.includes("zh") && !lang.includes("chinese")) {
        continue;
      }
      if (filters.language === "en" && !lang.includes("en") && !lang.includes("english")) {
        continue;
      }
    }
    filteredRemote.push(r);
  }
  return filteredRemote;
}

// ---- 漸進式渲染：本地先落地，公網後到者追加 ----
function renderSearchProgress(state, phase) {
  const bookList = document.getElementById("bookList");
  const resultsHeader = document.getElementById("resultsHeader");

  if (phase === "local") {
    // 若公網比本地先回來（罕見但可能），此時才拿得到正確的 local md5 集合，需重算去重
    if (state.remote.status === "done") {
      state.filteredRemote = filterRemoteItems(state.remote.items, state.local.items, state.filters);
    }
    currentResults = [...state.local.items, ...state.filteredRemote];
    resultsHeader.style.display = "flex";
    paintSearchList(state);
    state.localPainted = true;
  } else {
    currentResults = [...state.local.items, ...state.filteredRemote];
    if (!state.localPainted) {
      // 本地都還沒回來，先不動列表主體，只更新狀態列（避免公網內容搶先霸佔畫面）
      updateSearchStatusLine(state);
      return;
    }
    if (currentSort === "relevance" && state.filteredRemote.length > 0) {
      // relevance 排序＝本地優先、其餘維持原序，故「追加」與全量重排結果等價，
      // 但不會把使用者正在看／已勾選／已展開的本地卡片抽掉重畫。
      appendRemoteCards(state);
    } else {
      // 其他排序需與本地項目交錯，無法單純追加，只能重排整批
      paintSearchList(state);
    }
  }

  updateSearchStatusLine(state);
  updateSelectAllVisibility(state);
}

function paintSearchList(state) {
  const bookList = document.getElementById("bookList");
  if (currentResults.length > 0) {
    applySortAndRender();
    return;
  }
  // 零結果：必須等兩邊都落地才敢說「查無」。本地 0 筆 ≠ 查無。
  bookList.innerHTML = buildEmptyOrWaitingHtml(state);
}

function appendRemoteCards(state) {
  const bookList = document.getElementById("bookList");
  const placeholder = bookList.querySelector("[data-search-placeholder]");
  if (placeholder) placeholder.remove();

  const html = state.filteredRemote.map(item => renderLiveBookCard(item)).join("");
  bookList.insertAdjacentHTML("beforeend", html);
  bindCheckboxEvents();
  restoreSelectionState();
  snapshotRenderedCardSignatures();
}

// 三態必須在畫面上可區分：公網「檢索中」／「完成但 0 筆」／「檢索失敗」
function updateSearchStatusLine(state) {
  const totalCountEl = document.getElementById("totalCount");
  const localCount = state.local.items.length;
  const remoteCount = state.filteredRemote.length;
  const parts = [];

  if (state.local.status === "error") {
    parts.push(`<span style="color: var(--danger, #f87171);">⚠️ 本地書庫檢索失敗（${escapeHtml(state.local.error || "未知錯誤")}）</span>`);
  } else if (state.local.status === "pending") {
    parts.push(`<span style="color: var(--text-muted);">💾 本地檢索中…</span>`);
  } else {
    parts.push(`💾 本地已收錄 ${localCount} 本`);
  }

  if (state.remote.status === "pending") {
    parts.push(`<span data-remote-state="pending" style="color: var(--text-muted);">🌐 公網鏡像檢索中<span class="dot-ellipsis">…</span></span>`);
  } else if (state.remote.status === "error") {
    parts.push(`<span data-remote-state="error" style="color: var(--danger, #f87171);">⚠️ 公網鏡像檢索失敗（${escapeHtml(state.remote.error || "未知錯誤")}）</span>`);
  } else {
    parts.push(`<span data-remote-state="done">🌐 公網可收書 ${remoteCount} 本</span>`);
  }

  const settled = state.local.status !== "pending" && state.remote.status !== "pending";
  const prefix = settled
    ? (currentResults.length > 0 ? `聚合找到 ${currentResults.length} 本書籍　` : `查無符合書籍　`)
    : `已找到 ${currentResults.length} 本　`;

  totalCountEl.innerHTML = prefix + parts.join(" ・ ");
}

function updateSelectAllVisibility(state) {
  const selectAllCheckbox = document.getElementById("selectAllCheckbox");
  if (!selectAllCheckbox) return;
  selectAllCheckbox.style.display = state.filteredRemote.length > 0 ? "inline-block" : "none";
}

// 列表主體為空時的畫面：公網仍在跑 → 等待態；兩邊都結束 → 真正的查無／失敗態
function buildEmptyOrWaitingHtml(state) {
  if (state.remote.status === "pending") {
    return `
      <div class="search-loading-box" data-search-placeholder data-search-phase="remote">
        <div class="spinner-ring"></div>
        <p class="search-loading-text">💾 本地書庫尚無符合書籍，🌐 公網鏡像檢索中…</p>
        <p style="color: var(--text-muted); font-size: 0.85rem;">公網鏡像回應較慢，找到的結果會自動出現在此處。</p>
      </div>
    `;
  }

  const failedNote = (state.remote.status === "error" || state.local.status === "error")
    ? `<p style="color: var(--danger, #f87171); font-size: 0.9rem; margin-bottom: 1rem;">⚠️ ${state.local.status === "error" ? "本地書庫" : "公網鏡像"}檢索失敗，結果可能不完整——可稍後重試。</p>`
    : "";

  return `
    <div data-search-placeholder style="text-align:center; padding: 3rem; background: var(--bg-secondary); border-radius: var(--radius);">
      <p style="font-size: 1.1rem; color: var(--text-secondary); margin-bottom: 1rem;">📭 本地與公網鏡像均未找到符合之書籍</p>
      ${failedNote}
      <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1.5rem;">建議嘗試更換關鍵字、英文書名或 ISBN 再次檢索，或直接手動上傳檔案。</p>
      <button class="btn btn-primary" onclick="document.getElementById('openUploadBtn').click()">➕ 手動上傳檔案入庫</button>
    </div>
  `;
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
  restoreSelectionState();
  snapshotRenderedCardSignatures();
}

// 全量重繪後，依全域 selectedMd5s 補回使用者原有的勾選狀態（方案 A：狀態保留防護網）
function restoreSelectionState() {
  const bookList = document.getElementById("bookList");
  if (!bookList) return;

  let visibleCheckedCount = 0;
  const visibleMd5s = new Set();
  bookList.querySelectorAll(".book-select-checkbox").forEach(cb => {
    const md5 = cb.dataset.md5;
    visibleMd5s.add(md5);
    if (selectedMd5s.has(md5)) {
      cb.checked = true;
      visibleCheckedCount++;
    }
  });

  // 已離開可勾選狀態（例如已排入佇列 / 已完成收書）之項目不再屬於批次選取集合
  for (const md5 of Array.from(selectedMd5s)) {
    if (!visibleMd5s.has(md5)) selectedMd5s.delete(md5);
  }

  const selectAllCheckbox = document.getElementById("selectAllCheckbox");
  if (selectAllCheckbox) {
    selectAllCheckbox.checked = visibleMd5s.size > 0 && visibleCheckedCount === visibleMd5s.size;
  }
  updateBatchBar();
}

// 記錄目前畫面上每張公網卡片的狀態指紋，作為下一次輪詢的差量比對基準
function snapshotRenderedCardSignatures() {
  renderedCardSigByMd5.clear();
  for (const item of currentResults || []) {
    const md5Key = (item.md5 || "").toLowerCase();
    if (!md5Key) continue;
    if (item.availability_tier === 0 || (item.work_id && !item.work_id.startsWith("libgen_"))) continue;
    renderedCardSigByMd5.set(md5Key, getLiveCardSignature(item));
  }
}

function renderLocalBookCard(item) {
  const formatTag = getFormatTag(item.format);
  const langTag = item.language ? `<span class="tag tag-lang">${item.language.toUpperCase()}</span>` : "";
  const progressPercent = item.progress_ratio ? Math.round(item.progress_ratio * 100) : 0;
  const sizeMb = item.size_bytes ? (item.size_bytes / (1024 * 1024)).toFixed(1) + " MB" : "";
  const yearText = item.publication_year ? `• ${item.publication_year}年` : "";

  return `
    <div class="book-card">
      <div class="book-card-header">
        <div class="book-indicator-wrap">
          <span style="font-size: 1.25rem;" title="本地已落地">💾</span>
        </div>
        <div class="book-more-wrap">
          <button class="btn btn-icon btn-outline book-more-btn" onclick="toggleBookCardDropdown(this, event)" title="更多操作">
            ⋯
          </button>
          <div class="book-dropdown-menu">
            <button class="book-dropdown-item" onclick="openReader('${item.work_id}'); closeAllBookDropdowns();">
              <span>📖</span> <span>線上閱讀</span>
            </button>
              <button class="book-dropdown-item" onclick="openQuickCollection('${item.work_id}', '${escapeJsArg(item.title)}'); closeAllBookDropdowns();">
              <span>⭐</span> <span>加入個人書單</span>
            </button>
            <a class="book-dropdown-item" href="${BASE_PATH}/api/files/${item.work_id}/raw" download title="下載原檔至本地" onclick="closeAllBookDropdowns();">
              <span>📥</span> <span>下載原檔至本機</span>
            </a>
            <button class="book-dropdown-item" onclick="openDetail('${item.work_id}'); closeAllBookDropdowns();">
              <span>ℹ️</span> <span>書籍元資料詳情</span>
            </button>
          </div>
        </div>
      </div>
      <div class="book-main">
        <div class="book-title">${escapeHtml(item.title)}</div>
        <div class="book-meta">
          <span class="tag tag-local" title="本地已落地">💾</span>
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
    </div>
  `;
}

// 取得某公網卡片當前的佇列狀態指紋；僅在此指紋變動時才需要更新該卡片的 DOM
function getLiveCardSignature(item) {
  const md5Key = (item.md5 || "").toLowerCase();
  const queueJob = cachedJobsByMd5.get(md5Key) || (item.queue_status ? {
    status: item.queue_status,
    progress_percent: item.queue_progress || 0,
    job_id: item.queue_job_id,
    work_id: item.local_work_id
  } : null);

  if (!queueJob) return `none|${item.availability_tier === 0 && item.local_work_id ? item.local_work_id : ""}`;
  return [
    queueJob.status || "",
    queueJob.progress_percent || 0,
    queueJob.job_id || "",
    queueJob.work_id || item.local_work_id || ""
  ].join("|");
}

// 計算公網卡片三段動態區塊（左指示器 / 狀態標籤 / 下拉選單項目）
function buildLiveCardParts(item) {
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
  let dropdownItemsHtml = "";

  if (isCompleted) {
    const targetWorkId = item.local_work_id || (queueJob && queueJob.work_id);
    leftIndicatorHtml = `<span style="font-size: 1.25rem;" title="本地已收錄">💾</span>`;
    statusBadgeHtml = `<span class="tag tag-local" title="本地已收錄">💾</span>`;
    dropdownItemsHtml = `
      <button class="book-dropdown-item" onclick="openReader('${targetWorkId}'); closeAllBookDropdowns();">
        <span>📖</span> <span>線上閱讀</span>
      </button>
      <button class="book-dropdown-item" onclick="openQuickCollection('${targetWorkId}', '${escapeJsArg(item.title)}'); closeAllBookDropdowns();">
        <span>⭐</span> <span>加入個人書單</span>
      </button>
      <a class="book-dropdown-item" href="${BASE_PATH}/api/files/${targetWorkId}/raw" download title="下載原檔至本地" onclick="closeAllBookDropdowns();">
        <span>📥</span> <span>下載原檔至本機</span>
      </a>
      <button class="book-dropdown-item" onclick="openDetail('${targetWorkId}'); closeAllBookDropdowns();">
        <span>ℹ️</span> <span>書籍元資料詳情</span>
      </button>
    `;
  } else if (isDownloading) {
    leftIndicatorHtml = `<span class="pulse-anim" style="font-size: 1.15rem;" title="正在鏡像下載 (${queueJob.progress_percent}%)">⏳</span>`;
    statusBadgeHtml = `<span class="tag" style="background: rgba(56, 189, 248, 0.18); color: var(--accent); border: 1px solid var(--accent);"><span class="pulse-anim">⏳</span> 正在鏡像 (${queueJob.progress_percent}%)</span>`;
    dropdownItemsHtml = `
      <button class="book-dropdown-item" onclick="openQueueModal(); closeAllBookDropdowns();">
        <span>📥</span> <span>查看下載佇列 (${queueJob.progress_percent}%)</span>
      </button>
      <button class="book-dropdown-item" onclick="previewLiveDetail('${item.md5}'); closeAllBookDropdowns();">
        <span>ℹ️</span> <span>雲端書籍元資料詳情</span>
      </button>
    `;
  } else if (isQueued) {
    leftIndicatorHtml = `<span style="font-size: 1.15rem;" title="排隊收書中">⏳</span>`;
    statusBadgeHtml = `<span class="tag" style="background: rgba(245, 158, 11, 0.18); color: #f59e0b; border: 1px solid rgba(245,158,11,0.4);">⏳ 排隊收書中</span>`;
    dropdownItemsHtml = `
      <button class="book-dropdown-item" onclick="openQueueModal(); closeAllBookDropdowns();">
        <span>⏳</span> <span>查看排隊佇列</span>
      </button>
      <button class="book-dropdown-item" onclick="previewLiveDetail('${item.md5}'); closeAllBookDropdowns();">
        <span>ℹ️</span> <span>雲端書籍元資料詳情</span>
      </button>
    `;
  } else if (isPaused) {
    leftIndicatorHtml = `<span style="font-size: 1.15rem;" title="已暫停">⏸️</span>`;
    statusBadgeHtml = `<span class="tag" style="background: rgba(148, 163, 184, 0.18); color: var(--text-muted);">⏸️ 暫停收書中</span>`;
    dropdownItemsHtml = `
      <button class="book-dropdown-item" onclick="resumeJob('${queueJob.job_id}'); closeAllBookDropdowns();">
        <span>▶️</span> <span>繼續鏡像收書</span>
      </button>
      <button class="book-dropdown-item" onclick="previewLiveDetail('${item.md5}'); closeAllBookDropdowns();">
        <span>ℹ️</span> <span>雲端書籍元資料詳情</span>
      </button>
    `;
  } else if (isFailed) {
    leftIndicatorHtml = `<span style="font-size: 1.15rem;" title="下載失敗">❌</span>`;
    statusBadgeHtml = `<span class="tag" style="background: rgba(239, 68, 68, 0.18); color: #ef4444;">❌ 收書失敗</span>`;
    dropdownItemsHtml = `
      <button class="book-dropdown-item" onclick="retryJob('${queueJob.job_id}'); closeAllBookDropdowns();">
        <span>🔄</span> <span>重新收書</span>
      </button>
      <button class="book-dropdown-item" onclick="previewLiveDetail('${item.md5}'); closeAllBookDropdowns();">
        <span>ℹ️</span> <span>雲端書籍元資料詳情</span>
      </button>
    `;
  } else {
    leftIndicatorHtml = item.md5 ? `
      <input type="checkbox" class="book-select-checkbox" data-md5="${item.md5}" title="勾選以進行批次收書" style="cursor: pointer; width: 18px; height: 18px;">
    ` : `<span style="font-size: 1.25rem;" title="公網資源">🌐</span>`;
    statusBadgeHtml = `<span class="tag tag-remote" title="公網資源">🌐</span>`;
    dropdownItemsHtml = `
      <button class="book-dropdown-item" id="btn-dl-${item.md5}" onclick="triggerSingleDownload('${item.md5}'); closeAllBookDropdowns();">
        <span>📥</span> <span>鏡像收書至本地</span>
      </button>
      <button class="book-dropdown-item" onclick="previewLiveDetail('${item.md5}'); closeAllBookDropdowns();">
        <span>ℹ️</span> <span>雲端書籍元資料詳情</span>
      </button>
    `;
  }

  return { leftIndicatorHtml, statusBadgeHtml, dropdownItemsHtml };
}

function renderLiveBookCard(item) {
  const formatTag = getFormatTag(item.format);
  const langTag = item.language ? `<span class="tag tag-lang">${item.language.toUpperCase()}</span>` : "";
  const sizeMb = item.size_bytes ? (item.size_bytes / (1024 * 1024)).toFixed(1) + " MB" : "";
  const yearText = item.publication_year ? `• ${item.publication_year}年` : "";

  const md5Key = (item.md5 || "").toLowerCase();
  const { leftIndicatorHtml, statusBadgeHtml, dropdownItemsHtml } = buildLiveCardParts(item);

  return `
    <div class="book-card" data-md5="${md5Key}">
      <div class="book-card-header">
        <div class="book-indicator-wrap">
          ${leftIndicatorHtml}
        </div>
        <div class="book-more-wrap">
          <button class="btn btn-icon btn-outline book-more-btn" onclick="toggleBookCardDropdown(this, event)" title="更多操作">
            ⋯
          </button>
          <div class="book-dropdown-menu">
            ${dropdownItemsHtml}
          </div>
        </div>
      </div>
      <div class="book-main">
        <div class="book-title">${escapeHtml(item.title)}</div>
        <div class="book-meta">
          <span class="book-status-slot" style="display: contents;">${statusBadgeHtml}</span>
          ${formatTag}
          ${langTag}
          <span>✍️ ${escapeHtml(item.authors_display || "未知作者")}</span>
          <span>${yearText}</span>
          <span>💾 ${sizeMb}</span>
          ${item.publisher ? `<span>🏢 ${escapeHtml(item.publisher)}</span>` : ""}
          ${item.md5 ? `<span style="font-family:monospace; font-size:0.75rem; color:var(--text-muted);">MD5: ${item.md5.substring(0, 8)}...</span>` : ""}
        </div>
      </div>
    </div>
  `;
}

// 方案 B 核心：僅對佇列狀態指紋真的變動的卡片做局部 DOM 置換，其餘卡片完全不動。
// 回傳實際被更新的卡片數（0 代表本輪輪詢沒有觸碰任何 DOM）。
function patchChangedLiveCards() {
  const bookList = document.getElementById("bookList");
  if (!bookList) return 0;

  let patched = 0;
  for (const item of currentResults || []) {
    const md5Key = (item.md5 || "").toLowerCase();
    if (!md5Key) continue;
    if (item.availability_tier === 0 || (item.work_id && !item.work_id.startsWith("libgen_"))) continue;

    const nextSig = getLiveCardSignature(item);
    if (renderedCardSigByMd5.get(md5Key) === nextSig) continue;

    const card = bookList.querySelector(`.book-card[data-md5="${md5Key}"]`);
    if (!card) {
      // 該卡片不在目前 DOM（例如尚未渲染），僅更新指紋基準，不強制重繪
      renderedCardSigByMd5.set(md5Key, nextSig);
      continue;
    }

    const { leftIndicatorHtml, statusBadgeHtml, dropdownItemsHtml } = buildLiveCardParts(item);

    const indicatorWrap = card.querySelector(".book-indicator-wrap");
    if (indicatorWrap) {
      // 狀態已離開「可勾選」時，同步從批次選取集合移除，避免殘留幽靈選取
      const oldCb = indicatorWrap.querySelector(".book-select-checkbox");
      const wasChecked = !!(oldCb && oldCb.checked);
      indicatorWrap.innerHTML = leftIndicatorHtml;
      const newCb = indicatorWrap.querySelector(".book-select-checkbox");
      if (newCb) {
        newCb.checked = wasChecked || selectedMd5s.has(newCb.dataset.md5);
        bindOneCheckbox(newCb);
      } else if (!newCb) {
        selectedMd5s.delete(item.md5);
      }
    }

    const statusSlot = card.querySelector(".book-status-slot");
    if (statusSlot) statusSlot.innerHTML = statusBadgeHtml;

    // 下拉選單若正在開啟中則不動它，避免把使用者剛打開的選單關掉
    const menu = card.querySelector(".book-dropdown-menu");
    if (menu && !menu.classList.contains("active")) {
      menu.innerHTML = dropdownItemsHtml;
    }

    renderedCardSigByMd5.set(md5Key, nextSig);
    patched++;
  }

  if (patched > 0) updateBatchBar();
  return patched;
}

function bindOneCheckbox(cb) {
  if (!cb || cb.dataset.bound === "1") return;
  cb.dataset.bound = "1";
  cb.addEventListener("change", () => {
    const md5 = cb.dataset.md5;
    if (cb.checked) {
      selectedMd5s.add(md5);
    } else {
      selectedMd5s.delete(md5);
    }
    updateBatchBar();
  });
}

function bindCheckboxEvents() {
  document.querySelectorAll(".book-select-checkbox").forEach(bindOneCheckbox);
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
        mirror_links: item.mirror_links || [],
        publication_year: item.publication_year ?? null
      })
    });
    if (!res.ok) throw new Error("加入下載失敗");
    const job = await res.json();
    if (btn) {
      btn.innerText = "⏳ 正在鏡像...";
    }
    openQueueModal();
  } catch (err) {
    showCustomAlert({
      title: "下載失敗",
      message: `鏡像下載啟動失敗: ${err.message}`,
      icon: "❌",
      type: "error",
      anchor: btn
    });
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
        mirror_links: item.mirror_links || [],
        publication_year: item.publication_year ?? null
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
    selectedMd5s.clear();
    updateBatchBar();
    openQueueModal();
    showCustomAlert({
      title: "已加入下載佇列",
      message: `已成功將 <b>${data.enqueued_count}</b> 本書籍加入本地鏡像下載佇列！`,
      icon: "📥",
      type: "success",
      anchor: document.getElementById("batchDownloadBtn") || document.getElementById("openQueueBtn")
    });
  } catch (err) {
    showCustomAlert({
      title: "批次下載失敗",
      message: `批次下載失敗: ${err.message}`,
      icon: "❌",
      type: "error",
      anchor: document.getElementById("batchDownloadBtn")
    });
  }
}

async function retryJob(jobId) {
  try {
    const res = await fetch(`${BASE_PATH}/api/crawler/jobs/${jobId}/retry`, { method: "POST" });
    if (!res.ok) throw new Error("重試失敗");
    refreshQueueModal();
  } catch (e) {
    showCustomAlert({
      title: "重試失敗",
      message: `重試任務失敗: ${e.message}`,
      icon: "❌",
      type: "error"
    });
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

    // 若首頁當前正在呈現檢索結果，僅對佇列狀態真的變動的卡片做差量原地更新（方案 B），
    // 不再無條件 applySortAndRender() 全量重繪，以免毀掉使用者的勾選、已開啟的下拉選單與焦點
    if (currentResults && currentResults.length > 0 && document.getElementById("resultsHeader").style.display !== "none") {
      patchChangedLiveCards();
    }

    // 若開啟了本機同步，檢查是否有剛完成且尚未保存至本機磁碟的任務
    if (localStorage.getItem("cms_auto_download_local") === "true") {
      let syncedSet;
      try {
        syncedSet = new Set(JSON.parse(localStorage.getItem("cms_synced_local_jobs") || "[]"));
      } catch (e) { syncedSet = new Set(); }

      for (const j of jobs || []) {
        if (j.status === "completed" && j.work_id && !syncedSet.has(j.job_id)) {
          syncedSet.add(j.job_id);
          localStorage.setItem("cms_synced_local_jobs", JSON.stringify(Array.from(syncedSet)));
          autoSyncBookToLocalDisk(j.work_id, j.title, j.format, j.job_id);
        }
      }
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
                <button class="btn btn-secondary" style="padding: 0.25rem 0.65rem; font-size: 0.95rem; line-height: 1;" onclick="saveSingleBookToLocalDisk('${j.work_id}', '${escapeJsArg(j.title)}', '${j.format || 'pdf'}')" title="下載保存至本機硬碟">📥</button>
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
  if (format === "pdf_born_digital") return '<span class="tag tag-pdf-born" title="原生 PDF (數位原版)">📕</span>';
  if (format === "pdf_scanned") return '<span class="tag tag-pdf-scan" title="掃描版 PDF (影像掃描)">📷</span>';
  if (format === "epub") return '<span class="tag tag-epub" title="EPUB 電子書 (流式排版)">📗</span>';
  return '<span class="tag tag-pdf-born" title="PDF 文件">📕</span>';
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

// 用於 inline onclick 內的「JS 單引號字串」參數，例如 onclick="fn('${escapeJsArg(t)}')"。
// escapeHtml 不足以勝任：HTML 屬性會先被解碼再交給 JS parser，所以把 ' 轉成 &#39;
// 解碼後仍是 '，照樣截斷字串（實測："Silberschatz's ..." 觸發 SyntaxError，選單完全打不開）。
// 正解是先做 JS 字面值跳脫，再做 HTML 屬性跳脫——順序不可顛倒。
function escapeJsArg(str) {
  if (!str) return "";
  return String(str)
    .replace(/\\/g, "\\\\")
    .replace(/'/g, "\\'")
    .replace(/\r/g, "\\r")
    .replace(/\n/g, "\\n")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
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
    if (isChromeExtensionAvailable) {
      badge.innerHTML = `<span style="color: #34d399; font-weight: 600;">🟢 Chrome 原生書籤已連線同步</span>`;
    } else {
      badge.innerHTML = `<span>⚪ 本地模式 • 可在「⚙️ 設定」中下載 Chrome 擴充套件啟用原生同步</span>`;
    }
  }

  const badgeSettings = document.getElementById("extStatusBadgeSettings");
  if (badgeSettings) {
    if (isChromeExtensionAvailable) {
      badgeSettings.className = "tag tag-local";
      badgeSettings.style.background = "";
      badgeSettings.style.color = "";
      badgeSettings.innerText = "🟢 已連線同步中";
    } else {
      badgeSettings.className = "tag";
      badgeSettings.style.background = "rgba(148, 163, 184, 0.15)";
      badgeSettings.style.color = "var(--text-muted)";
      badgeSettings.innerText = "⚪ 未連線 (請安裝插件)";
    }
  }
}

// 頁面載入時自動探測擴充套件
setTimeout(() => {
  callExtension("PING");
}, 150);

// === 本機硬碟儲存與系統設定 (Local Disk Storage & Settings) ===
let localDirectoryHandle = null;

function initSettingsModal() {
  const autoCheckbox = document.getElementById("autoDownloadLocalCheckbox");
  if (autoCheckbox) {
    autoCheckbox.checked = localStorage.getItem("cms_auto_download_local") === "true";
  }
  const pathDisplay = document.getElementById("localDirPathDisplay");
  const savedDirName = localStorage.getItem("cms_local_dir_name");
  if (pathDisplay) {
    if (savedDirName) {
      pathDisplay.innerText = `📁 目前指定: ${savedDirName}`;
    } else {
      pathDisplay.innerText = `📁 目前路徑: 瀏覽器預設下載目錄`;
    }
  }
  updateExtensionStatusIndicator();
  loadLibgenMirrorsSettings();
}

// === 自訂 Libgen 來源與鏡像管理 (Custom Libgen Mirrors & Pre-flight Validation) ===
let cachedLibgenMirrors = [];

async function loadLibgenMirrorsSettings() {
  const container = document.getElementById("mirrorsListContainer");
  if (!container) return;
  
  try {
    const res = await fetch(`${BASE_PATH}/api/settings/libgen-mirrors`);
    if (!res.ok) throw new Error("讀取鏡像設定失敗");
    cachedLibgenMirrors = await res.json();
    renderLibgenMirrorsList(cachedLibgenMirrors);
    await loadDispatchedIssuesNotice();
  } catch (err) {
    console.error("載入鏡像失敗:", err);
    container.innerHTML = `<p style="color: #ef4444; font-size: 0.85rem; padding: 1rem; text-align: center;">載入失敗: ${err.message}</p>`;
  }
}

function renderLibgenMirrorsList(mirrors) {
  const container = document.getElementById("mirrorsListContainer");
  if (!container) return;

  if (!mirrors || mirrors.length === 0) {
    container.innerHTML = `<p style="color: var(--text-muted); font-size: 0.85rem; padding: 1rem; text-align: center;">目前無設定任何鏡像來源，請點選「恢復預設」或手動新增。</p>`;
    return;
  }

  container.innerHTML = mirrors.map((m, idx) => {
    const safeId = "m_" + btoa(m.url).replace(/[^a-zA-Z0-9]/g, "").substring(0, 16);
    
    // 狀態標籤
    let statusBadge = "";
    if (m.validation_status === "verified") {
      statusBadge = `<span class="tag" style="background: rgba(16, 185, 129, 0.18); color: #34d399; font-size: 0.72rem;">🟢 通過驗證</span>`;
    } else if (m.validation_status === "incompatible_layout") {
      const brLink = m.br_id ? `<span title="點擊查看 BR 報告" style="cursor: pointer; text-decoration: underline;" onclick="showBrDetailModal('${m.br_id}', '${escapeJsArg(m.last_error || '')}')">[${m.br_id}]</span>` : "";
      statusBadge = `<span class="tag" style="background: rgba(239, 68, 68, 0.18); color: #f87171; font-size: 0.72rem;" title="${escapeHtml(m.last_error || '結構無法解析')}">⚠️ 結構不相容 ${brLink}</span>`;
    } else if (m.validation_status === "offline") {
      statusBadge = `<span class="tag" style="background: rgba(100, 116, 139, 0.2); color: var(--text-muted); font-size: 0.72rem;" title="${escapeHtml(m.last_error || '連線逾時')}">🔴 連線失敗</span>`;
    } else {
      statusBadge = `<span class="tag" style="background: rgba(245, 158, 11, 0.18); color: #fbbf24; font-size: 0.72rem;">⏳ 待驗證</span>`;
    }

    // 適配器標籤
    let adapterBadge = "";
    if (m.adapter_type === "libgen_li") {
      adapterBadge = `<span class="tag" style="background: rgba(56, 189, 248, 0.15); color: var(--accent); font-size: 0.7rem;">Libgen.li 系列</span>`;
    } else if (m.adapter_type === "libgen_is") {
      adapterBadge = `<span class="tag" style="background: rgba(168, 85, 247, 0.15); color: #c084fc; font-size: 0.7rem;">Libgen.is 傳統</span>`;
    } else if (m.adapter_type === "direct_gateway") {
      adapterBadge = `<span class="tag" style="background: rgba(234, 179, 8, 0.15); color: #facc15; font-size: 0.7rem;">直鏈 Gateway</span>`;
    }

    const latencyText = m.latency_ms ? `${m.latency_ms} ms` : "-- ms";

    return `
      <div class="mirror-item-row status-${m.validation_status || 'unverified'}" id="row-${safeId}">
        <div style="display: flex; align-items: center; gap: 0.55rem; flex: 1; min-width: 0;">
          <input type="checkbox" class="mirror-toggle-checkbox" data-url="${escapeHtml(m.url)}" ${m.enabled ? "checked" : ""} title="啟用/停用此來源" style="width: 16px; height: 16px; cursor: pointer;">
          <div style="flex: 1; min-width: 0;">
            <div style="display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap;">
              <span style="font-family: monospace; font-weight: 700; font-size: 0.88rem; color: var(--text-primary);">${escapeHtml(m.url)}</span>
              ${m.is_default ? `<span class="tag" style="background: rgba(99, 102, 241, 0.15); color: #a5b4fc; font-size: 0.7rem;">內建</span>` : `<span class="tag" style="background: rgba(16, 185, 129, 0.15); color: #34d399; font-size: 0.7rem;">自訂</span>`}
              ${statusBadge}
              ${adapterBadge}
            </div>
            <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.15rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
              ${escapeHtml(m.note || "無備註")} • 抽樣解析: ${m.sample_records_count || 0} 筆
            </div>
          </div>
        </div>

        <div class="mirror-actions-wrap">
          <span class="mirror-ping-badge" id="ping-${safeId}" style="font-size: 0.75rem; color: var(--text-muted); min-width: 52px; text-align: right; font-family: monospace;">${latencyText}</span>
          <button class="btn btn-secondary mirror-action-btn" onclick="handleValidateSingleMirror('${m.url}', '${safeId}', this)" title="執行預檢驗證與爬取適配器測試">⚡</button>
          <button class="btn btn-secondary mirror-action-btn" onclick="handleMoveMirror(${idx}, -1)" title="調高優先級" ${idx === 0 ? 'disabled style="opacity: 0.4; cursor: not-allowed;"' : ''}>⬆️</button>
          <button class="btn btn-secondary mirror-action-btn" onclick="handleMoveMirror(${idx}, 1)" title="降低優先級" ${idx === mirrors.length - 1 ? 'disabled style="opacity: 0.4; cursor: not-allowed;"' : ''}>⬇️</button>
          <button class="btn btn-outline mirror-action-btn" onclick="handleDeleteMirror('${m.url}', event)" title="刪除此來源" style="color: #ef4444;">🗑️</button>
        </div>
      </div>
    `;
  }).join("");

  // 綁定開關事件
  container.querySelectorAll(".mirror-toggle-checkbox").forEach(cb => {
    cb.addEventListener("change", async (e) => {
      const url = e.target.dataset.url;
      const checked = e.target.checked;
      await handleToggleMirror(url, checked);
    });
  });
}

async function handleValidateSingleMirror(url, safeId, btn) {
  const pingEl = document.getElementById(`ping-${safeId}`);
  if (pingEl) pingEl.innerText = "驗證中...";
  if (btn) btn.disabled = true;

  try {
    const res = await fetch(`${BASE_PATH}/api/settings/libgen-mirrors/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url, auto_dispatch_br: true })
    });
    if (!res.ok) throw new Error("預檢驗證請求失敗");
    const report = await res.json();
    
    await loadLibgenMirrorsSettings();

    if (report.validation_status === "verified") {
      showCustomAlert({
        title: "預檢驗證通過",
        message: `鏡像 <b>${escapeHtml(url)}</b> 驗證成功！<br>• 適配器: <code>${report.adapter_type}</code><br>• 延遲: <code>${report.latency_ms} ms</code><br>• 抽樣解析: <code>${report.sample_records_count} 筆</code><br><span style="color: #10b981;">已正式啟用並納入檢索與下載輪替池。</span>`,
        icon: "🟢",
        type: "success",
        anchor: btn
      });
    } else if (report.validation_status === "incompatible_layout") {
      showCustomAlert({
        title: "結構不相容 · 自動建立 BR 報告",
        message: `鏡像 <b>${escapeHtml(url)}</b> 連線正常但無法解析 HTML 表格。<br><span style="color: #ef4444; font-size: 0.85rem;">已自動發送 Bug Report：<code>${report.br_id}</code></span><br>該來源已被隔離暫停，待開發專屬解析適配器。`,
        icon: "⚠️",
        type: "warning",
        anchor: btn
      });
    } else {
      showCustomAlert({
        title: "連線失敗",
        message: `鏡像 <b>${escapeHtml(url)}</b> 連線失敗: ${escapeHtml(report.error_message || '逾時')}`,
        icon: "❌",
        type: "error",
        anchor: btn
      });
    }
  } catch (err) {
    console.error("預檢驗證錯誤:", err);
    if (pingEl) pingEl.innerText = "錯誤";
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleValidateAllMirrors() {
  const btn = document.getElementById("validateAllMirrorsBtn");
  if (btn) {
    btn.disabled = true;
    btn.innerText = "⏳ 驗證中...";
  }

  let successCount = 0;
  let brCount = 0;
  let offlineCount = 0;

  for (const m of cachedLibgenMirrors) {
    try {
      const res = await fetch(`${BASE_PATH}/api/settings/libgen-mirrors/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: m.url, auto_dispatch_br: true })
      });
      if (res.ok) {
        const rep = await res.json();
        if (rep.validation_status === "verified") successCount++;
        else if (rep.validation_status === "incompatible_layout") brCount++;
        else offlineCount++;
      }
    } catch (e) {
      offlineCount++;
    }
  }

  await loadLibgenMirrorsSettings();

  if (btn) {
    btn.disabled = false;
    btn.innerText = "⚡ 全部預檢驗證";
  }

  showCustomAlert({
    title: "全部預檢驗證完成",
    message: `共驗證 <b>${cachedLibgenMirrors.length}</b> 個鏡像來源：<br>• 🟢 驗證通過: <b>${successCount}</b> 個<br>• ⚠️ 結構不相容 (已發 BR): <b>${brCount}</b> 個<br>• 🔴 斷線或逾時: <b>${offlineCount}</b> 個`,
    icon: "⚡",
    type: "success",
    anchor: btn
  });
}

async function handleAddCustomMirror() {
  const urlInput = document.getElementById("newMirrorUrlInput");
  const noteInput = document.getElementById("newMirrorNoteInput");
  const addBtn = document.getElementById("addMirrorBtn");

  const rawUrl = (urlInput ? urlInput.value : "").trim();
  const note = (noteInput ? noteInput.value : "").trim();

  if (!rawUrl) {
    showCustomAlert({
      title: "請輸入網址",
      message: "請輸入有效的 Libgen 來源或鏡像網址。",
      icon: "⚠️",
      anchor: addBtn
    });
    return;
  }

  let formattedUrl = rawUrl;
  if (!formattedUrl.startsWith("http://") && !formattedUrl.startsWith("https://")) {
    formattedUrl = `https://${formattedUrl}`;
  }

  if (addBtn) {
    addBtn.disabled = true;
    addBtn.innerText = "⏳ 預檢驗證中...";
  }

  try {
    const res = await fetch(`${BASE_PATH}/api/settings/libgen-mirrors/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: formattedUrl, auto_dispatch_br: true })
    });
    if (!res.ok) throw new Error("新增鏡像預檢驗證失敗");
    const report = await res.json();

    // 更新備註
    if (note) {
      const getRes = await fetch(`${BASE_PATH}/api/settings/libgen-mirrors`);
      if (getRes.ok) {
        const list = await getRes.json();
        const target = list.find(m => m.url === formattedUrl || m.url === report.url);
        if (target) {
          target.note = note;
          await fetch(`${BASE_PATH}/api/settings/libgen-mirrors`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mirrors: list })
          });
        }
      }
    }

    urlInput.value = "";
    if (noteInput) noteInput.value = "";
    await loadLibgenMirrorsSettings();

    if (report.validation_status === "verified") {
      showCustomAlert({
        title: "來源新增並通過驗證",
        message: `自訂來源 <b>${escapeHtml(formattedUrl)}</b> 預檢驗證通過（${report.adapter_type}，${report.latency_ms} ms），已正式上線啟用！`,
        icon: "🎉",
        type: "success",
        anchor: addBtn
      });
    } else if (report.validation_status === "incompatible_layout") {
      showCustomAlert({
        title: "來源新增但結構無法解析",
        message: `來源 <b>${escapeHtml(formattedUrl)}</b> 連線正常但無法以現有爬蟲解析，已<b>自動向 Repo 發送 ${report.br_id}</b> 並暫停隔離，等待適配器修復。`,
        icon: "⚠️",
        type: "warning",
        anchor: addBtn
      });
    } else {
      showCustomAlert({
        title: "來源無法連線",
        message: `來源 <b>${escapeHtml(formattedUrl)}</b> 無法連線（${report.error_message || '逾時'}），已加入但預設處於停用狀態。`,
        icon: "🔴",
        type: "warning",
        anchor: addBtn
      });
    }
  } catch (err) {
    showCustomAlert({
      title: "新增失敗",
      message: `新增鏡像失敗: ${err.message}`,
      icon: "❌",
      type: "error",
      anchor: addBtn
    });
  } finally {
    if (addBtn) {
      addBtn.disabled = false;
      addBtn.innerText = "➕ 新增並驗證";
    }
  }
}

async function handleToggleMirror(url, checked) {
  const mirror = cachedLibgenMirrors.find(m => m.url === url);
  if (mirror) {
    mirror.enabled = checked;
    await saveCurrentMirrors();
  }
}

async function handleMoveMirror(index, direction) {
  const newIndex = index + direction;
  if (newIndex < 0 || newIndex >= cachedLibgenMirrors.length) return;

  const item = cachedLibgenMirrors.splice(index, 1)[0];
  cachedLibgenMirrors.splice(newIndex, 0, item);
  
  // 重新編號優先級
  cachedLibgenMirrors.forEach((m, i) => { m.priority = i + 1; });
  renderLibgenMirrorsList(cachedLibgenMirrors);
  await saveCurrentMirrors();
}

async function handleDeleteMirror(url, event) {
  const anchor = event ? (event.currentTarget || event.target) : null;
  const confirmed = await showCustomConfirm({
    title: "刪除來源",
    message: `確定要移除鏡像來源「<b>${escapeHtml(url)}</b>」嗎？`,
    confirmText: "確認刪除",
    cancelText: "取消",
    isDanger: true,
    icon: "🗑️",
    anchor: anchor
  });
  if (!confirmed) return;

  cachedLibgenMirrors = cachedLibgenMirrors.filter(m => m.url !== url);
  cachedLibgenMirrors.forEach((m, i) => { m.priority = i + 1; });
  renderLibgenMirrorsList(cachedLibgenMirrors);
  await saveCurrentMirrors();
}

async function handleResetMirrors() {
  const btn = document.getElementById("resetMirrorsBtn");
  const confirmed = await showCustomConfirm({
    title: "恢復原廠預設",
    message: "確定要將 Libgen 鏡像清單重設回原廠預設設定嗎？（將保留官方內建 9 組鏡像節點）",
    confirmText: "確認恢復",
    cancelText: "取消",
    icon: "🔄",
    anchor: btn
  });
  if (!confirmed) return;

  try {
    const res = await fetch(`${BASE_PATH}/api/settings/libgen-mirrors/reset`, { method: "POST" });
    if (!res.ok) throw new Error("重設失敗");
    cachedLibgenMirrors = await res.json();
    renderLibgenMirrorsList(cachedLibgenMirrors);
    showCustomAlert({
      title: "已恢復預設來源",
      message: "已成功恢復系統預設 Libgen 鏡像來源清單！",
      icon: "✅",
      type: "success",
      anchor: btn
    });
  } catch (err) {
    showCustomAlert({
      title: "重設失敗",
      message: `恢復預設失敗: ${err.message}`,
      icon: "❌",
      type: "error",
      anchor: btn
    });
  }
}

async function saveCurrentMirrors() {
  try {
    await fetch(`${BASE_PATH}/api/settings/libgen-mirrors`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mirrors: cachedLibgenMirrors })
    });
  } catch (e) {
    console.error("儲存鏡像清單失敗:", e);
  }
}

async function loadDispatchedIssuesNotice() {
  const noticeEl = document.getElementById("dispatchedIssuesNotice");
  if (!noticeEl) return;

  try {
    const res = await fetch(`${BASE_PATH}/api/settings/libgen-mirrors/issues`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.total > 0) {
      noticeEl.style.display = "block";
      noticeEl.innerHTML = `
        <div style="display: flex; align-items: center; justify-content: space-between;">
          <span>📋 系統已自動向 Repo 提出 <b>${data.total}</b> 份爬蟲適配器缺失報告 (BR)</span>
          <span style="font-size: 0.75rem; color: #f87171;">已自動隔離無效來源</span>
        </div>
      `;
    } else {
      noticeEl.style.display = "none";
    }
  } catch (e) {
    noticeEl.style.display = "none";
  }
}

function showBrDetailModal(brId, errorMsg) {
  showCustomAlert({
    title: `Bug Report: ${brId}`,
    message: `此來源目前處於隔離狀態，失敗詳情：<br><pre style="background: rgba(0,0,0,0.3); padding: 0.5rem; border-radius: 4px; font-size: 0.78rem; overflow-x: auto; margin-top: 0.4rem;">${escapeHtml(errorMsg || '未知錯誤')}</pre><br><span style="font-size: 0.8rem; color: var(--text-muted);">報告已寫入專案 <code>issues/${brId}.md</code>，待開發適配器修復後即可重新驗證上線。</span>`,
    icon: "📋"
  });
}

async function handleSelectLocalDirectory(event) {
  const anchor = event ? (event.currentTarget || event.target) : document.getElementById("selectLocalDirBtn");
  if (!window.showDirectoryPicker) {
    showCustomAlert({
      title: "瀏覽器限制",
      message: "您的瀏覽器不支援直接選取本地資料夾（File System Access API）。系統將透過瀏覽器預設下載機制自動下載保存。",
      icon: "ℹ️",
      anchor: anchor
    });
    return;
  }
  try {
    const dirHandle = await window.showDirectoryPicker({ mode: "readwrite" });
    if (dirHandle) {
      localDirectoryHandle = dirHandle;
      localStorage.setItem("cms_local_dir_name", dirHandle.name);
      localStorage.setItem("cms_auto_download_local", "true");
      
      const autoCheckbox = document.getElementById("autoDownloadLocalCheckbox");
      if (autoCheckbox) autoCheckbox.checked = true;

      const pathDisplay = document.getElementById("localDirPathDisplay");
      if (pathDisplay) pathDisplay.innerText = `📁 目前指定: ${dirHandle.name}`;
      showCustomAlert({
        title: "本機儲存已就緒",
        message: `已成功指定本機儲存目錄：<b>${dirHandle.name}</b>！<br>收書落地時將自動寫入此資料夾。`,
        icon: "📁",
        type: "success",
        anchor: anchor
      });
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      console.error("選取目錄失敗:", err);
      showCustomAlert({
        title: "選取目錄失敗",
        message: `選取目錄失敗: ${err.message}`,
        icon: "❌",
        type: "error",
        anchor: anchor
      });
    }
  }
}

async function autoSyncBookToLocalDisk(workId, title, format, jobId) {
  if (localStorage.getItem("cms_auto_download_local") !== "true") return;
  if (!workId) return;

  await saveSingleBookToLocalDisk(workId, title, format);
}

async function saveSingleBookToLocalDisk(workId, title, format) {
  if (!workId) return;
  const ext = (format || "pdf").toLowerCase().includes("epub") ? "epub" : "pdf";
  const cleanTitle = (title || "book").replace(/[\\/:*?"<>|]/g, "_").trim();
  const filename = `${cleanTitle}.${ext}`;
  const rawUrl = `${BASE_PATH}/api/files/${workId}/raw`;

  // 若使用者已透過 File System Access API 指定本機資料夾，直接寫入檔案
  if (localDirectoryHandle) {
    try {
      const fileHandle = await localDirectoryHandle.getFileHandle(filename, { create: true });
      const writable = await fileHandle.createWritable();
      const res = await fetch(rawUrl);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await res.body.pipeTo(writable);
      console.log(`[Local Disk Sync] 已成功直接寫入本機硬碟: ${filename}`);
      return;
    } catch (fsErr) {
      console.warn("[Local Disk Sync] File System API 寫入失敗，降級為瀏覽器下載觸發:", fsErr);
    }
  }

  // 瀏覽器原生下載觸發
  const a = document.createElement("a");
  a.href = rawUrl;
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    if (a.parentNode) document.body.removeChild(a);
  }, 2000);
  console.log(`[Local Disk Sync] 已觸發瀏覽器下載至本機: ${filename}`);
}

// === 個人書單 (Personal Collections) ===
let currentActiveCollectionId = null;
let quickTargetWorkId = null;
let quickTargetWorkTitle = null;
// 遞增序號：使用者連續點不同書籍時，舊的載入回應不得寫進新開的選單
let quickCollectionReqId = 0;

async function openCollectionsModal(targetColId = null) {
  const modal = document.getElementById("collectionsModal");
  modal.classList.add("active");
  modal.classList.remove("in-detail-view");
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
      <div style="display: flex; align-items: center; gap: 0.5rem;">
        <button class="btn btn-secondary mobile-back-btn" onclick="document.getElementById('collectionsModal').classList.remove('in-detail-view')" title="返回書單列表" style="padding: 0.25rem 0.65rem; font-size: 0.85rem;">⬅️</button>
        <div>
          <h3 style="font-size: 1.3rem; display: flex; align-items: center; gap: 0.4rem; margin: 0;">
            <span>📁</span>
            <span>${escapeHtml(folderNode.title)}</span>
          </h3>
          <p style="font-size: 0.85rem; color: var(--text-muted); margin-top: 0.25rem;">Chrome 原生書籤資料夾 • 共 ${items.length} 筆書籤</p>
        </div>
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
              <span class="tag tag-local" title="Chrome 原生書籤">🌐</span>
              <span style="font-size: 0.75rem; color: var(--text-muted);">${escapeHtml(it.url)}</span>
            </div>
          </div>
        <div class="book-actions">
            <a class="btn btn-icon btn-primary" href="${it.url}" target="_blank" title="立即閱讀">👁️</a>
            <button class="btn btn-icon btn-outline" onclick="removeChromeBookmark('${it.id}', '${escapeJsArg(it.title)}', event)" title="刪除書籤" style="color: #ef4444;">❌</button>
          </div>
        </div>
      `).join("")}
    </div>
  `;
}

async function removeChromeBookmark(bookmarkId, title, event) {
  const anchor = event ? (event.currentTarget || event.target) : null;
  const confirmed = await showCustomConfirm({
    title: "刪除書籤",
    message: `確定要刪除「<b>${escapeHtml(title || '此書籤')}</b>」嗎？`,
    confirmText: "確認刪除",
    cancelText: "取消",
    isDanger: true,
    icon: "🗑️",
    anchor: anchor
  });
  if (!confirmed) return;
  await callExtension("REMOVE_BOOKMARK", { bookmarkId });
  await loadCollectionsList(currentActiveCollectionId);
}

async function selectCollection(collectionId) {
  currentActiveCollectionId = collectionId;
  const items = document.querySelectorAll(".collection-sidebar-item");
  items.forEach(el => el.classList.remove("active"));
  document.getElementById("collectionsModal").classList.add("in-detail-view");
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
        <div style="display: flex; align-items: center; gap: 0.5rem;">
          <button class="btn btn-secondary mobile-back-btn" onclick="document.getElementById('collectionsModal').classList.remove('in-detail-view')" title="返回書單列表" style="padding: 0.25rem 0.65rem; font-size: 0.85rem;">⬅️</button>
          <div>
            <h3 style="font-size: 1.3rem; display: flex; align-items: center; gap: 0.4rem; margin: 0;">
              <span>${col.icon || '📚'}</span>
              <span>${escapeHtml(col.name)}</span>
            </h3>
            <p style="font-size: 0.85rem; color: var(--text-muted); margin-top: 0.25rem;">${escapeHtml(col.description || '自訂書單')} • 共 ${col.items.length} 本書籍</p>
          </div>
        </div>
        <div style="display: flex; gap: 0.5rem;">
          <button class="btn btn-outline" onclick="renameCollectionPrompt('${col.collection_id}', '${escapeJsArg(col.name)}', event)" title="重命名書單" style="padding: 0.35rem 0.65rem;">✏️</button>
          ${!isSystem ? `<button class="btn btn-outline" onclick="deleteCollectionPrompt('${col.collection_id}', '${escapeJsArg(col.name)}', event)" title="刪除此書單" style="padding: 0.35rem 0.65rem; color: #ef4444;">🗑️</button>` : ''}
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
                <span class="tag tag-local" title="本地書單藏書">💾</span>
                ${getFormatTag(it.work.format)}
                <span>✍️ ${escapeHtml(it.work.authors_display || "未知作者")}</span>
                ${it.work.publication_year ? `<span>• ${it.work.publication_year}年</span>` : ''}
              </div>
            </div>
            <div class="book-actions">
              <button class="btn btn-icon btn-primary" onclick="openReader('${it.work_id}')" title="立即閱讀">📖</button>
              <button class="btn btn-icon btn-outline" onclick="removeBookFromCollection('${col.collection_id}', '${it.work_id}', '${escapeJsArg(it.work.title)}', event)" title="從書單移除" style="color: #ef4444;">❌</button>
            </div>
          </div>
        `).join("")}
      </div>
    `;
  } catch (err) {
    console.error("載入書單詳情失敗:", err);
  }
}

async function createNewCollectionPrompt(eventOrAnchor) {
  const anchor = (eventOrAnchor && eventOrAnchor.currentTarget) ? eventOrAnchor.currentTarget : (eventOrAnchor || document.getElementById("newCollectionBtn"));
  const name = await showCustomPrompt({
    title: "新建個人書單",
    message: "請輸入新書單名稱（例如：科幻經典、待讀清單）：",
    placeholder: "書單名稱...",
    confirmText: "建立書單",
    cancelText: "取消",
    icon: "📚",
    anchor: anchor
  });
  if (!name || !name.trim()) return;

  if (isChromeExtensionAvailable) {
    try {
      const extRes = await callExtension("CREATE_FOLDER", { name: name.trim() });
      if (extRes && extRes.success) {
        await loadCollectionsList(extRes.data ? extRes.data.id : null);
        return;
      }
    } catch (e) {
      console.warn("Chrome 擴充套件建立書單資料夾失敗，改用後端:", e);
    }
  }

  try {
    const res = await fetch(`${BASE_PATH}/api/collections`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), icon: "📚" })
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || `伺服器回應錯誤 (${res.status})`);
    }
    const data = await res.json();
    await loadCollectionsList(data.collection_id);
  } catch (err) {
    console.error("建立書單失敗:", err);
    showCustomAlert({
      title: "建立書單失敗",
      message: `建立失敗: ${err.message}`,
      icon: "❌",
      type: "error",
      anchor: anchor
    });
  }
}

async function renameCollectionPrompt(colId, currentName, event) {
  const anchor = event ? (event.currentTarget || event.target) : null;
  const newName = await showCustomPrompt({
    title: "重命名書單",
    message: "請輸入書單新名稱：",
    defaultValue: currentName,
    placeholder: "書單名稱...",
    confirmText: "儲存名稱",
    cancelText: "取消",
    icon: "✏️",
    anchor: anchor
  });
  if (!newName || !newName.trim() || newName.trim() === currentName) return;
  try {
    const res = await fetch(`${BASE_PATH}/api/collections/${colId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName.trim() })
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || `伺服器回應錯誤 (${res.status})`);
    }
    await loadCollectionsList(colId);
  } catch (err) {
    console.error("重命名失敗:", err);
    showCustomAlert({
      title: "重命名失敗",
      message: `重命名失敗: ${err.message}`,
      icon: "❌",
      type: "error",
      anchor: anchor
    });
  }
}

async function deleteCollectionPrompt(colId, colName, event) {
  const anchor = event ? (event.currentTarget || event.target) : null;
  const confirmed = await showCustomConfirm({
    title: "刪除書單",
    message: `確定要刪除「<b>${escapeHtml(colName || '此書單')}</b>」嗎？<br><span style="font-size: 0.8rem; color: var(--text-muted);">（不會刪除書籍本體檔案）</span>`,
    confirmText: "確認刪除",
    cancelText: "保留書單",
    isDanger: true,
    icon: "🗑️",
    anchor: anchor
  });
  if (!confirmed) return;
  try {
    const res = await fetch(`${BASE_PATH}/api/collections/${colId}`, {
      method: "DELETE"
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || `伺服器回應錯誤 (${res.status})`);
    }
    currentActiveCollectionId = null;
    await loadCollectionsList();
  } catch (err) {
    console.error("刪除失敗:", err);
    showCustomAlert({
      title: "刪除失敗",
      message: `刪除失敗: ${err.message}`,
      icon: "❌",
      type: "error",
      anchor: anchor
    });
  }
}

async function removeBookFromCollection(colId, workId, bookTitle, event) {
  const anchor = event ? (event.currentTarget || event.target) : null;
  const confirmed = await showCustomConfirm({
    title: "移除書籍",
    message: `確定要從此書單移除「<b>${escapeHtml(bookTitle || '此書籍')}</b>」嗎？`,
    confirmText: "確認移除",
    cancelText: "取消",
    isDanger: true,
    icon: "❌",
    anchor: anchor
  });
  if (!confirmed) return;
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

// === 個人書單可攜化：Netscape HTML 書籤匯出 / 匯入 & JSON 備份 ===

async function fetchAllCollectionsWithItems() {
  const res = await fetch(`${BASE_PATH}/api/collections`);
  if (!res.ok) throw new Error("讀取書單列表失敗");
  const list = await res.json();
  
  const fullCollections = [];
  for (const c of list) {
    try {
      const detailRes = await fetch(`${BASE_PATH}/api/collections/${c.collection_id}`);
      if (detailRes.ok) {
        fullCollections.push(await detailRes.json());
      } else {
        fullCollections.push(c);
      }
    } catch (e) {
      fullCollections.push(c);
    }
  }
  return fullCollections;
}

async function exportCollectionsAsNetscapeHtml(anchor) {
  try {
    const collections = await fetchAllCollectionsWithItems();
    if (!collections || collections.length === 0) {
      showCustomAlert({
        title: "書單為空",
        message: "您目前尚未建立任何書單，請先建立書單或收藏書籍後再行匯出。",
        icon: "ℹ️",
        anchor: anchor
      });
      return;
    }

    const timestamp = Math.floor(Date.now() / 1000);
    const dateStr = new Date().toISOString().split("T")[0];
    let html = `<!DOCTYPE NETSCAPE-Bookmark-file-1>
<!-- This is an automatically generated file. -->
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>CMS圖書館書籤</TITLE>
<H1>CMS圖書館書籤</H1>
<DL><p>
    <DT><H3 ADD_DATE="${timestamp}" LAST_MODIFIED="${timestamp}">CMS圖書館</H3>
    <DL><p>
`;

    let totalBooks = 0;
    collections.forEach(col => {
      html += `        <DT><H3 ADD_DATE="${timestamp}" LAST_MODIFIED="${timestamp}">${escapeHtml(col.name)}</H3>\n`;
      html += `        <DL><p>\n`;
      (col.items || []).forEach(it => {
        totalBooks++;
        const title = it.work ? it.work.title : (it.title || "書籍");
        const authors = (it.work && it.work.authors) ? it.work.authors.map(a => a.name).join(", ") : "";
        const displayTitle = authors ? `${title} - ${authors}` : title;
        const workUrl = `${window.location.origin}${BASE_PATH}/reader?work_id=${it.work_id}`;
        html += `            <DT><A HREF="${workUrl}" ADD_DATE="${timestamp}">${escapeHtml(displayTitle)}</A>\n`;
      });
      html += `        </DL><p>\n`;
    });

    html += `    </DL><p>\n</DL><p>`;

    const blob = new Blob([html], { type: "text/html;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `CMS圖書館_書籤_${dateStr}.html`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 1500);

    showCustomAlert({
      title: "書籤匯出成功",
      message: `已成功匯出 <b>${collections.length}</b> 個書單、共 <b>${totalBooks}</b> 筆書籍連結為標準 <code>.html</code> 書籤檔！<br><span style="font-size: 0.8rem; color: var(--text-muted);">可在 Chrome、Safari、Edge、Firefox 點選「匯入書籤」或直接存放於 Google Drive / OneDrive。</span>`,
      icon: "💾",
      type: "success",
      anchor: anchor
    });
  } catch (err) {
    console.error("匯出書籤失敗:", err);
    showCustomAlert({
      title: "匯出失敗",
      message: `匯出書籤失敗: ${err.message}`,
      icon: "❌",
      type: "error",
      anchor: anchor
    });
  }
}

async function exportCollectionsAsJson(anchor) {
  try {
    const collections = await fetchAllCollectionsWithItems();
    const dateStr = new Date().toISOString().split("T")[0];
    const jsonStr = JSON.stringify(collections, null, 2);

    const blob = new Blob([jsonStr], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `CMS圖書館_書單備份_${dateStr}.json`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 1500);

    showCustomAlert({
      title: "JSON 備份匯出成功",
      message: `已成功匯出完整書單資料庫備份檔 (JSON)。`,
      icon: "💾",
      type: "success",
      anchor: anchor
    });
  } catch (err) {
    console.error("匯出備份失敗:", err);
    showCustomAlert({
      title: "匯出失敗",
      message: `匯出備份失敗: ${err.message}`,
      icon: "❌",
      type: "error",
      anchor: anchor
    });
  }
}

async function handleImportBookmarkFile(file, anchor) {
  if (!file) return;
  try {
    const text = await file.text();
    let importedCollections = [];

    if (file.name.endsWith(".json")) {
      const data = JSON.parse(text);
      if (!Array.isArray(data)) throw new Error("JSON 格式不符");
      importedCollections = data;
    } else {
      // HTML Netscape Bookmarks
      const parser = new DOMParser();
      const doc = parser.parseFromString(text, "text/html");
      const h3Elements = doc.querySelectorAll("h3");

      if (h3Elements.length === 0) {
        const links = doc.querySelectorAll("a");
        const items = [];
        links.forEach(a => {
          const href = a.getAttribute("href") || "";
          const title = a.textContent.trim();
          const match = href.match(/work_id=([^&#]+)/);
          if (match) {
            items.push({ work_id: match[1], title: title });
          }
        });
        if (items.length > 0) {
          importedCollections.push({ name: "匯入的書籤", icon: "📁", items: items });
        }
      } else {
        h3Elements.forEach(h3 => {
          const folderName = h3.textContent.trim();
          if (folderName === "CMS圖書館" && h3Elements.length > 1) return;

          let nextDl = h3.nextElementSibling;
          while (nextDl && nextDl.tagName !== "DL") {
            nextDl = nextDl.nextElementSibling;
          }

          const items = [];
          if (nextDl) {
            const links = nextDl.querySelectorAll("a");
            links.forEach(a => {
              const href = a.getAttribute("href") || "";
              const title = a.textContent.trim();
              const match = href.match(/work_id=([^&#]+)/);
              if (match) {
                items.push({ work_id: match[1], title: title });
              }
            });
          }

          if (items.length > 0 || folderName) {
            importedCollections.push({ name: folderName || "自訂書單", icon: "📁", items: items });
          }
        });
      }
    }

    if (importedCollections.length === 0) {
      showCustomAlert({
        title: "未找到有效書單",
        message: "在所選檔案中未找到可解析的 CMS圖書館 書籤或書籍連結。",
        icon: "⚠️",
        anchor: anchor
      });
      return;
    }

    // 寫入後端與本地
    let totalItemsAdded = 0;
    for (const c of importedCollections) {
      try {
        const colRes = await fetch(`${BASE_PATH}/api/collections`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: c.name || "匯入書單", icon: c.icon || "📚" })
        });
        if (colRes.ok) {
          const colData = await colRes.json();
          const colId = colData.collection_id;
          for (const it of (c.items || [])) {
            if (it.work_id && !it.work_id.startsWith("imported_")) {
              await fetch(`${BASE_PATH}/api/collections/${colId}/items`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ work_id: it.work_id })
              });
              totalItemsAdded++;
            }
          }
        }
      } catch (e) {
        console.warn("寫入書單失敗:", e);
      }
    }

    await loadCollectionsList();
    showCustomAlert({
      title: "書籤匯入成功",
      message: `成功匯入 <b>${importedCollections.length}</b> 個書單資料夾、共 <b>${totalItemsAdded}</b> 本書籍！`,
      icon: "🎉",
      type: "success",
      anchor: anchor
    });
  } catch (err) {
    console.error("匯入檔案失敗:", err);
    showCustomAlert({
      title: "匯入失敗",
      message: `解析檔案失敗: ${err.message}`,
      icon: "❌",
      type: "error",
      anchor: anchor
    });
  }
}

// === 快速加入書單 Popover ===
async function openQuickCollection(workId, title) {
  quickTargetWorkId = workId;
  quickTargetWorkTitle = title;
  const modal = document.getElementById("quickCollectionModal");
  modal.classList.add("active");
  const listEl = document.getElementById("quickCollectionList");

  // 競態守門：使用者連續點不同書籍時，舊的回應不得寫進新的選單
  const reqId = ++quickCollectionReqId;
  const isStale = () => reqId !== quickCollectionReqId;

  // 等待態必須可見且會隨時間演進——不得與「空書單」、「載入失敗」共用同一個輸出
  listEl.innerHTML = `<p data-quick-state="loading" style="color: var(--text-muted); text-align: center; padding: 1rem;">載入書單中…</p>`;
  const tLoad = performance.now();
  const slowTimer = setInterval(() => {
    if (isStale()) return clearInterval(slowTimer);
    const el = listEl.querySelector('[data-quick-state="loading"]');
    if (!el) return clearInterval(slowTimer);
    const secs = Math.round((performance.now() - tLoad) / 1000);
    el.innerHTML = `載入書單中…<br><span style="font-size: 0.82rem;">已等待 ${secs} 秒，後端回應較慢</span>`;
  }, 1000);
  const stopSlowTimer = () => clearInterval(slowTimer);

  // 1. 若 Chrome 擴充套件已連線，讀取 Chrome 資料夾
  if (isChromeExtensionAvailable) {
    try {
      const extRes = await callExtension("GET_TREE");
      if (isStale()) { stopSlowTimer(); return; }
      if (extRes.success && extRes.data) {
        stopSlowTimer();
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
  if (isStale()) { stopSlowTimer(); return; }

  // 2. 預設後端/本地資料庫模式
  try {
    const [colsRes, statusRes] = await Promise.all([
      fetch(`${BASE_PATH}/api/collections`),
      fetch(`${BASE_PATH}/api/collections/work/${workId}/status`)
    ]);

    if (!colsRes.ok) throw new Error(`書單清單 HTTP ${colsRes.status}`);
    if (!statusRes.ok) throw new Error(`收藏狀態 HTTP ${statusRes.status}`);

    const collections = await colsRes.json();
    const joinedIds = new Set(await statusRes.json());
    if (isStale()) { stopSlowTimer(); return; }
    stopSlowTimer();

    // 空書單（缺席態）必須與「還在載入」、「載入失敗」在畫面上可區分
    if (!Array.isArray(collections) || collections.length === 0) {
      listEl.innerHTML = `<p data-quick-state="empty" style="color: var(--text-muted); text-align: center; padding: 1rem;">📖 尚未建立任何個人書單<br><span style="font-size: 0.82rem;">可到「📚 個人書單」建立新書單後再回來收藏</span></p>`;
      return;
    }

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
    if (isStale()) { stopSlowTimer(); return; }
    stopSlowTimer();
    listEl.innerHTML = `<p data-quick-state="error" style="color: #ef4444; text-align: center; padding: 1rem;">⚠️ 載入書單失敗<br><span style="font-size: 0.82rem;">${escapeHtml(err && err.message ? err.message : String(err))}</span></p>`;
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
  modal.classList.remove("in-shelf-view");
  const backBtn = document.getElementById("bookstallBackToTreeBtn");
  if (backBtn) backBtn.style.display = "none";
  await loadCategoryTree();
  if (window.innerWidth > 768) {
    await loadShelfWorks(currentActiveCategoryId, "文學與小說", "📚", "文學與小說");
  }
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
           title="點擊查看「${escapeHtml(node.name)}」架位藏書"
           onclick="handleCategoryClick('${node.category_id}', '${escapeJsArg(node.name)}', '${node.icon}', '${escapeJsArg(currentPath)}')">
        <div style="display: flex; align-items: center; gap: 0.35rem; overflow: hidden;">
          ${hasChildren ? `<span class="tree-expander expanded" title="展開/折疊分類" onclick="event.stopPropagation(); toggleTreeNode('${node.category_id}')">▶</span>` : `<span style="width: 1.2rem;"></span>`}
          <span>${node.icon || '📖'}</span>
          <span style="font-size: 0.9rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(node.name)}</span>
        </div>
        <span class="tree-badge" title="共 ${node.works_count} 本藏書">${node.works_count}</span>
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

  const modal = document.getElementById("bookstallModal");
  modal.classList.add("in-shelf-view");
  const backBtn = document.getElementById("bookstallBackToTreeBtn");
  if (backBtn) backBtn.style.display = "inline-block";

  await loadShelfWorks(catId, name, icon, breadcrumbs);
}

async function loadShelfWorks(catId, name, icon, breadcrumbs) {
  document.getElementById("shelfBreadcrumbs").innerText = breadcrumbs;
  document.getElementById("shelfTitle").innerHTML = `${icon || '📖'} ${escapeHtml(name)}`;
  const shelfGrid = document.getElementById("shelfGrid");
  shelfGrid.innerHTML = `<p style="color: var(--text-muted); padding: 2rem; grid-column: 1 / -1; text-align: center;">載入書架藏書中...</p>`;

  try {
    const res = await fetch(`${BASE_PATH}/api/categories/${catId}/works?page=1&page_size=50&include_cloud=true`);
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

    shelfGrid.innerHTML = data.items.map(w => {
      const isLocal = (w.availability_tier === 0 && w.local_work_id) || (w.work_id && !w.availability_tier);
      const targetWorkId = w.local_work_id || w.work_id;

      let badgeHtml = "";
      let actionsHtml = "";

      if (isLocal && targetWorkId) {
        badgeHtml = `<span class="tag tag-local" title="本地典藏已收錄">💾</span>`;
        actionsHtml = `
          <div class="shelf-actions">
            <button class="btn btn-icon btn-primary" onclick="openReader('${targetWorkId}')" title="線上閱讀">
              👁️
            </button>
            <div class="shelf-more-wrap">
              <button class="btn btn-icon btn-outline shelf-more-btn" onclick="toggleShelfDropdown(this, event)" title="更多操作">
                ⋯
              </button>
              <div class="shelf-dropdown-menu">
                <button class="shelf-dropdown-item" onclick="openQuickCollection('${targetWorkId}', '${escapeJsArg(w.title)}'); closeAllDropdowns();">
                  <span>⭐</span>
                  <span>加入個人書單</span>
                </button>
                <a class="shelf-dropdown-item" href="${BASE_PATH}/api/files/${targetWorkId}/raw" download title="下載原檔至本地硬碟" onclick="closeAllDropdowns();">
                  <span>💾</span>
                  <span>下載原檔至本機</span>
                </a>
                <button class="shelf-dropdown-item" onclick="openDetail('${targetWorkId}'); closeAllDropdowns();">
                  <span>ℹ️</span>
                  <span>書籍元資料詳情</span>
                </button>
              </div>
            </div>
          </div>
        `;
      } else {
        badgeHtml = `<span class="tag tag-remote" title="公網雲端精選">🌐</span>`;
        actionsHtml = `
          <div class="shelf-actions">
            <button class="btn btn-icon btn-primary" id="shelf-dl-${w.md5}" onclick="triggerSingleDownload('${w.md5}')" title="鏡像收書至本地">
              📥
            </button>
            <div class="shelf-more-wrap">
              <button class="btn btn-icon btn-outline shelf-more-btn" onclick="toggleShelfDropdown(this, event)" title="更多操作">
                ⋯
              </button>
              <div class="shelf-dropdown-menu">
                <button class="shelf-dropdown-item" onclick="previewLiveDetail('${w.md5}'); closeAllDropdowns();">
                  <span>ℹ️</span>
                  <span>雲端書籍元資料詳情</span>
                </button>
              </div>
            </div>
          </div>
        `;
      }

      return `
        <div class="shelf-book-card">
          <div class="shelf-card-content">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.5rem; gap: 0.35rem;">
              <div style="display: flex; gap: 0.35rem; align-items: center; flex-wrap: wrap;">
                ${badgeHtml}
                ${getFormatTag(w.format)}
              </div>
              <span style="font-size: 0.8rem; color: var(--text-muted); white-space: nowrap;">${w.publication_year ? `${w.publication_year}年` : ''}</span>
            </div>
            <div style="font-weight: 700; font-size: 0.98rem; line-height: 1.4; color: var(--text-primary); margin-bottom: 0.35rem; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;" title="${escapeHtml(w.title)}">
              ${escapeHtml(w.title)}
            </div>
            <div style="font-size: 0.82rem; color: var(--text-secondary); margin-bottom: 0.75rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
              ✍️ ${escapeHtml(w.authors_display || "未知作者")}
            </div>
          </div>
          ${actionsHtml}
        </div>
      `;
    }).join("");
  } catch (err) {
    console.error("載入架位書籍失敗:", err);
    shelfGrid.innerHTML = `<p style="color: #ef4444; padding: 2rem; grid-column: 1 / -1;">載入失敗</p>`;
  }
}

// === 書架卡片「…」更多操作下拉選單控制 ===
function toggleShelfDropdown(btn, event) {
  if (event) event.stopPropagation();
  const wrap = btn.closest(".shelf-more-wrap");
  if (!wrap) return;
  const menu = wrap.querySelector(".shelf-dropdown-menu");
  if (!menu) return;

  const isActive = menu.classList.contains("active");
  closeAllDropdowns();

  if (!isActive) {
    menu.classList.add("active");
  }
}

function closeAllShelfDropdowns() {
  document.querySelectorAll(".shelf-dropdown-menu.active").forEach(m => m.classList.remove("active"));
}

// === 搜尋結果卡片「…」更多操作下拉選單控制 ===
function toggleBookCardDropdown(btn, event) {
  if (event) event.stopPropagation();
  const wrap = btn.closest(".book-more-wrap");
  if (!wrap) return;
  const menu = wrap.querySelector(".book-dropdown-menu");
  if (!menu) return;

  const isActive = menu.classList.contains("active");
  closeAllDropdowns();

  if (!isActive) {
    menu.classList.add("active");
  }
}

function closeAllBookDropdowns() {
  document.querySelectorAll(".book-dropdown-menu.active").forEach(m => m.classList.remove("active"));
}

function closeAllDropdowns() {
  closeAllShelfDropdowns();
  closeAllBookDropdowns();
}

// 全域點選或滾動時自動關閉所有卡片下拉選單
document.addEventListener("click", (e) => {
  if (!e.target.closest(".shelf-more-wrap") && !e.target.closest(".book-more-wrap")) {
    closeAllDropdowns();
  }
});
document.addEventListener("scroll", closeAllDropdowns, true);

