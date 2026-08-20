# BR-20260821_010000 — `resolve_download_url` 的 md5 早退擋掉了一條不需要 md5 的下載路徑

- **Status**: **CLOSED — 路線被使用者否決，非修復**（2026-08-21，`3597e2e`）
- **Closed**: 2026-08-21 by `ses_fe7b5cbadffeSlxj0dv1Z740O4`

## 處置（先讀這節，本 BR 的主張未被採納）

**使用者裁示（原話）：「下載不到的書就不要顯示搜尋結果」。**

本 BR 主張的是相反方向——「md5 空但 `mirror_links` 可用時應該還是能下載」，即拿掉
`mirror_resolver.py:140` 的 `if not md5: return None` 早退，讓 step 2 有機會執行。
**使用者裁定不走這條路。**

實際採行的是 `BR-20260821_030000` 的修法 A：在 **parser 來源端**就丟棄無 md5 的 row
（`libgen_live.py:361` / `:437` 的 `and` 改成單一條件）。既然那些項目不再出現在搜尋
結果裡，`mirror_resolver.py:140` 的早退**從此走不到**——它原地保留當防護，**未動**。

所以本 BR 的技術分析仍然成立（那行確實是我們自己寫的、確實擋掉了一條 helper 簽名裡
沒有 md5 的路徑），但它描述的「機會」被產品決策關閉了。**這是 WONTFIX，不是 FIXED**——
差別在於：若未來要重開那條路徑，本 BR 的分析可直接引用，不必重做。

**Q3 實測結論（探勘 `ses_fdfff5772`，控制組完整）**：兩個活躍鏡像 libgen.li / libgen.la
共 50 row，第 8 欄含 32-hex 的 **50 row**，缺 **0 row**——md5 由同一 row 的 5 條 href
冗餘攜帶。控制組：改抓 `cols[7]`（副檔名欄）含 32-hex = 0；404 body 解析 `table_found=False`；
恆等式 `A(有href) == B(含32hex) + C(缺32hex)` 成立。**本 BR 描述的路徑觸發機率為 0。**

⚠ 但探勘 agent 給的**理由**被 dispatcher 推翻：它說 `libgen.is` 不在 dao 預設清單。
實測 `DEFAULT_LIBGEN_MIRRORS` 裡 `libgen.is` / `.rs` / `.st` 三者 `enabled=True` 且確實
在清單內；真正濾掉它們的是 `dao.py:941-943` 的 `validation_status != "verified"`。
**那是使用者按一次驗證就會翻轉的狀態，不是結構性死路。**
- **Filed by**: `ses_fe7b5cbadffeSlxj0dv1Z740O4`（dispatcher）
- **Trigger**: 使用者質問「你的意思是遠端下載的時候一定要查得到 md5 才准下載？誰規定的？」
- **Family**: `openshelf/crawler-download-path`

## **Related**

- `BR-20260820_235500`（公網書格式標籤失真）—— **同一個 parser、同一個函式段落**（`libgen_live.py` 的
  `_parse_libgen_*_html`），同屬「parser 產出的欄位與它宣稱的語意不符」這一類。
- `BR-20260820_210000`（async 路由族群性同步 I/O）—— **同一條執行路徑**（`download_worker` →
  `mirror_resolver`），但失效類別不同（那張是阻塞，這張是邏輯早退）。
- `plans/torrent-p2p-integration/`（Phase 2）—— 本 BR 的證據 ③ 是它的前置事實：torrent 欄位
  parser 已抓、worker 從未消費。

## 症狀

搜尋結果中某些項目雖然**有可用的鏡像連結**，但只要 parser 沒能從那些連結的 href 裡正則挖出
32 位十六進位字串，該項目就**完全無法下載**——不是「嘗試後失敗」，是**在發出任何網路請求之前就
被自己的程式碼拒絕**。

## 根因

`app/crawler/mirror_resolver.py` src:138-141：

```python
async def resolve_download_url(self, md5: str, candidate_mirrors: Optional[List[str]] = None):
    md5 = md5.strip().lower()
    if not md5:
        return None          # ← 這一行
```

**這個早退發生在 step 2 有機會執行之前**，而 step 2 根本不需要 md5：

```
src:156/:162   step 1 —— 用 md5 自己組 URL
                 f"{base}/main/{md5}"  /  f"{base}/ads.php?md5={md5}"

src:167-181    step 2 —— 直接用 candidate_mirrors（即 job.mirror_links）
                 for mirror_url in candidate_mirrors:
                     _resolve_from_libgen_li(client, mirror_url, base)

src:184        async def _resolve_from_libgen_li(self, client, page_url, base_url)
src:205        async def _resolve_from_library_lol(self, client, page_url, base_url)
               ↑ 兩個 helper 的簽名裡都沒有 md5 參數
```

兩個 helper 做的事是「抓那一頁、把 `get.php?md5=...` 的 href 刮出來」（src:196）——
**md5 是從回應頁面讀到的結果，不是拿去查詢的鑰匙**。step 1 需要 md5 只因為它要自己組 URL；
step 2 手上已經有現成的 URL。

**所以 `if not md5: return None` 不是外部規定，是我們自己加的閘，而且它擋住的是一條
不依賴該條件的路徑。**

## 證據

### ① md5 不是獨立欄位，是從 mirror_links 的 href 正則挖出來的

`app/crawler/libgen_live.py` src:349-358：

```python
mirror_links = []
md5_val = ""
for a in cols[8].find_all("a"):
    href = a.get("href", "")
    if href:
        mirror_links.append(href)
        md5_match = re.search(r"([a-fA-F0-9]{32})", href)
        if md5_match and not md5_val:
            md5_val = md5_match.group(1).lower()
```

**兩者來自同一個來源**（第 8 欄的 `<a href>`）。所謂「缺 md5」的真實意思是
「這些 href 裡沒有 32-hex 字串」，**不是「這本書沒有識別碼」，更不是「這本書無法下載」**。

### ② libgen.li 的連結形態本來就可能不含 md5

Phase 2 探勘實測的 libgen.li 路線是 `index.php?req=md5:<hash>` → `file.php?id=<id>` → `.torrent href`。
**`file.php?id=<id>` 這種 href 裡沒有 32-hex 字串。** 所以「有 mirror_links 卻沒 md5」
不只可能，還可能是某些鏡像的常態。

⚠ **這一格未實測**（見「沒驗證的」）。

### ③ 另有一條完全沒接上的下載通道

```
parser 產出        libgen_live.py src:383-385  torrent_url / magnet_uri / download_protocol
download_worker    grep -n "download_protocol\|torrent_url\|magnet_uri"  → rc=1，0 行
CONTROL 同檔       grep -n "resolve_download_url"                        → rc=0，1 行（src:648）
```

**torrent 欄位在 parser 端已抓好、在 worker 端從來沒有任何消費點。** 那是第二條不需要 md5 的
下載路徑，躺在那裡沒人用。屬 Phase 2 範圍，本 BR 只記載事實不主張修法。

### ④ dispatcher 的錯誤推論（記在自己頭上）

dispatcher 在派探勘後回報使用者「md5 是下載主鍵，缺它 100% 失敗，所以容許缺 md5 等於默默排隊
一個註定失敗的任務」。**那個 100% 是本 repo 自己的 src:140 造成的，不是遠端要求的。**

失效形狀：**把「當前實作的行為」讀成「事物的本質」**。探勘 agent 犯了同一個錯（斷言
「有 mirror_links 卻沒 md5 在 parser 層不會發生」，而那個斷言只是在描述當前 regex 的行為）。
dispatcher 採信了它。

**使用者的一句質問推翻了兩層。**

## 影響

- 有可用鏡像連結、但 href 不含 32-hex 的書籍 **一本都下載不了**，且失敗發生在網路請求之前
- 該情況下 `/api/crawler/download` 回 HTTP 400「必須提供書籍之 MD5 指紋」（`crawler_routes.py` src:115-116），
  訊息把**我們的實作限制**講成**書籍缺少指紋**——使用者無從得知那本書其實下載得到
- `enqueue_batch_download` 的 `if item.md5` 過濾（`crawler_routes.py` src:156）建立在同一個錯誤前提上

## 修復方向（未定，需決策）

- **A. 把早退挪到 step 2 之後** —— `if not md5 and not candidate_mirrors: return None`。
  最小改動，讓有 mirror_links 的項目走 step 2。**風險**：step 1 的迴圈仍會用空 md5 組出
  `{base}/ads.php?md5=` 這種殘缺 URL 打出去，需要一併跳過 step 1。
- **B. 拆成兩個函式** —— `resolve_by_md5()` 與 `resolve_by_mirror_urls()`，呼叫端依手上有什麼決定走哪條。
  語意最清楚，但動到 `download_worker.py` src:648 的呼叫點（目前是別的 handler 的 OWNS 領域）。
- **C. 只修訊息** —— 保留閘門但把 400 的文案改成「本站無法解析此項目的下載連結」。
  不修根因，只讓錯誤歸因不再誤導使用者。

**判準**：修復後必須有一個測試能區分「這本書真的下載不到」與「我們自己拒絕嘗試」。
目前 `resolve_download_url` 回 `None` 同時是兩者的答案——**缺席態與失敗態共用同一個輸出**。

該測試至少要覆蓋：`md5=""` 且 `candidate_mirrors=["…/ads.php?md5=abc…"]` 時，
resolver **必須真的去打那個 URL**（用假 client 斷言請求發生），而不是直接回 None。

## 沒驗證的

1. **沒有抓真實 HTML 確認哪些鏡像的第 8 欄不含 32-hex。** 證據 ② 是依 Phase 2 探勘的
   `file.php?id=` 路線**推論**，未取樣實測。**這格是本 BR 最弱的一環**——若實務上所有活躍鏡像的
   href 都含 md5，那 src:140 雖然邏輯上多餘，但無使用者可感知的傷害，優先序應降低。
2. **沒有實測「空 md5 + 有效 mirror_links」是否真能下載成功。** 需要一個真實的
   `ads.php` URL 做端到端驗證。目前只證明了**程式碼路徑上不需要 md5**，未證明**遠端真的接受**。
3. **沒查 step 1 在空 md5 下的實際行為。** 若拿掉早退，`f"{base}/ads.php?md5="` 會打出去，
   遠端回什麼未知（可能 200 空頁、可能 404、可能被 `_guard_is_library` 判為非書庫而誤標 dead 鏡像）。
   **修法 A 若不一併跳過 step 1，可能誤傷鏡像健康狀態。**
4. **沒評估對 `work_id` 的連帶影響。** `libgen_live.py` src:370/:446 用 `f"libgen_{md5_val}"` 組 work_id，
   空 md5 會讓所有此類項目的 work_id 互撞成 `"libgen_"`。**即使下載能修好，識別碼互撞是另一格**，
   需一併設計（可能該改用 mirror_links 的雜湊或 file.php 的 id）。
5. **沒查第二個 parser（src:437 附近）是否同構。** 只讀了第一個。
