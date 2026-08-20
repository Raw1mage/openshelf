# BR-20260821_020000 — 讀取端 `list_dispatched_issues` 在落點目錄不存在時回 `total=0`，零訊號

Status: **PARTIAL** — 主修復已 landed 並經 dispatcher 獨立驗收（`7c0ff48`），但保留三格未消除的殘留，故不進 `closed/`
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4
Family: openshelf/container-mount-boundary
Severity: 使用者可感知（前端清單恆空，且無任何可據以排查的訊號）
Fixed-by: `7c0ff48`（handler `ses_fe0047b7affeRjQIojOZ6ZY65b`，dispatcher 驗收 2026-08-21）

## 殘留（PARTIAL 的理由，三格皆未消除）

1. **未真的移除 `./issues` 掛載再打端點**。移除掛載必須重啟容器，而 handler 無此授權。
   所以「掛載真的失效時端點回 `source_available:false`」目前是**測試層證據 + 靜態推論**，
   不是生產環境實測。原 BR「沒驗證的」第 1 格**未被消除**，只從「完全沒有證據」推進到
   「有測試層證據」。
2. **前端未在真實瀏覽器渲染驗證**。mutation 探針是抽出函式本體用 `new Function` 執行，
   非 DOM 環境。證明的是分支邏輯，**沒有**證明 `innerHTML` 實際渲染樣貌，也沒證明
   `loadDispatchedIssuesNotice` 在 `app.js:1667` 那個呼叫點真的被觸發。
3. **未查是否還有第三個模組**也用 `Path(__file__).parent.parent.parent` 解析落點。
   原 BR 提出的這一格，handler 判定超出本包邊界而未查，**仍然開著**。

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

## 沒驗證的

- **沒實際把掛載拿掉打這個端點** —— 上述是靜態解析 + 該檔 logger 計數實測，
  沒有真的移除 `./issues:/app/issues` 重啟容器再打一次。推論鏈完整（早退分支就在那六行內），
  但「端到端真的回 total=0 且無訊號」這格是推論不是實測。
- **沒查前端拿到 `total=0` 後渲染什麼** —— 只確認 src:2034 是唯一 fetch 點，
  沒讀它下游的 DOM 邏輯，所以不知道使用者實際看到的是「（無）」還是空白。
- **沒查是否還有第三個模組也用 `Path(__file__).parent.parent.parent`** —— 只查了
  `validator.py`（已修）與 `settings_routes.py`（本案）兩處。
