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
  showCustomAlert({
    title: "提示",
    message: "未指定書籍 ID！即將返回圖書館首頁。",
    type: "warning",
    icon: "⚠️"
  }).then(() => {
    window.location.href = `${BASE_PATH}/`;
  });
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
    showCustomAlert({
      title: "載入失敗",
      message: `書籍載入失敗: ${err.message}`,
      type: "error",
      icon: "❌"
    });
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

    // 手機端預設最適螢幕寬度比例
    if (window.innerWidth <= 768) {
      try {
        const firstPage = await pdfDoc.getPage(1);
        const unscaledVp = firstPage.getViewport({ scale: 1.0 });
        currentScale = Math.min(2.5, Math.max(0.6, (window.innerWidth - 12) / unscaledVp.width));
      } catch (e) {
        currentScale = 0.95;
      }
    }

    // 初始化底部滑桿
    const pageSlider = document.getElementById("pageSlider");
    pageSlider.min = 1;
    pageSlider.max = totalPagesCount;
    document.getElementById("scrubberMax").innerText = totalPagesCount;

    currentPageNum = Math.min(Math.max(1, initialPage), totalPagesCount);
    renderPdfPage(currentPageNum);
    showUI(2000);
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

    // 綁定 EPUB iframe 內部單擊翻頁、滑動翻頁與雙擊/中央喚醒 UI 事件
    epubRendition.on("rendered", (section, view) => {
      if (view && view.document) {
        let epubTouchStartX = 0;
        let epubTouchStartY = 0;
        let epubTouchStartTime = 0;
        let lastEpubTapTime = 0;
        let lastEpubTapX = 0;
        let lastEpubTapY = 0;

        view.document.addEventListener("touchstart", (e) => {
          if (e.touches.length === 1) {
            epubTouchStartX = e.touches[0].clientX;
            epubTouchStartY = e.touches[0].clientY;
            epubTouchStartTime = Date.now();
          }
        }, { passive: true });

        view.document.addEventListener("touchend", (e) => {
          if (e.changedTouches && e.changedTouches.length === 1) {
            const touch = e.changedTouches[0];
            const now = Date.now();
            const deltaX = touch.clientX - epubTouchStartX;
            const deltaY = touch.clientY - epubTouchStartY;
            const moveDist = Math.hypot(deltaX, deltaY);
            const elapsed = now - epubTouchStartTime;

            // 1. 水平快速滑動翻頁
            if (Math.abs(deltaX) > 40 && Math.abs(deltaY) < 65 && elapsed < 450) {
              if (deltaX < -40) {
                nextPage();
              } else if (deltaX > 40) {
                prevPage();
              }
              lastEpubTapTime = 0;
              return;
            }

            // 2. 單擊 / 雙擊觸控處理
            if (moveDist < 20 && elapsed < 400) {
              const doubleTapDist = Math.hypot(touch.clientX - lastEpubTapX, touch.clientY - lastEpubTapY);
              if (now - lastEpubTapTime < 320 && doubleTapDist < 35) {
                toggleUI();
                lastEpubTapTime = 0;
              } else {
                lastEpubTapTime = now;
                lastEpubTapX = touch.clientX;
                lastEpubTapY = touch.clientY;

                const screenW = view.document.documentElement.clientWidth || window.innerWidth;
                const ratioX = touch.clientX / screenW;
                if (ratioX < 0.35) {
                  prevPage();
                } else if (ratioX > 0.65) {
                  nextPage();
                } else {
                  toggleUI();
                }
              }
            }
          }
        }, { passive: true });

        view.document.addEventListener("click", (e) => {
          const screenW = view.document.documentElement.clientWidth || window.innerWidth;
          const ratioX = e.clientX / screenW;
          if (ratioX < 0.35) {
            prevPage();
          } else if (ratioX > 0.65) {
            nextPage();
          } else {
            toggleUI();
          }
        });

        view.document.addEventListener("dblclick", () => toggleUI());
      }
    });

    // 尋找第一個實際存在且非空/非無效的章節
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
    showUI(2000);

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

let uiVisible = true;
let hideTimer = null;

function showUI(autoHideMs = 2000) {
  const header = document.getElementById("readerHeader");
  const scrubber = document.getElementById("floatingScrubber");
  const zoneLeft = document.getElementById("zoneLeft");
  const zoneRight = document.getElementById("zoneRight");

  if (header) header.classList.remove("ui-hidden");
  if (scrubber) scrubber.classList.remove("ui-hidden");
  if (zoneLeft) zoneLeft.classList.remove("ui-hidden");
  if (zoneRight) zoneRight.classList.remove("ui-hidden");
  uiVisible = true;

  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }

  if (autoHideMs > 0) {
    hideTimer = setTimeout(() => {
      hideUI();
    }, autoHideMs);
  }
}

function hideUI() {
  const header = document.getElementById("readerHeader");
  const scrubber = document.getElementById("floatingScrubber");
  const zoneLeft = document.getElementById("zoneLeft");
  const zoneRight = document.getElementById("zoneRight");

  if (header) header.classList.add("ui-hidden");
  if (scrubber) scrubber.classList.add("ui-hidden");
  if (zoneLeft) zoneLeft.classList.add("ui-hidden");
  if (zoneRight) zoneRight.classList.add("ui-hidden");
  uiVisible = false;

  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
}

function toggleUI() {
  if (uiVisible) {
    hideUI();
  } else {
    showUI(2000);
  }
}

function initControls() {
  document.getElementById("prevPageBtn").addEventListener("click", () => {
    prevPage();
    showUI(2000);
  });
  document.getElementById("nextPageBtn").addEventListener("click", () => {
    nextPage();
    showUI(2000);
  });

  // 畫面左/右邊緣感測翻頁（按一下立刻翻頁，永不 disable）
  const zoneLeft = document.getElementById("zoneLeft");
  const zoneRight = document.getElementById("zoneRight");

  if (zoneLeft) {
    zoneLeft.addEventListener("click", (e) => {
      e.stopPropagation();
      prevPage();
    });
    zoneLeft.addEventListener("touchend", (e) => {
      e.stopPropagation();
      prevPage();
    }, { passive: true });
  }

  if (zoneRight) {
    zoneRight.addEventListener("click", (e) => {
      e.stopPropagation();
      nextPage();
    });
    zoneRight.addEventListener("touchend", (e) => {
      e.stopPropagation();
      nextPage();
    }, { passive: true });
  }

  // 手機全螢幕手勢：單擊翻頁（點左側/點右側）、雙擊/中央喚醒、左右滑動翻頁
  let touchStartX = 0;
  let touchStartY = 0;
  let touchStartTime = 0;
  let lastTapTime = 0;
  let lastTapX = 0;
  let lastTapY = 0;

  window.addEventListener("touchstart", (e) => {
    if (e.touches.length === 1) {
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
      touchStartTime = Date.now();
    } else if (e.touches.length === 2) {
      isPinching = true;
      pinchStartDistance = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      pinchStartScale = currentScale;
    }
  }, { passive: true });

  window.addEventListener("touchend", (e) => {
    // 若點擊在控制列本體或按鈕上，交由原生事件處理
    if (e.target.closest("#readerHeader") || e.target.closest("#floatingScrubber") || e.target.closest("button") || e.target.closest("input") || e.target.closest(".page-edge-zone")) {
      return;
    }

    if (isPinching) {
      isPinching = false;
      const canvas1 = document.getElementById("pdfCanvas1");
      const canvas2 = document.getElementById("pdfCanvas2");
      if (canvas1) canvas1.style.transform = "";
      if (canvas2) canvas2.style.transform = "";
      return;
    }

    if (e.changedTouches && e.changedTouches.length === 1) {
      const touch = e.changedTouches[0];
      const now = Date.now();
      const deltaX = touch.clientX - touchStartX;
      const deltaY = touch.clientY - touchStartY;
      const moveDist = Math.hypot(deltaX, deltaY);
      const elapsed = now - touchStartTime;

      // 1. 水平快速滑動翻頁 (Swipe Gesture)
      if (Math.abs(deltaX) > 40 && Math.abs(deltaY) < 65 && elapsed < 450) {
        if (deltaX < -40) {
          nextPage(); // 向左滑 ➔ 下一頁
        } else if (deltaX > 40) {
          prevPage(); // 向右滑 ➔ 上一頁
        }
        lastTapTime = 0;
        return;
      }

      // 2. 單擊 / 雙擊觸控處理（手指移動小於 20px 且時間小於 400ms）
      if (moveDist < 20 && elapsed < 400) {
        const doubleTapDist = Math.hypot(touch.clientX - lastTapX, touch.clientY - lastTapY);
        
        if (now - lastTapTime < 320 && doubleTapDist < 35) {
          // 雙擊畫面：喚醒 / 隱藏控制列
          toggleUI();
          lastTapTime = 0;
        } else {
          lastTapTime = now;
          lastTapX = touch.clientX;
          lastTapY = touch.clientY;

          // 單擊畫面：按一下立刻翻頁！
          const screenW = window.innerWidth;
          const ratioX = touch.clientX / screenW;

          if (ratioX < 0.35) {
            // 點擊左側 35% ➔ 按一下立刻翻上一頁
            prevPage();
          } else if (ratioX > 0.65) {
            // 點擊右側 35% ➔ 按一下立刻翻下一頁
            nextPage();
          } else {
            // 點擊中央 30% ➔ 喚醒或切換控制列
            toggleUI();
          }
        }
      }
    }
  }, { passive: true });

  // 桌面滑鼠點擊畫面左右側直接翻頁
  const readerContainer = document.getElementById("readerContainer");
  if (readerContainer) {
    readerContainer.addEventListener("click", (e) => {
      if (readerContainer.classList.contains("is-dragging") || e.target.closest("#readerHeader") || e.target.closest("#floatingScrubber") || e.target.closest("button") || e.target.closest("input") || e.target.closest(".page-edge-zone")) {
        return;
      }
      const screenW = window.innerWidth;
      const ratioX = e.clientX / screenW;
      if (ratioX < 0.35) {
        prevPage();
      } else if (ratioX > 0.65) {
        nextPage();
      } else {
        toggleUI();
      }
    });
  }

  window.addEventListener("dblclick", (e) => {
    if (e.target.closest("#readerHeader") || e.target.closest("#floatingScrubber")) return;
    toggleUI();
  });

  // 控制條觸控與懸停時維持顯示
  const floatingScrubberEl = document.getElementById("floatingScrubber");
  const readerHeaderEl = document.getElementById("readerHeader");
  
  [floatingScrubberEl, readerHeaderEl].forEach(el => {
    if (!el) return;
    el.addEventListener("touchstart", () => {
      if (hideTimer) clearTimeout(hideTimer);
    }, { passive: true });
    el.addEventListener("mouseenter", () => {
      if (hideTimer) clearTimeout(hideTimer);
    });
    el.addEventListener("mouseleave", () => {
      if (uiVisible) showUI(2000);
    });
  });

  // 雙指手勢縮放 (Multi-Touch Pinch-to-Zoom)
  let pinchStartDistance = 0;
  let pinchStartScale = 1.25;
  let isPinching = false;

  window.addEventListener("touchstart", (e) => {
    if (e.touches.length === 2) {
      isPinching = true;
      pinchStartDistance = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      pinchStartScale = currentScale;
    }
  }, { passive: true });

  window.addEventListener("touchmove", (e) => {
    if (isPinching && e.touches.length === 2 && pinchStartDistance > 0) {
      const currentDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      const scaleMultiplier = currentDist / pinchStartDistance;
      
      const canvas1 = document.getElementById("pdfCanvas1");
      const canvas2 = document.getElementById("pdfCanvas2");
      if (canvas1) {
        canvas1.style.transform = `scale(${scaleMultiplier})`;
        canvas1.style.transformOrigin = "center center";
      }
      if (canvas2) {
        canvas2.style.transform = `scale(${scaleMultiplier})`;
        canvas2.style.transformOrigin = "center center";
      }
    }
  }, { passive: true });

  window.addEventListener("touchend", (e) => {
    if (isPinching && e.touches.length < 2) {
      isPinching = false;
      const canvas1 = document.getElementById("pdfCanvas1");
      const canvas2 = document.getElementById("pdfCanvas2");
      if (canvas1) canvas1.style.transform = "";
      if (canvas2) canvas2.style.transform = "";
      
      if (pinchStartDistance > 0 && e.changedTouches && e.changedTouches.length > 0) {
        const lastDist = Math.hypot(
          e.changedTouches[0].clientX - (e.touches[0] ? e.touches[0].clientX : e.changedTouches[0].clientX),
          e.changedTouches[0].clientY - (e.touches[0] ? e.touches[0].clientY : e.changedTouches[0].clientY)
        );
        if (lastDist > 0) {
          const multiplier = lastDist / pinchStartDistance;
          if (multiplier > 1.15 || multiplier < 0.85) {
            currentScale = Math.min(3.5, Math.max(0.5, currentScale * multiplier));
            if (bookFormat === "pdf") {
              queueRenderPage(currentPageNum);
            }
          }
        }
      }
      pinchStartDistance = 0;
    }
  }, { passive: true });

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
    showUI(2000);
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
    showUI(2000);
  });

  // 底部懸浮滑桿快速翻頁
  const pageSlider = document.getElementById("pageSlider");
  const scrubberBubble = document.getElementById("scrubberBubble");

  pageSlider.addEventListener("input", (e) => {
    if (hideTimer) clearTimeout(hideTimer);
    const targetPage = parseInt(e.target.value);
    scrubberBubble.innerText = `第 ${targetPage} 頁`;
  });

  pageSlider.addEventListener("change", (e) => {
    const targetPage = parseInt(e.target.value);
    if (bookFormat === "pdf" && !isNaN(targetPage) && targetPage >= 1 && targetPage <= totalPagesCount) {
      currentPageNum = targetPage;
      queueRenderPage(currentPageNum);
    }
    showUI(2000);
  });

  // 縮放控制
  document.getElementById("zoomInBtn").addEventListener("click", () => {
    if (bookFormat === "pdf") {
      currentScale += 0.2;
      queueRenderPage(currentPageNum);
    }
    showUI(2000);
  });

  document.getElementById("zoomOutBtn").addEventListener("click", () => {
    if (bookFormat === "pdf" && currentScale > 0.5) {
      currentScale -= 0.2;
      queueRenderPage(currentPageNum);
    }
    showUI(2000);
  });

  document.getElementById("fullscreenBtn").addEventListener("click", () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen();
    } else {
      document.exitFullscreen();
    }
    showUI(2000);
  });

  // 鍵盤方向鍵監聽
  window.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") {
      prevPage();
      showUI(2000);
    } else if (e.key === "ArrowRight" || e.key === "PageDown" || e.key === " ") {
      nextPage();
      showUI(2000);
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
    startX = e.pageX - container.offsetLeft;
    startY = e.pageY - container.offsetTop;
    scrollLeft = container.scrollLeft;
    scrollTop = container.scrollTop;
  });

  window.addEventListener("mouseup", (e) => {
    if (isDown) {
      isDown = false;
      const walkX = (e.pageX - container.offsetLeft) - startX;
      const walkY = (e.pageY - container.offsetTop) - startY;
      
      // 若水平拖曳超過 55px 且未大幅放大橫向捲動，觸發滑動翻頁
      if (Math.abs(walkX) > 55 && Math.abs(walkY) < 70 && container.scrollWidth <= container.clientWidth + 40) {
        if (walkX < -55) {
          nextPage();
        } else if (walkX > 55) {
          prevPage();
        }
      }
      setTimeout(() => container.classList.remove("is-dragging"), 60);
    }
  });

  window.addEventListener("mousemove", (e) => {
    if (!isDown) return;
    const x = e.pageX - container.offsetLeft;
    const y = e.pageY - container.offsetTop;
    const walkX = (x - startX);
    const walkY = (y - startY);
    if (Math.abs(walkX) > 10 || Math.abs(walkY) > 10) {
      container.classList.add("is-dragging");
    }
    container.scrollLeft = scrollLeft - walkX;
    container.scrollTop = scrollTop - walkY;
  });
}
