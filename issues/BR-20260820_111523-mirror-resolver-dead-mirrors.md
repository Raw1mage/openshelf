# BR-20260820_111523 — mirror_resolver 硬編鏡像六死四，且四種失敗全部靜默

Status: OPEN
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: crawler-mirror-health
Filed: 2026-08-20 by ses_fe7b5cbadffeSlxj0dv1Z740O4
Related: 本次 torrent phase-2 可行性實測（subagent ses_fe2dd0a83ffeISDdx4HOpQT4eV）順手發現；
         與 commit 6a0f795（torrent Phase 1）同屬 crawler 層但不同關注點，非同族復發。

## 一句話

`app/crawler/mirror_resolver.py:12-19` 的 `BASE_MIRRORS` 六個鏡像**只剩兩個活著**，
而四種死法（NXDOMAIN / TCP 逾時 / 法院查封 / 自簽憑證）在現行程式碼中
**全部收斂成同一個輸出：`return None`**——呼叫端無法區分「這本書沒有」與「整個鏡像已被查封」。

## 實測證據（2026-08-20，正控制組驗證過）

| BASE_MIRRORS 項 | 實測 | 現行程式碼的反應 |
|---|---|---|
| `https://libgen.li` | **200 真書庫** | ✅ 正常 |
| `https://libgen.la` | **200 真書庫** | ✅ 正常 |
| `https://libgen.rocks` | curl rc=60 **自簽憑證**；加 `-k` → 200 但內容是 `<title>Domain Seizure Notice</title>` | ⚠️ 因 `verify=False`（:47）憑證錯誤被吞掉 → 拿到查封頁 → 解析不到連結 → `None` |
| `https://libgen.gs` | **DNS NXDOMAIN**（`getent hosts` rc=2，空輸出） | ⚠️ httpx 拋例外 → `except Exception: pass`（:89）→ `None` |
| `https://libgen.pm` | **DNS NXDOMAIN**（rc=2） | 同上 |
| `http://library.lol` | 200，但 body 1339 bytes 為 `<title>Domain Seizure Notice</title>`，`<a>` 標籤數 **0** | ⚠️ `status_code == 200` 通過（:97）→ `find("div", id="download")` 回 None → `None` |

控制組：`getent hosts libgen.li` rc=0（證明 DNS 探針有效）；查封頁 `<a >` 命中 0
而 `libgen.li` 詳情頁 `<a >` 命中 90（證明 grep 有鑑別力）。

額外：`https://libgen.is` DNS 解析成功（`193.218.118.42`）但 https/http 皆 curl rc=28
逾時，**屬 UNDECIDABLE**（無法區分站點下線與本地網路阻擋），不列為死亡。

## 四個獨立缺陷

### D1 — 硬編清單已死四個（:12-19）
清單是快照，站點是活的。無任何機制偵測條目失效。

### D2 — `verify=False` 全域關閉 TLS 驗證（**四處，非一處**）
這行讓 `libgen.rocks` 的自簽憑證被靜默接受。**中間人攻擊零阻力**，
而這條路徑最終會把使用者導向一個「直鏈下載」——下載的是什麼由中間人決定。
安全問題，與鏡像存活無關，獨立成立。

**實測分布（2026-08-20，`grep -rn "verify=False" app/ --include=*.py`）**：
```
app/crawler/libgen_live.py:219
app/crawler/mirror_resolver.py:47
app/crawler/validator.py:42
app/crawler/download_worker.py:337   ← 這處最嚴重：實際下載書檔的連線
```
建檔時只寫了 `mirror_resolver.py:47`，那是漏量。**四處全部成立，且 `download_worker.py:337`
是真正落檔的那條連線** —— 前三處洩漏的是搜尋 metadata，第四處決定使用者硬碟上出現什麼位元組。

⚠ **不可盲目全刪**：libgen 鏡像確實常有憑證問題（`libgen.rocks` 就是自簽）。
移除前必須先量測「哪些存活鏡像在 `verify=True` 下會壞」，否則會把功能一起關掉。

### D3 — 查封頁與「書不存在」共用同一個輸出
`library.lol` / `libgen.rocks` 現在回 **HTTP 200**，`status_code == 200` 的檢查完全通過，
只是解析不到目標元素。程式碼無法分辨：
- 這本書在這個鏡像上沒有（正常，該換下一個）
- 這個鏡像已經不是書庫了（異常，該永久剔除並告警）

**這正是「缺席態與失敗態共用同一個輸出」。** 沒有任何日誌、沒有任何計數器。

### D4 — 死路徑：`libgen.is` 家族被路由到已查封的 `library.lol`（:54-55）
```python
elif any(k in base for k in ("libgen.is", "libgen.rs", "libgen.st")):
    direct_url = await self._resolve_from_library_lol(client, f"http://library.lol/main/{md5}")
```
`library.lol` 已被查封，這條分支恆回 `None`。且 `libgen.is/rs/st`
根本不在 `BASE_MIRRORS` 裡，此分支只有在 `dao.get_active_libgen_mirror_urls()`
或 `_custom_mirrors` 供應時才會走到——是一條沒人測過的路徑。

## 使用者感受得到的傷害

`resolve_download_url` 依序試 6 個鏡像，`timeout=10.0`（:46）。
最壞情況：2 個 NXDOMAIN 快速失敗 + `libgen.rocks` 拿查封頁 + `library.lol` 拿查封頁
+ 2 個真鏡像各 10s ≈ **每次下載請求多花數十秒在已死站台上**，
且失敗時使用者只看到「找不到下載連結」，看不到「你的鏡像清單過期了」。

## 修復方向（未實作，需獨立工作包）

1. **D1**：`BASE_MIRRORS` 縮到 `["https://libgen.li", "https://libgen.la"]`。
   `libgen.is` 因 UNDECIDABLE 可保留但降到最後順位。
2. **D3（最重要）**：加一個 `_looks_like_libgen(html) -> bool` 哨兵——
   查封頁 `<a>` 數為 0 而真頁 ≥ 79，用結構特徵而非字串比對。
   命中查封特徵時**大聲記錄**並將該鏡像標記為 dead（寫進 dao），不要只回 `None`。
3. **D2**：`verify=False` 移除。若某鏡像真的需要，改成該鏡像單獨 opt-in 並註明理由。
4. **D4**：刪掉 `library.lol` 分支，或改指向仍存活的 `ads.php` 路徑。
5. `except Exception: pass` 至少要 `log.debug` 帶上 exception 型別，
   否則 NXDOMAIN 與 HTML 解析失敗在日誌上長得一模一樣。

## 驗收判準

- [ ] 對 `library.lol` 呼叫 `resolve_download_url` 時，**日誌出現查封告警**（非靜默 None）
- [ ] 負控制組：對 `libgen.li` 的真實 md5 呼叫仍能取得直鏈（證明沒有誤殺）
- [ ] `verify=False` 在 repo 中命中數為 0（或每一處都有具名理由註解）
- [ ] 死鏡像不再消耗 10s timeout

## 沒驗證的

- 未實際跑過 `resolve_download_url`（本 BR 純靜態閱讀 + 站點實測，未進容器）。
- `dao.get_active_libgen_mirror_urls()` 目前回什麼未查——若它已供應動態清單，
  `BASE_MIRRORS` 可能只是 fallback，D1 的實際嚴重度會下降（但 D2/D3/D4 不受影響）。
