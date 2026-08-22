import shutil
import subprocess
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "app" / "static" / "js" / "app.js"


def test_late_shelf_responses_cannot_overwrite_current_view():
    node = shutil.which("node")
    assert node is not None, "node is required for the executable shelf race test"

    script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");

function createHarness() {
  const elements = new Map();
  for (const id of ["shelfBreadcrumbs", "shelfTitle", "shelfGrid"]) {
    elements.set(id, { innerText: "", innerHTML: "", textContent: "", title: "", onclick: null });
  }
  const badges = new Map([
    ["cat_a", { textContent: "1", title: "共 1 本藏書" }],
    ["cat_b", { textContent: "2", title: "共 2 本藏書" }],
    ["cat_failed", { textContent: "4", title: "共 4 本藏書" }],
    ["cat_paged", { textContent: "0", title: "共 0 本藏書" }],
  ]);
  const requests = [];
  const context = {
    AbortController,
    URLSearchParams,
    console,
    performance,
    setInterval,
    clearInterval,
    setTimeout() { return 0; },
    clearTimeout,
    window: { location: { pathname: "/" }, innerWidth: 1024, addEventListener() {} },
    document: {
      addEventListener() {},
      getElementById(id) { return elements.get(id); },
      querySelector(selector) {
        const match = selector.match(/^#node_(.+) > \.tree-header \.tree-badge$/);
        return match ? badges.get(match[1]) : null;
      },
      querySelectorAll() { return []; },
    },
    fetch(url, options = {}) {
      return new Promise(resolve => requests.push({ url, options, resolve }));
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return { context, elements, badges, requests };
}

function response(items, total, catalogStatus = "fresh", page = 1) {
  return {
    ok: true,
    json: async () => ({
      items,
      total,
      page,
      page_size: 20,
      catalog_status: { status: catalogStatus, accumulated_total: total },
      category: { works_count: total },
    }),
  };
}

function localItem(title, workId) {
  return {
    availability_tier: 0,
    local_work_id: workId,
    work_id: workId,
    title,
    authors_display: "作者",
    format: "epub",
    publication_year: 2026,
  };
}

function remoteItem(title, md5) {
  return {
    availability_tier: 1,
    local_work_id: null,
    work_id: `libgen_${md5}`,
    md5,
    title,
    authors_display: "遠端作者",
    format: "pdf_born_digital",
    publication_year: 2025,
  };
}

function snapshot(elements, badges) {
  const title = elements.get("shelfTitle");
  const grid = elements.get("shelfGrid");
  return {
    title: title.innerHTML,
    grid: grid.innerHTML,
    badges: Array.from(badges.entries()),
  };
}

async function categorySwitchRace() {
  const { context, elements, badges, requests } = createHarness();
  const first = context.loadShelfWorks("cat_a", "分類 A", "A", "分類 A");
  const second = context.loadShelfWorks("cat_b", "分類 B", "B", "分類 B");
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.signal.aborted, true, "new request must abort the previous fetch");

  requests[1].resolve(response([
    localItem("B 本地書籍", "work-b"),
    remoteItem("B 線上書籍", "b".repeat(32)),
  ], 4));
  await second;
  const current = snapshot(elements, badges);
  assert.match(current.title, /分類 B/);
  assert.match(current.grid, /B 本地書籍/);
  assert.match(current.grid, /B 線上書籍/);
  assert.equal(badges.get("cat_b").textContent, 4);
  assert.equal(badges.get("cat_b").title, "持久化可逛書目共 4 本");
  assert.equal(badges.get("cat_a").textContent, "1");

  requests[0].resolve(response([localItem("A 晚回書籍", "work-a")], 9));
  await first;
  assert.deepEqual(snapshot(elements, badges), current, "late category response changed badge or cards");
  console.log("ASSERTED category-switch-race");
}


async function failedCloudBadgeSemantics() {
  const { context, elements, badges, requests } = createHarness();
  const load = context.loadShelfWorks("cat_failed", "失敗分類", "📚", "失敗分類");
  requests[0].resolve(response([localItem("僅本地書籍", "work-local")], 1, "failed"));
  await load;

  assert.match(elements.get("shelfGrid").innerHTML, /僅本地書籍/);
  assert.equal(badges.get("cat_failed").textContent, 1);
  assert.equal(badges.get("cat_failed").title, "持久化可逛書目共 1 本；背景刷新失敗，保留既有資料");
  console.log("ASSERTED failed-cloud-badge-semantics");
}


async function persistentTotalAndPagination() {
  const { context, elements, badges, requests } = createHarness();
  const firstPage = context.loadShelfWorks("cat_paged", "分頁分類", "📚", "分頁分類");
  assert.match(requests[0].url, /page=1&page_size=20/);
  const pageItems = Array.from({ length: 20 }, (_, index) =>
    remoteItem(`第一頁書籍 ${index + 1}`, String(index + 1).padStart(32, "0"))
  );
  requests[0].resolve(response(pageItems, 120, "fresh", 1));
  await firstPage;

  assert.equal(badges.get("cat_paged").textContent, 120, "badge must use persisted total, not page length");
  assert.match(elements.get("shelfGrid").innerHTML, /第 1 \/ 6 頁 · 共 120 本/);
  assert.match(elements.get("shelfGrid").innerHTML, /changeShelfPage\(2\)/);

  const secondPage = context.changeShelfPage(2);
  assert.equal(requests.length, 2);
  assert.match(requests[1].url, /page=2&page_size=20/);
  requests[1].resolve(response([remoteItem("第二頁書籍", "f".repeat(32))], 120, "fresh", 2));
  await secondPage;

  assert.match(elements.get("shelfGrid").innerHTML, /第二頁書籍/);
  assert.match(elements.get("shelfGrid").innerHTML, /第 2 \/ 6 頁 · 共 120 本/);
  assert.equal(badges.get("cat_paged").textContent, 120);
  console.log("ASSERTED persistent-total-and-pagination");
}


Promise.all([categorySwitchRace(), failedCloudBadgeSemantics(), persistentTotalAndPagination()]).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
'''

    result = subprocess.run(
        [node, "-e", script, str(APP_JS)],
        cwd=APP_JS.parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ASSERTED category-switch-race" in result.stdout
    assert "ASSERTED failed-cloud-badge-semantics" in result.stdout
    assert "ASSERTED persistent-total-and-pagination" in result.stdout
