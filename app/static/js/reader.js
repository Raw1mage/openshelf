// openshelf 線上閱讀器核心控制器（支援單頁/雙頁排版、邊緣觸發翻頁與懸浮快速滑桿）

const getBasePath = () => {
  const path = window.location.pathname;
  if (path.startsWith("/libgen")) {
    return "/libgen";
  }
  return "";
};

const BASE_PATH = getBasePath();
const urlParams = new URLSearchParams(window.location.search);
const workId = urlParams.get("work_id");

let pdfDoc = null;
let epubBook = null;
let epubRendition = null;
let currentPageNum = 1;
let totalPagesCount = 1;
let currentScale = 1.25;
let isRendering = false;
let pageRenderingQueue = null;
let bookFormat = "pdf";
let isSpreadMode = false;

let bookTitle = "";

if (!workId) {
  alert("未指定書籍 ID！");
  window.location.href = `${BASE_PATH}/`;
}

if (window.pdfjsLib) {
  pdfjsLib.GlobalWorkerOptions.workerSrc = "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js";
}

document.addEventListener("DOMContentLoaded", async () => {
  await loadBookMetadata();
  initControls();
});

async function loadBookMetadata() {
  try {
    const res = await fetch(`${BASE_PATH}/api/works/${workId}`);
    if (!res.ok) throw new Error("無法讀取書籍資料");
    const data = await res.json();
    bookTitle = data.title;

    document.getElementById("readerTitle").innerText = `《${data.title}》`;
    document.title = `閱讀: ${data.title} — CMS圖書館`;

    const fileUrl = `${BASE_PATH}/api/files/${workId}/raw`;
    const initialPage = data.reading_state ? (data.reading_state.last_page || 1) : 1;

    // 設定 Header 下載全檔按鈕
    const downloadBtn = document.getElementById("downloadFullBtn");
    if (downloadBtn) {
      downloadBtn.href = fileUrl;
      downloadBtn.setAttribute("download", `${data.title || "book"}`);
    }

    let isEpub = false;
    for (const mf of data.manifestations || []) {
      if (mf.format === "epub") {
        isEpub = true;
        break;
      }
    }

    if (isEpub) {
      bookFormat = "epub";
      initEpubReader(fileUrl, initialPage);
    } else {
      bookFormat = "pdf";
      initPdfReader(fileUrl, initialPage);
    }
  } catch (err) {
    alert("載入失敗: " + err.message);
  }
}

async function initPdfReader(url, initialPage) {
  document.getElementById("pdfViewer").style.display = "flex";
  document.getElementById("epubViewer").style.display = "none";
  document.getElementById("readerTitle").innerText = bookTitle ? `《${bookTitle}》` : "正在載入 PDF 檔案...";

  try {
    const loadingTask = pdfjsLib.getDocument({
      url: url,
      cMapUrl: "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/cmaps/",
      cMapPacked: true,
      standardFontDataUrl: "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/standard_fonts/"
    });
    pdfDoc = await loadingTask.promise;
    totalPagesCount = pdfDoc.numPages;
    document.getElementById("totalPages").innerText = totalPagesCount;
    document.getElementById("readerTitle").innerText = `《${bookTitle}》`;

    // 初始化底部滑桿
    const pageSlider = document.getElementById("pageSlider");
    pageSlider.min = 1;
    pageSlider.max = totalPagesCount;
    document.getElementById("scrubberMax").innerText = totalPagesCount;

    currentPageNum = Math.min(Math.max(1, initialPage), totalPagesCount);
    renderPdfPage(currentPageNum);
  } catch (err) {
    console.error("PDF 渲染錯誤:", err);
    document.getElementById("pdfViewer").innerHTML = `
      <div style="color: #ef4444; padding: 2rem; background: var(--bg-secondary); border-radius: 8px; text-align: center;">
        <h3>⚠️ PDF 載入失敗</h3>
        <p style="margin-top: 0.5rem; color: var(--text-secondary);">${err.message}</p>
        <a href="${url}" class="btn btn-primary" style="margin-top: 1rem; display: inline-block;">📥 改以瀏覽器下載閱讀</a>
      </div>
    `;
  }
}

async function renderPdfPage(num) {
  if (!pdfDoc) return;
  isRendering = true;

  const canvas1 = document.getElementById("pdfCanvas1");
  const canvas2 = document.getElementById("pdfCanvas2");

  if (!isSpreadMode) {
    // === 單頁模式 ===
    canvas2.style.display = "none";
    try {
      const page = await pdfDoc.getPage(num);
      const ctx = canvas1.getContext("2d");
      const viewport = page.getViewport({ scale: currentScale });

      canvas1.height = viewport.height;
      canvas1.width = viewport.width;

      await page.render({ canvasContext: ctx, viewport: viewport }).promise;
    } catch (e) {
      console.error(e);
    }
  } else {
    // === 雙頁跨頁模式 ===
    const leftPageNum = (num % 2 === 1) ? num : Math.max(1, num - 1);
    const rightPageNum = leftPageNum + 1;
    const spreadScale = currentScale * 0.85;

    // 左頁
    try {
      const pageLeft = await pdfDoc.getPage(leftPageNum);
      const ctx1 = canvas1.getContext("2d");
      const vp1 = pageLeft.getViewport({ scale: spreadScale });
      canvas1.height = vp1.height;
      canvas1.width = vp1.width;
      canvas1.style.display = "block";
      await pageLeft.render({ canvasContext: ctx1, viewport: vp1 }).promise;
    } catch (e) {}

    // 右頁
    if (rightPageNum <= totalPagesCount) {
      try {
        const pageRight = await pdfDoc.getPage(rightPageNum);
        const ctx2 = canvas2.getContext("2d");
        const vp2 = pageRight.getViewport({ scale: spreadScale });
        canvas2.height = vp2.height;
        canvas2.width = vp2.width;
        canvas2.style.display = "block";
        await pageRight.render({ canvasContext: ctx2, viewport: vp2 }).promise;
      } catch (e) {}
    } else {
      canvas2.style.display = "none";
    }
  }

  isRendering = false;
  if (pageRenderingQueue !== null) {
    const nextNum = pageRenderingQueue;
    pageRenderingQueue = null;
    renderPdfPage(nextNum);
  }

  // 更新頁碼輸入框與懸浮氣泡
  document.getElementById("pageInput").value = num;
  document.getElementById("pageSlider").value = num;
  
  if (isSpreadMode && num + 1 <= totalPagesCount) {
    const p1 = (num % 2 === 1) ? num : num - 1;
    const p2 = Math.min(p1 + 1, totalPagesCount);
    document.getElementById("scrubberBubble").innerText = `第 ${p1}-${p2} 頁`;
  } else {
    document.getElementById("scrubberBubble").innerText = `第 ${num} 頁`;
  }

  syncProgress(num, totalPagesCount);
}

function queueRenderPage(num) {
  if (isRendering) {
    pageRenderingQueue = num;
  } else {
    renderPdfPage(num);
  }
}

async function initEpubReader(url, initialPage) {
  document.getElementById("pdfViewer").style.display = "none";
  const epubContainer = document.getElementById("epubViewer");
  epubContainer.style.display = "block";
  document.getElementById("readerTitle").innerText = bookTitle ? `《${bookTitle}》` : "正在載入 EPUB 檔案...";

  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const buffer = await res.arrayBuffer();

    epubBook = ePub(buffer);
    await epubBook.ready;

    epubRendition = epubBook.renderTo(epubContainer, {
      width: "100%",
      height: "100%",
      spread: isSpreadMode ? "always" : "none",
      flow: "paginated"
    });

    // 尋找第一個實際存在且非空/非無效的章節（自動略過損毀的空 titlepage 節點）
    let initialTarget = undefined;
    if (epubBook.spine && epubBook.spine.spineItems && epubBook.spine.spineItems.length > 0) {
      for (const item of epubBook.spine.spineItems) {
        if (item && item.href && !item.href.includes("titlepage.xhtml")) {
          initialTarget = item.href;
          break;
        }
      }
    }

    await epubRendition.display(initialTarget);

    epubRendition.themes.default({
      body: {
        color: "#e2e8f0 !important",
        background: "#0f172a !important",
        "font-family": "system-ui, -apple-system, sans-serif !important",
        "line-height": "1.8 !important",
        "padding": "0 1.5rem !important"
      },
      p: {
        color: "#cbd5e1 !important",
        "font-size": "1.1rem !important",
        "margin-bottom": "1.2rem !important"
      },
      h1: { color: "#38bdf8 !important" },
      h2: { color: "#38bdf8 !important" },
      h3: { color: "#38bdf8 !important" },
      a: { color: "#38bdf8 !important" }
    });

    document.getElementById("readerTitle").innerText = `《${bookTitle}》`;

    // 產生章節頁面定位
    epubBook.ready.then(() => {
      return epubBook.locations.generate(1000);
    }).then(() => {
      const total = epubBook.locations.total || 100;
      totalPagesCount = total;
      document.getElementById("totalPages").innerText = total;
      const pageSlider = document.getElementById("pageSlider");
      pageSlider.max = total;
      document.getElementById("scrubberMax").innerText = total;
    });

    epubRendition.on("relocated", location => {
      if (location && location.start) {
        const cfi = location.start.cfi;
        const progress = epubBook.locations.percentageFromCfi(cfi) || 0;
        const pageEst = Math.max(1, Math.round(progress * (epubBook.locations.total || 100)));
        currentPageNum = pageEst;
        document.getElementById("pageInput").value = pageEst;
        document.getElementById("pageSlider").value = pageEst;
        document.getElementById("scrubberBubble").innerText = `進度 ${Math.round(progress * 100)}%`;
        syncProgress(pageEst, epubBook.locations.total || 100);
      }
    });
  } catch (err) {
    console.error("EPUB 載入失敗:", err);
    epubContainer.innerHTML = `
      <div style="color: #ef4444; padding: 2rem; background: var(--bg-secondary); border-radius: 8px; text-align: center;">
        <h3>⚠️ EPUB 載入失敗</h3>
        <p style="margin-top: 0.5rem; color: var(--text-secondary);">${err.message}</p>
        <a href="${url}" class="btn btn-primary" style="margin-top: 1rem; display: inline-block;">📥 改以瀏覽器下載閱讀</a>
      </div>
    `;
  }
}

function prevPage() {
  if (bookFormat === "pdf") {
    const step = isSpreadMode ? 2 : 1;
    if (currentPageNum <= 1) return;
    currentPageNum = Math.max(1, currentPageNum - step);
    queueRenderPage(currentPageNum);
  } else if (epubRendition) {
    epubRendition.prev();
  }
}

function nextPage() {
  if (bookFormat === "pdf") {
    const step = isSpreadMode ? 2 : 1;
    if (currentPageNum >= totalPagesCount) return;
    currentPageNum = Math.min(totalPagesCount, currentPageNum + step);
    queueRenderPage(currentPageNum);
  } else if (epubRendition) {
    epubRendition.next();
  }
}

function initControls() {
  document.getElementById("prevPageBtn").addEventListener("click", prevPage);
  document.getElementById("nextPageBtn").addEventListener("click", nextPage);

  // 畫面左/右邊緣感測翻頁
  document.getElementById("zoneLeft").addEventListener("click", prevPage);
  document.getElementById("zoneRight").addEventListener("click", nextPage);

  // 雙頁/單頁模式偏好記憶與切換
  const spreadBtn = document.getElementById("spreadToggleBtn");
  const savedSpread = localStorage.getItem("cms_reader_spread");
  if (savedSpread !== null) {
    isSpreadMode = savedSpread === "true";
  } else {
    isSpreadMode = window.innerWidth >= 1100;
  }

  spreadBtn.innerText = isSpreadMode ? "📄" : "📖";
  spreadBtn.title = isSpreadMode ? "切換單頁模式" : "切換雙頁模式";
  if (isSpreadMode) {
    spreadBtn.classList.add("active");
  } else {
    spreadBtn.classList.remove("active");
  }

  spreadBtn.addEventListener("click", () => {
    isSpreadMode = !isSpreadMode;
    spreadBtn.innerText = isSpreadMode ? "📄" : "📖";
    spreadBtn.title = isSpreadMode ? "切換單頁模式" : "切換雙頁模式";
    if (isSpreadMode) {
      spreadBtn.classList.add("active");
    } else {
      spreadBtn.classList.remove("active");
    }
    localStorage.setItem("cms_reader_spread", isSpreadMode);
    if (bookFormat === "pdf") {
      queueRenderPage(currentPageNum);
    } else if (epubRendition) {
      epubRendition.spread(isSpreadMode ? "always" : "none");
    }
  });

  // 滑鼠拖曳平移畫面 (Mouse Pan & Drag to Scroll)
  initPanAndDrag();

  // 頁碼直接輸入
  document.getElementById("pageInput").addEventListener("change", (e) => {
    const val = parseInt(e.target.value);
    if (bookFormat === "pdf" && !isNaN(val) && val >= 1 && val <= totalPagesCount) {
      currentPageNum = val;
      queueRenderPage(currentPageNum);
    }
  });

  // 底部懸浮滑桿快速翻頁
  const pageSlider = document.getElementById("pageSlider");
  const scrubberBubble = document.getElementById("scrubberBubble");
  const floatingScrubber = document.getElementById("floatingScrubber");

  pageSlider.addEventListener("input", (e) => {
    const targetPage = parseInt(e.target.value);
    scrubberBubble.innerText = `第 ${targetPage} 頁`;
    floatingScrubber.classList.add("active");
  });

  pageSlider.addEventListener("change", (e) => {
    const targetPage = parseInt(e.target.value);
    if (bookFormat === "pdf" && !isNaN(targetPage) && targetPage >= 1 && targetPage <= totalPagesCount) {
      currentPageNum = targetPage;
      queueRenderPage(currentPageNum);
    }
    setTimeout(() => floatingScrubber.classList.remove("active"), 1200);
  });

  // 滑鼠移至底部 120px 時主動喚醒滑桿
  window.addEventListener("mousemove", (e) => {
    if (window.innerHeight - e.clientY < 100) {
      floatingScrubber.classList.add("active");
    } else if (!pageSlider.matches(":focus") && !floatingScrubber.matches(":hover")) {
      floatingScrubber.classList.remove("active");
    }
  });

  // 縮放控制
  document.getElementById("zoomInBtn").addEventListener("click", () => {
    if (bookFormat === "pdf") {
      currentScale += 0.2;
      queueRenderPage(currentPageNum);
    }
  });

  document.getElementById("zoomOutBtn").addEventListener("click", () => {
    if (bookFormat === "pdf" && currentScale > 0.5) {
      currentScale -= 0.2;
      queueRenderPage(currentPageNum);
    }
  });

  document.getElementById("fullscreenBtn").addEventListener("click", () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen();
    } else {
      document.exitFullscreen();
    }
  });

  // 鍵盤方向鍵監聽
  window.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") {
      prevPage();
    } else if (e.key === "ArrowRight" || e.key === "PageDown" || e.key === " ") {
      nextPage();
    }
  });
}

async function syncProgress(page, total) {
  const ratio = total > 0 ? page / total : 0;
  try {
    await fetch(`${BASE_PATH}/api/progress/${workId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        progress_ratio: ratio,
        last_page: page,
        total_pages: total
      })
    });
  } catch (err) {
    console.error("進度同步失敗:", err);
  }
}

function initPanAndDrag() {
  const container = document.getElementById("readerContainer");
  if (!container) return;

  let isDown = false;
  let startX, startY;
  let scrollLeft, scrollTop;

  container.addEventListener("mousedown", (e) => {
    // 忽略邊緣翻頁區塊、底部滑桿或文字輸入框
    if (e.target.closest(".page-edge-zone") || e.target.closest(".floating-scrubber") || e.target.tagName === "INPUT" || e.target.tagName === "BUTTON") {
      return;
    }
    isDown = true;
    container.classList.add("is-dragging");
    startX = e.pageX - container.offsetLeft;
    startY = e.pageY - container.offsetTop;
    scrollLeft = container.scrollLeft;
    scrollTop = container.scrollTop;
  });

  window.addEventListener("mouseup", () => {
    if (isDown) {
      isDown = false;
      container.classList.remove("is-dragging");
    }
  });

  window.addEventListener("mousemove", (e) => {
    if (!isDown) return;
    e.preventDefault();
    const x = e.pageX - container.offsetLeft;
    const y = e.pageY - container.offsetTop;
    const walkX = (x - startX);
    const walkY = (y - startY);
    container.scrollLeft = scrollLeft - walkX;
    container.scrollTop = scrollTop - walkY;
  });
}
