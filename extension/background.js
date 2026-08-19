// CMS圖書館 Chrome Extension - Background Service Worker

const ROOT_FOLDER_NAME = "CMS圖書館";

/**
 * 確保 Chrome 書籤列中存在「CMS圖書館」根資料夾。
 */
async function ensureRootFolder() {
  const existing = await chrome.bookmarks.search({ title: ROOT_FOLDER_NAME });
  for (const node of existing) {
    if (!node.url) { // 是資料夾
      return node;
    }
  }

  // 找不到時，預設建立於書籤列 (Bookmarks Bar: id "1" 或第一個節點)
  const tree = await chrome.bookmarks.getTree();
  let parentId = "1";
  if (tree[0] && tree[0].children && tree[0].children[0]) {
    parentId = tree[0].children[0].id;
  }

  const root = await chrome.bookmarks.create({
    parentId: parentId,
    title: ROOT_FOLDER_NAME
  });

  // 預設建立「⭐ 我的最愛」與「待讀清單」子資料夾
  await chrome.bookmarks.create({ parentId: root.id, title: "⭐ 我的最愛" });
  await chrome.bookmarks.create({ parentId: root.id, title: "📖 待讀清單" });

  return root;
}

/**
 * 遞迴獲取 CMS圖書館 完整樹狀結構。
 */
async function getLibraryTree() {
  const root = await ensureRootFolder();
  const subTree = await chrome.bookmarks.getSubTree(root.id);
  return subTree[0];
}

/**
 * 在指定資料夾下建立書籍書籤。
 */
async function addBookmark({ title, url, folderId, folderName }) {
  let targetParentId = folderId;

  if (!targetParentId && folderName) {
    const root = await ensureRootFolder();
    const children = await chrome.bookmarks.getChildren(root.id);
    const foundFolder = children.find(c => !c.url && c.title === folderName);
    if (foundFolder) {
      targetParentId = foundFolder.id;
    } else {
      const newFolder = await chrome.bookmarks.create({ parentId: root.id, title: folderName });
      targetParentId = newFolder.id;
    }
  }

  if (!targetParentId) {
    const root = await ensureRootFolder();
    targetParentId = root.id;
  }

  // 檢查是否已有相同 URL 的書籤，避免重複
  const existing = await chrome.bookmarks.search({ url });
  const inLibrary = existing.find(b => b.parentId === targetParentId);
  if (inLibrary) {
    return inLibrary;
  }

  return await chrome.bookmarks.create({
    parentId: targetParentId,
    title: title,
    url: url
  });
}

/**
 * 依據 URL 刪除書籤。
 */
async function removeBookmark({ url, bookmarkId }) {
  if (bookmarkId) {
    await chrome.bookmarks.remove(bookmarkId);
    return { success: true };
  }
  if (url) {
    const existing = await chrome.bookmarks.search({ url });
    for (const b of existing) {
      await chrome.bookmarks.remove(b.id);
    }
    return { success: true, count: existing.length };
  }
  return { success: false, error: "Missing url or bookmarkId" };
}

/**
 * 建立新分類資料夾。
 */
async function createFolder({ name, parentId }) {
  const root = await ensureRootFolder();
  const targetParent = parentId || root.id;
  return await chrome.bookmarks.create({
    parentId: targetParent,
    title: name
  });
}

/**
 * 監聽來自 Content Script 的通訊指令。
 */
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  const { action, payload } = request;

  (async () => {
    try {
      if (action === "GET_TREE") {
        const tree = await getLibraryTree();
        sendResponse({ success: true, data: tree });
      } else if (action === "ADD_BOOKMARK") {
        const result = await addBookmark(payload);
        sendResponse({ success: true, data: result });
      } else if (action === "REMOVE_BOOKMARK") {
        const result = await removeBookmark(payload);
        sendResponse({ success: true, data: result });
      } else if (action === "CREATE_FOLDER") {
        const result = await createFolder(payload);
        sendResponse({ success: true, data: result });
      } else if (action === "PING") {
        sendResponse({ success: true, pong: true, version: "1.0.0" });
      } else {
        sendResponse({ success: false, error: "Unknown action" });
      }
    } catch (err) {
      console.error("[CMS Ext Background] Error:", err);
      sendResponse({ success: false, error: err.message });
    }
  })();

  return true; // 保持非同步通道開啟
});
