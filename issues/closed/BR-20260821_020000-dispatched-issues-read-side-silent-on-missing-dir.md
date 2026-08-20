# BR-20260821_020000 — 讀取端 `list_dispatched_issues` 在落點目錄不存在時回 `total=0`，零訊號

Status: **CLOSED** — 主修復已 landed 並經 dispatcher 獨立驗收（`7c0ff48`）；三格殘留已於 2026-08-21 全數銷帳（`4d7ab3d` + dispatcher 直接查證），無殘留
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4
Family: openshelf/container-mount-boundary
Severity: 使用者可感知（前端清單恆空，且無任何可據以排查的訊號）
Fixed-by: `7c0ff48`（handler `ses_fe0047b7affeRjQIojOZ6ZY65b`，dispatcher 驗收 2026-08-21）

## 殘留銷帳（三格，2026-08-21 全數消除）

> 原本三格都是 PARTIAL 的理由。以下每一格的證據 dispatcher 都獨立重做過，
> 非採信 handler 自報。

**① 未真的移除 `./issues` 掛載再打端點 — 已消除（生產環境實測）**

改用**獨立臨時容器**（同 image、不同 port、不同掛載），全程未動線上 `openshelf-app`
（`docker inspect` 全程 `RestartCount=0`、`StartedAt=2026-08-20T15:59:47Z` 未變）。

```
port 18188  無 -v issues            source_available=False  total=0
port 18189  -v <空目錄>:/app/issues  source_available=True   total=0
port 8088   線上（有 BR）            source_available=True   total=6
DISTINCT_SIGNATURES = 3 of 3
```

無掛載容器 body 逐字：
`{"total":0,"issues":[],"source_available":false,"source_path":"/app/issues"}` HTTP=200
（先印 `KEYS=['issues','source_available','source_path','total']` 再取值，避免用錯 key）

控制組：同容器 `/zzz_not_a_route` → HTTP=404，證明 curl 探針有鑑別力。

容器 log 實測（不是推論）：`"BR 清單來源目錄不可用"` 命中 1；
負控制組 `ZZZ_NOT_A_REAL_LOG_LINE` 命中 0 rc=1；正控制組 `"Uvicorn running"` 命中 1 rc=0。

**這格從「測試層證據 + 靜態推論」升級為生產環境實測。**

**② 前端未在真實瀏覽器渲染驗證 — 已消除（`4d7ab3d`）**

`tests/e2e/test_dispatched_issues_notice.py` 8 條，跑在真 chromium
（`OPENSHELF_E2E=1` 下 8 passed rc=0；預設模式 skip）。證明了 `new Function`
探針做不到的三件事：

- `route_hits == 1` — `loadDispatchedIssuesNotice` 真的被觸發。**沒有這條，
  「函式被呼叫但渲染錯」與「函式根本沒被呼叫」共用同一個輸出**（畫面上都是提示列不出現）
- `inner_text()` 取到「…為『未知』而非『無報告』」— `innerHTML` 真的被渲染
- `is_visible()` — `display` 真的讓元素視覺出現/消失

四分支簽章：`unavailable=block/True`、`empty_ok=none/False`、`legacy(無欄位)=none/False`、
`populated=visible/含 "3"`，三元組相異 3/3。

⚠ **範圍界線（誠實標出）**：本批用 `page.route` 注入 payload，測的是「前端拿到某
payload 時渲染什麼」，**不是**「後端真讀不到目錄時渲染什麼」——後半由①的容器實測負責。
兩段接縫由 `test_live_endpoint_shape_matches_injected_payload` 鎖住（打真端點比對 key
集合），否則後端改欄位名後注入式測試會繼續全綠。payload 形狀逐欄照抄自①的實測 body。

**③ 是否還有第三個模組 — 已消除（沒有第三個）**

```
grep -rn 'parent\.parent\.parent' app/ script/    命中 2 處
  app/api/settings_routes.py:119
  app/crawler/validator.py:52
CONTROL  grep -rn 'Path(__file__)' app/           命中 4 處
  （另有 app/main.py:67、app/db/engine.py:79，兩者都只上溯一層，非落點解析）
```

控制組比目標多兩處 ⇒ grep 有鑑別力，那個 2 不是 pattern 寫錯造成的。**沒有第三個模組。**

## 已驗收的部分（dispatcher 獨立重做，非採信 handler 自報）

- 全套件 `246 passed` rc=0；新測試 `9 passed`；控制組（跑不存在的 test）rc=4 有鑑別力
- 後端 mutation 三格，各帶指紋、跑完還原，`sha256` 三次皆對回 `28f555c01a72a08a…`
  - `is_dir()`→`exists()`：1→0 / 0→1，只殺 `test_path_exists_but_is_a_file`
  - `source_available` 缺目錄分支 `False`→`True`：1→0 / 1→2，殺 3 條
  - 刪 `log.error` 整段：1→0，只殺 `test_missing_dir_emits_log_error`
- 前端 mutation `=== false`→`!== true`：D（舊後端 `undefined`）由 `none/false` 變成
  `block/warn:true`；**控制組**（原版同一支探針）D = `none/false/false` ⇒ 兩者可分，
  證明 `=== false` 是必要條件不是風格
- 線上 8088 實測 `HTTP=200`、`keys=['issues','source_available','source_path','total']`；
  控制組 `/zzz_not_a_route` = 404
- `total=7` vs 容器內 8 個 `.md` 已釐清為**非缺陷**：`settings_routes.py:154`
  `startswith("BR-")` 依設計過濾掉 `FR-*`（`HOST_ONLY` 僅該 FR，`API_ONLY`=0）

## 已採納的推翻（handler 推翻派工單兩格，兩格都成立）

- **欄位不叫 `mount_ok`**：該檢查分辨不出成因（未掛載 / 路徑解析改變 / 權限不足 /
  該位置是檔案），用欄位名斷言證明不了的成因，本身就是新的一次「兩態共用一個輸出」。
  改用 `source_available`（只陳述可觀察事實）+ `source_path`（交出判讀原料）。
- **前端做三分支而非一行提示**：正常空清單時不顯示任何東西。把錯誤訊號變成常態雜訊，
  使用者學會忽略之後，真的失效時同樣看不見——那是同一個病的鏡像。

## **Related**

- `closed/BR-20260820_223000-dispatch-br-writes-to-ephemeral-container-dir.md` — **同一個症狀的第二個產地**。
  223000 修的是**寫入端**（`validator.py`：落點缺失時不建目錄、log.error、raise 具名例外）。
  本 BR 是**讀取端**，兩者共用同一句症狀「前端『已派發 BR』清單永遠是空的」，
  但走**互相獨立的觸發路徑**：使用者可能從未觸發過鏡像驗證（寫入端的 log 從無機會發出），
  卻天天在看這個清單。223000 的修復在這條路徑上完全不在場。
  **由 handler L（`ses_fe0378fa9ffeEYUOnCUTRBcB4k`）在交件時推翻 dispatcher 的範圍判定而發現。**
- `BR-20260820_160000-live-sqlite-on-nfs-latency-undecided-and-multihost-risk.md` — **同一種失效類別**：
  **掛載邊界未被程式碼感知**。160000 是 DB 落在 NFS 而程式以為是本地；223000 是 `issues/` 沒掛
  而程式以為它在 repo 裡；本案是**讀取端同樣以為它在 repo 裡，且連「不在」都不說**。
- `BR-20260820_210000-async-routes-sync-io-on-event-loop-family.md` — **同一條執行路徑**：
  F 節改寫過 `list_dispatched_issues` 的讀取方式（`os.scandir` + `readline()`），
  但**沒有觸及本缺陷**（它改的是「怎麼讀」，本缺陷是「讀不到時說什麼」）。

## 症狀

`GET /api/settings/libgen-mirrors/issues` 在 `issues/` 目錄不存在時回：

```json
{"total": 0, "issues": []}
```

HTTP **200**、零 log、零錯誤欄位。與「目錄存在但真的一份 BR 都沒有」**完全無法區分**。

## 證據（dispatcher 獨立實測）

`app/api/settings_routes.py` src:113-118：

```python
@router.get("/libgen-mirrors/issues")
def list_dispatched_issues():
    issues_dir = Path(__file__).parent.parent.parent / "issues"
    if not issues_dir.exists():
        return {"total": 0, "issues": []}
```

該檔**目前沒有任何 logger**：

```
grep -c "log\.\|logging\."  app/api/settings_routes.py  =  0
CONTROL  grep -c "return"   app/api/settings_routes.py  =  8   ← 證明 grep 對本檔有鑑別力
```

所以修法不只是加一行 `log.error`，還要先把 logger 引進來。

前端唯一消費點 `app/static/js/app.js` src:2034：

```js
const res = await fetch(`${BASE_PATH}/api/settings/libgen-mirrors/issues`);
```

## 為什麼 223000 的修復擋不住這條路徑

| | 寫入端（223000，已修） | 讀取端（本案，未修） |
|---|---|---|
| 觸發條件 | 使用者觸發鏡像驗證且驗證失敗 | 使用者打開設定頁看清單 |
| 缺目錄時的行為 | `log.error` + raise 具名例外 + `error_message` 追加 | 回 `total=0`，HTTP 200 |
| 使用者看得到嗎 | 看得到（訊號沿 `error_message` 浮上來） | **看不到** |

**使用者可以永遠不觸發左欄而天天走右欄。**

## 修復方向（未定，需使用者裁決——會動 API 契約）

- **A. 加欄位** —— 回應多帶 `mount_ok: false`（或 `source_available`），並 `log.error`。
  最誠實，但**改變 API 回應形狀**；既有前端忽略未知欄位仍可運作，
  要讓使用者看得到還需動 `app.js`（新增一行提示）。
- **B. 回 5xx / 503** —— 最大聲，但「目錄不存在」在使用者眼中是設定問題不是伺服器故障，
  且會讓設定頁該區塊整塊壞掉而非只顯示一行提示。
- **C. 只加 log** —— 不動契約，最小改動。但**前端仍然只看到空清單**，
  與修好之前的症狀一模一樣——這正是本 BR 要修的病，等於沒修。

**判準**：修復後必須有一個測試能區分「目錄存在但沒有 BR」與「目錄根本不存在」。
目前 `total=0` 同時是兩者的答案。

## 沒驗證的（建檔當下的狀態 — **三條已於 2026-08-21 全部銷帳，見本檔開頭「殘留銷帳」節**）

> ⚠ 以下是**建檔當下**的證據強度，保留供追溯。三條現在都有實測證據了，
> **不要照著這一節重做實驗**。每條後面標了銷帳去處。

- ~~**沒實際把掛載拿掉打這個端點**~~ —— 上述是靜態解析 + 該檔 logger 計數實測，
  沒有真的移除 `./issues:/app/issues` 重啟容器再打一次。
  → **已銷帳**：獨立臨時容器三態實測（18188 無掛載 / 18189 空目錄 / 8088 線上），
  簽章相異 3/3，`source_available=false` 逐字驗證，容器 log 命中 1（含正負控制組）。
- ~~**沒查前端拿到 `total=0` 後渲染什麼**~~ —— 只確認 src:2034 是唯一 fetch 點，
  沒讀它下游的 DOM 邏輯。
  → **已銷帳**：`tests/e2e/test_dispatched_issues_notice.py` 8 條真 chromium
  （`4d7ab3d`），四分支 `inner_text()` / `is_visible()` 實測，三元組相異 3/3。
- ~~**沒查是否還有第三個模組**~~ —— 只查了 `validator.py`（已修）與 `settings_routes.py`（本案）。
  → **已銷帳**：全 repo `parent.parent.parent` 命中恰為這 2 處；控制組 `Path(__file__)`
  命中 4 處（多出的 `main.py:67` / `engine.py:79` 都只上溯一層，非落點解析）⇒ grep 有鑑別力。
  **沒有第三個模組。**
