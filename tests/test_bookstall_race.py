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
  for (const id of ["shelfBreadcrumbs", "shelfTitle", "shelfDiscoveryBtn", "shelfGrid"]) {
    elements.set(id, { innerText: "", innerHTML: "", textContent: "", title: "", onclick: null });
  }
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
      querySelectorAll() { return []; },
    },
    fetch(url, options = {}) {
      return new Promise(resolve => requests.push({ url, options, resolve }));
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return { context, elements, requests };
}

function response(items) {
  return { ok: true, json: async () => ({ items }) };
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

function snapshot(elements) {
  const title = elements.get("shelfTitle");
  const button = elements.get("shelfDiscoveryBtn");
  const grid = elements.get("shelfGrid");
  return {
    title: title.innerHTML,
    buttonText: button.textContent,
    buttonTitle: button.title,
    grid: grid.innerHTML,
  };
}

async function categorySwitchRace() {
  const { context, elements, requests } = createHarness();
  const first = context.loadShelfWorks("cat_a", "分類 A", "A", "分類 A");
  const second = context.loadShelfWorks("cat_b", "分類 B", "B", "分類 B");
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.signal.aborted, true, "new request must abort the previous fetch");

  requests[1].resolve(response([localItem("B 書籍", "work-b")]));
  await second;
  const current = snapshot(elements);
  assert.match(current.title, /分類 B/);
  assert.match(current.grid, /B 書籍/);

  requests[0].resolve(response([localItem("A 晚回書籍", "work-a")]));
  await first;
  assert.deepEqual(snapshot(elements), current, "late category response changed the current shelf view");
  console.log("ASSERTED category-switch-race");
}


async function localCloudSwitchRace() {
  const { context, elements, requests } = createHarness();
  const local = context.loadShelfWorks("cat_same", "同一分類", "📚", "同一分類", false);
  const cloud = context.loadShelfWorks("cat_same", "同一分類", "📚", "同一分類", true);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.signal.aborted, true, "cloud toggle must abort the local fetch");

  requests[1].resolve(response([remoteItem("雲端新書", "f".repeat(32))]));
  await cloud;
  const current = snapshot(elements);
  assert.match(current.title, /雲端推薦/);
  assert.equal(current.buttonText, "📚");
  assert.match(current.grid, /雲端新書/);

  requests[0].resolve(response([localItem("本地晚回書籍", "work-local")]));
  await local;
  assert.deepEqual(snapshot(elements), current, "late local response changed the cloud shelf view");
  console.log("ASSERTED local-cloud-switch-race");
}


Promise.all([categorySwitchRace(), localCloudSwitchRace()]).catch(error => {
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
    assert "ASSERTED local-cloud-switch-race" in result.stdout
