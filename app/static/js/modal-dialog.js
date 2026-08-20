/**
 * OpenShelf 通用浮動 Modal / Popover / 對話框核心模組
 * 支援 Anchor 觸發點附著定位、自動邊界防溢出、鍵盤快速鍵與流暢動畫。
 * 全面替代瀏覽器原生 prompt(), confirm(), alert()。
 */

(function () {
  let activeDialogResolver = null;
  let activeContainer = null;

  function createDialogDOM() {
    let container = document.getElementById("customDialogContainer");
    if (!container) {
      container = document.createElement("div");
      container.id = "customDialogContainer";
      container.className = "custom-dialog-container";
      container.innerHTML = `
        <div class="custom-dialog-backdrop" id="customDialogBackdrop"></div>
        <div class="custom-dialog-card" id="customDialogCard" role="dialog" aria-modal="true">
          <div class="custom-dialog-arrow" id="customDialogArrow"></div>
          <div class="custom-dialog-header">
            <div class="custom-dialog-title-wrap">
              <span class="custom-dialog-icon" id="customDialogIcon">💬</span>
              <h3 class="custom-dialog-title" id="customDialogTitle">提示</h3>
            </div>
            <button class="custom-dialog-close" id="customDialogCloseBtn" title="關閉">&times;</button>
          </div>
          <div class="custom-dialog-body" id="customDialogBody">
            <p class="custom-dialog-message" id="customDialogMessage"></p>
            <div class="custom-dialog-input-wrap" id="customDialogInputWrap" style="display: none;">
              <input type="text" class="custom-dialog-input" id="customDialogInput" autocomplete="off" spellcheck="false">
            </div>
          </div>
          <div class="custom-dialog-footer" id="customDialogFooter">
            <button class="btn btn-outline" id="customDialogCancelBtn">取消</button>
            <button class="btn btn-primary" id="customDialogConfirmBtn">確定</button>
          </div>
        </div>
      `;
      document.body.appendChild(container);

      // 事件綁定
      const backdrop = document.getElementById("customDialogBackdrop");
      const closeBtn = document.getElementById("customDialogCloseBtn");
      const cancelBtn = document.getElementById("customDialogCancelBtn");
      const confirmBtn = document.getElementById("customDialogConfirmBtn");
      const inputEl = document.getElementById("customDialogInput");

      const handleCancel = () => {
        if (activeDialogResolver) {
          const resolve = activeDialogResolver;
          closeCustomDialog();
          resolve(null);
        }
      };

      const handleConfirm = () => {
        if (activeDialogResolver) {
          const resolve = activeDialogResolver;
          const isPrompt = inputEl.parentElement.style.display !== "none";
          const val = isPrompt ? inputEl.value : true;
          closeCustomDialog();
          resolve(val);
        }
      };

      backdrop.addEventListener("click", handleCancel);
      closeBtn.addEventListener("click", handleCancel);
      cancelBtn.addEventListener("click", handleCancel);
      confirmBtn.addEventListener("click", handleConfirm);

      inputEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          handleConfirm();
        } else if (e.key === "Escape") {
          e.preventDefault();
          handleCancel();
        }
      });

      window.addEventListener("keydown", (e) => {
        if (container.classList.contains("active") && e.key === "Escape") {
          e.preventDefault();
          handleCancel();
        }
      });

      // 監聽視窗縮放與滾動時重新調整位置
      window.addEventListener("resize", () => {
        if (container.classList.contains("active") && container._currentAnchor) {
          positionDialog(container._currentAnchor);
        }
      });
    }
    return container;
  }

  function resolveAnchorRect(anchor) {
    if (!anchor) return null;
    if (anchor instanceof HTMLElement) {
      return anchor.getBoundingClientRect();
    }
    if (anchor.target instanceof HTMLElement) {
      return anchor.target.getBoundingClientRect();
    }
    if (anchor.currentTarget instanceof HTMLElement) {
      return anchor.currentTarget.getBoundingClientRect();
    }
    if (typeof anchor.clientX === "number" && typeof anchor.clientY === "number") {
      return {
        top: anchor.clientY,
        bottom: anchor.clientY + 2,
        left: anchor.clientX,
        right: anchor.clientX + 2,
        width: 2,
        height: 2
      };
    }
    if (typeof anchor.top === "number" && typeof anchor.left === "number") {
      return anchor;
    }
    return null;
  }

  function positionDialog(anchor) {
    const container = document.getElementById("customDialogContainer");
    const card = document.getElementById("customDialogCard");
    const arrow = document.getElementById("customDialogArrow");
    if (!container || !card) return;

    container._currentAnchor = anchor;
    const rect = resolveAnchorRect(anchor);
    const isMobile = window.innerWidth <= 640;

    // 手機端或無錨點時：中央對齊模式
    if (isMobile || !rect) {
      card.classList.remove("anchored");
      card.classList.remove("arrow-top", "arrow-bottom");
      card.style.left = "";
      card.style.top = "";
      card.style.bottom = "";
      card.style.right = "";
      card.style.transform = "";
      if (arrow) arrow.style.display = "none";
      return;
    }

    // 桌面端：精確附著於觸發點附近
    card.classList.add("anchored");
    if (arrow) arrow.style.display = "block";

    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const cardWidth = Math.min(380, vw - 32);
    card.style.width = `${cardWidth}px`;

    // 取得卡片高度預估或實測值
    const cardHeight = card.offsetHeight || 220;

    // 垂直方向判定（優先下方，空間不足則上方）
    let top = rect.bottom + 10;
    let arrowPlacement = "arrow-top"; // 箭頭朝上（卡片在按鈕下方）

    if (top + cardHeight > vh - 20) {
      // 下方空間不夠，改放上方
      top = rect.top - cardHeight - 10;
      arrowPlacement = "arrow-bottom"; // 箭頭朝下（卡片在按鈕上方）
    }

    // 邊界防溢出 clamping
    top = Math.max(12, Math.min(top, vh - cardHeight - 12));

    // 水平方向判定（盡可能以 anchor 中心對齊）
    const anchorCenterX = rect.left + rect.width / 2;
    let left = anchorCenterX - cardWidth / 2;

    // 邊界防溢出 clamping
    const minLeft = 16;
    const maxLeft = vw - cardWidth - 16;
    left = Math.max(minLeft, Math.min(left, maxLeft));

    // 箭頭水平位置
    let arrowLeft = anchorCenterX - left;
    arrowLeft = Math.max(20, Math.min(arrowLeft, cardWidth - 20));

    card.classList.remove("arrow-top", "arrow-bottom");
    card.classList.add(arrowPlacement);

    card.style.left = `${left}px`;
    card.style.top = `${top}px`;
    card.style.right = "auto";
    card.style.bottom = "auto";

    if (arrow) {
      arrow.style.left = `${arrowLeft}px`;
    }
  }

  function showCustomPrompt({
    title = "請輸入",
    message = "",
    defaultValue = "",
    placeholder = "請輸入內容...",
    confirmText = "確定",
    cancelText = "取消",
    icon = "✏️",
    isDanger = false,
    anchor = null
  } = {}) {
    return new Promise((resolve) => {
      const container = createDialogDOM();
      activeDialogResolver = resolve;

      document.getElementById("customDialogIcon").innerText = icon;
      document.getElementById("customDialogTitle").innerText = title;
      
      const msgEl = document.getElementById("customDialogMessage");
      msgEl.innerHTML = message;
      msgEl.style.display = message ? "block" : "none";

      const inputWrap = document.getElementById("customDialogInputWrap");
      const inputEl = document.getElementById("customDialogInput");
      inputWrap.style.display = "block";
      inputEl.value = defaultValue || "";
      inputEl.placeholder = placeholder || "";

      const cancelBtn = document.getElementById("customDialogCancelBtn");
      cancelBtn.style.display = "inline-flex";
      cancelBtn.innerText = cancelText;

      const confirmBtn = document.getElementById("customDialogConfirmBtn");
      confirmBtn.innerText = confirmText;
      confirmBtn.className = isDanger ? "btn btn-danger" : "btn btn-primary";

      container.classList.add("active");
      positionDialog(anchor);

      // 自動聚焦與選取預設文字
      setTimeout(() => {
        inputEl.focus();
        if (defaultValue) {
          inputEl.select();
        }
      }, 50);
    });
  }

  function showCustomConfirm({
    title = "確認操作",
    message = "您確定要執行此操作嗎？",
    confirmText = "確定",
    cancelText = "取消",
    icon = "❓",
    isDanger = false,
    anchor = null
  } = {}) {
    return new Promise((resolve) => {
      const container = createDialogDOM();
      activeDialogResolver = (val) => resolve(Boolean(val));

      document.getElementById("customDialogIcon").innerText = icon;
      document.getElementById("customDialogTitle").innerText = title;
      
      const msgEl = document.getElementById("customDialogMessage");
      msgEl.innerHTML = message;
      msgEl.style.display = "block";

      document.getElementById("customDialogInputWrap").style.display = "none";

      const cancelBtn = document.getElementById("customDialogCancelBtn");
      cancelBtn.style.display = "inline-flex";
      cancelBtn.innerText = cancelText;

      const confirmBtn = document.getElementById("customDialogConfirmBtn");
      confirmBtn.innerText = confirmText;
      confirmBtn.className = isDanger ? "btn btn-danger" : "btn btn-primary";

      container.classList.add("active");
      positionDialog(anchor);

      setTimeout(() => {
        confirmBtn.focus();
      }, 50);
    });
  }

  function showCustomAlert({
    title = "提示",
    message = "",
    confirmText = "好",
    icon = null,
    type = "info", // info | success | warning | error
    anchor = null
  } = {}) {
    return new Promise((resolve) => {
      const container = createDialogDOM();
      activeDialogResolver = () => resolve(true);

      let defaultIcon = "💬";
      if (type === "success") defaultIcon = "✅";
      else if (type === "warning") defaultIcon = "⚠️";
      else if (type === "error") defaultIcon = "❌";
      else if (type === "info") defaultIcon = "ℹ️";

      document.getElementById("customDialogIcon").innerText = icon || defaultIcon;
      document.getElementById("customDialogTitle").innerText = title;
      
      const msgEl = document.getElementById("customDialogMessage");
      msgEl.innerHTML = message;
      msgEl.style.display = "block";

      document.getElementById("customDialogInputWrap").style.display = "none";

      const cancelBtn = document.getElementById("customDialogCancelBtn");
      cancelBtn.style.display = "none";

      const confirmBtn = document.getElementById("customDialogConfirmBtn");
      confirmBtn.innerText = confirmText;
      confirmBtn.className = type === "error" ? "btn btn-danger" : "btn btn-primary";

      container.classList.add("active");
      positionDialog(anchor);

      setTimeout(() => {
        confirmBtn.focus();
      }, 50);
    });
  }

  function closeCustomDialog() {
    const container = document.getElementById("customDialogContainer");
    if (container) {
      container.classList.remove("active");
      container._currentAnchor = null;
    }
  }

  // 暴露公開 API
  window.showCustomPrompt = showCustomPrompt;
  window.showCustomConfirm = showCustomConfirm;
  window.showCustomAlert = showCustomAlert;
  window.closeCustomDialog = closeCustomDialog;

  // 全域攔截原生瀏覽器 Message Box，禁止跳出原生視窗
  window.alert = function (msg) {
    const activeEl = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    return showCustomAlert({ message: String(msg), anchor: activeEl });
  };

  window.confirm = function (msg) {
    const activeEl = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    return showCustomConfirm({ message: String(msg), anchor: activeEl });
  };

  window.prompt = function (msg, def) {
    const activeEl = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    return showCustomPrompt({ message: String(msg), defaultValue: def, anchor: activeEl });
  };

})();
