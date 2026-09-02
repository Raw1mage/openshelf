# 來源盤點（2026-08-19 實查，非記憶）

由 opencode `[★]main` 派出的調研 subagent 產出。**所有數字為當日實查。**

> **📌 2026-09-02 修訂（`aggregator_multi-source-provider` Phase 5）**
>
> **論文側（§論文側、Q-A、Q-B、現成方案）維持 2026-08-19 原狀，未重驗，本次不背書。**
> 只有 **書籍側** 經 Phase 1-4 實作與實測重寫：見下方〈書籍側〉的推翻對照表。
>
> 判準：本次改動的每一列都對得回一個**可驗證依據**（commit sha、當日 curl 記錄、
> 或 `plans/aggregator_multi-source-provider/tasks.md` 的實測表）。**分不出真假的一律標
> 「待確認」，不用沉默或推測填空。**

---

## ⚠ 三個時效性陷阱（最優先）

| | 事實 | 影響 |
|---|---|---|
| **PMC FTP 五天後關閉** | 官方公告 **2026-08-24 之後** legacy FTP、OA Web Service API、legacy Cloud Service 全部下架 | 照舊教學寫的 PMC 腳本下週全掛，必須改用新版 AWS Cloud Service |
| **OpenAlex 已吃下 Unpaywall** | `help.openalex.org/access/unpaywall` 已是 OpenAlex 產品頁；API 回應的 `evidence`/`updated` 欄位值 literally 變成字串 `"deprecated"` | **架構上不該當成兩個來源** |
| **`is_oa` 包含 bronze** | 實測 `10.1038/nature12373` → `oa_status: bronze`，全文免費但 `license: null` | bronze 是「出版商今天讓你看」，隨時可撤。用 `is_oa` 算覆蓋率會**系統性高估可長期保存的量** |

---

## 論文側（節錄關鍵格）

| 來源 | 規模 | 資料量 | 授權 | 最大陷阱 |
|---|---|---|---|---|
| **OpenAlex** | 322,887,298 works；`is_oa` 120,967,376（37.5%）；snapshot 含 XPAC ~510M | JSONL **~750 GB** 壓縮 | **CC0** | snapshot 510M vs API 322M **不是 bug**，是 XPAC 語料要自己用 `is_xpac` 濾；CC0 給的是**資料**不是全文 |
| **arXiv** | 3,139,414 篇，全文覆蓋 **100%** | **~9.2 TB**（PDF 2.7TB + src 2.9TB），月增 ~100GB | ⚠️ **多數是 arXiv 預設授權：授予 arXiv 散布權，不授權你再散布** | 官方明寫 "unable to grant others the right to distribute"。**自架自用可以，對外服務不行**。S3 是 requester-pays（你付傳輸費） |
| **PMC OA Subset** | 未查到現行總數 | 未查到 | **三組必須分開下載**：Commercial / Non-Commercial / Other | ①OA Subset ≠ PMC 全部 ②**Other 組沒有機讀授權** ③COVID 期間開放的一批**已收回** ④新 baseline 一出舊的全刪 |
| **CORE** | 452M records，**40M+ 全文** | **749 GB 壓縮 / ~2.7 TB 解壓** | 自訂 T&C（非標準 CC，**必須自己讀完**） | 全文是**抽出的純文字**不是原始 PDF，且常是 accepted manuscript 非 VoR |
| **Semantic Scholar** | 200M papers / **s2orc_v2 僅 16M 全文（8%）** | s2orc_v2 ~180GB | **ODC-BY** | 全文是 Grobid 解析結果，**公式表格會失真** |
| **Crossref** | 185,587,637 works | 未查到 | metadata 幾乎無版權；**但摘要可能有** | metadata 由會員自行 deposit，品質參差 |
| **DOAJ** | 未查到 | 未查到 | metadata **CC0 明文 waive** | **data dump 是 case-by-case 人工審核**，要 email 說明用途，不是自助下載 |
| **bioRxiv/medRxiv** | 未查到 | 無 bulk dump，API 每頁 30 筆 | **逐篇不同**，含 `cc_no` | `cc_no` 是真實存在的值（實測抓到 2 筆）。不逐篇讀 license 就打包＝踩雷 |

**額外實測**：OpenAlex 中 arXiv 這個 source 的 97,717 筆**只有 15,360 標記 OA（15.7%）**，
而 arXiv 全部論文都免費可讀 —— **證明聚合層的 `is_oa` 旗標本身就有系統性缺漏。**

---

## 書籍側（2026-09-02 重寫）

本節已依 `aggregator_multi-source-provider` Phase 1-4 的**實作與實測**重寫。
已落地進系統的來源標 ✅，仍未採用的標 —。

| 來源 | 規模（2026-09-02 實查） | 現況與陷阱 |
|---|---|---|
| **Project Gutenberg** ✅ | **catalog CSV 共 79,288 列，其中 `Type=="Text"` 為 78,037 本**（餘為 Sound 1,114 / Dataset 89 / Image 33 / MovingImage 8 / Collection 4 / StillImage 3）。檔案 21,196,613 bytes | 原記「未查到本數」已被推翻。另原記「唯一合法路徑是 `/robot/harvest` 或私有 mirror」**不完整**——`cache/epub/feeds/pg_catalog.csv` 是官方例外之一，**免申請直下、HTTP 200**。授權是 `"Public domain in the USA."`，**非全球公版**，不得與一般公版混同 |
| **OpenStax** ✅ | **129 本**（CMS API `meta.total_count`）。授權逐本不同：CC BY-NC-SA 72 / CC BY 46 / **未宣告（null）11** | 原記「完全未查到（SPA 取不到內容）」已被推翻——有公開 JSON API（`openstax.org/apps/cms/api/v2/pages/?type=books.Book`）。⚠️ **不帶 `fields=` 參數時 payload 只有 `id`/`meta`/`title`，拿不到 `license_name`**；400 只在 `fields=` 帶了**不認得的欄位名**時觸發 |
| **Open Library / IA** ✅（僅橋接層） | dump: editions 9.2G / all 12.4G / complete 29.6G，**月更，免申請直下**（此列為 2026-08-19 原數字，本次未重驗） | ⚠️ **dump 是 metadata，不含任何全文**（仍成立）。IA 的書分公開下載 vs **Controlled Digital Lending**，CDL 那批不能聚合。<br>**新**：`search.json` 一次查詢可回 6 方對映（key/ia/ebook\_access/isbn/oclc/lccn/id\_project\_gutenberg），適合當**寫入時一次性橋接**；官方明文禁 bulk harvest 與「hundreds of single-book requests」，未識別約 1 req/s。<br>⚠️ **IA 的 `licenseurl` 欄位不可信**（實測公版書被錯填 GPL），需改讀 `rights` |
| **Standard Ebooks** — | **本數仍未查到**（待確認） | 授權是 CC0，**但 bulk 取得有付費牆**：2026-09-02 實測 OPDS feed `/feeds/opds/all` 回 **HTTP 401**（正向控制組：同站首頁 200，故 401 是授權牆不是站台不通）。**授權與取得管道是兩件事** |
| **HathiTrust** — | 18M volumes，**僅 6.7M 美國公版（37%）** ← 此數字來自 Wikipedia，官方站回 403，**二手來源，本次未重驗** | 「公版」指可全文檢索、美國境內可看，**不等於可批次下載**。且政策明寫**美國境外使用者，1896 年後在美國境外出版者一律限制** —— 台灣端再砍一大塊 |
| **DOAB / OAPEN** — | **108,000+ 同儀審查書籍**（此為 2026-08-19 原數字，本次未重驗） | metadata CC0，**但書本身逐本不同**。Selective Export 只適用 ≤500 筆。全文可得率未量測，故不納入本期 |
| **NAP** ❌ 剔除 | — | `terms-of-use` 明文禁止 automated scraping / TDM / AI training，援引 EU DSM Directive Art. 4(3)；**連 metadata 索引都不行**，非技術可繞過的限制，直接剔除不進候選 |

### ✅ 已被 Phase 1-4 實測推翻的舊結論

> **重要：這張表是重建的，不是拄來的。** `design.md:17,50` / `proposal.md:7,80` 三處都寫「8 項
> 推翻結論」，但**原 plan 從未列出那 8 項的明細**。下表是依 Phase 1-4 實際留下的可驗
> 證依據逐項重建而成，共 **8 項**；它是否與原作者心中那 8 項逐字相同，**無法從現存
> 文件判定 —— 此為待確認項，不以推測填空。**

| # | 舊結論（出處） | 2026-09-02 實測結果 | 依據 |
|---|---|---|---|
| 1 | Gutenberg「**未查到本數**」 | 79,288 列 / 78,037 本 `Type=="Text"` | 本次 CSV 實下 + `csv.DictReader` 逐列計數 |
| 2 | Gutenberg「**唯一合法路徑是 `/robot/harvest` 或私有 mirror**」 | catalog CSV 亦為官方入口，免申請直下 `HTTP/2 200`、`content-length: 21196613` | `tasks.md` 2.1；負向控制：同 host 不存在檔回 404 |
| 3 | OpenStax「**完全未查到（SPA 取不到內容）**」 | 有公開 CMS JSON API，`meta.total_count = 129` | `4d471c8b`；本次 curl 重驗 HTTP 200 / 129 |
| 4 | OpenStax 授權不明（原表格一欄写 `—`） | 逐本 `license_name` **3 種值**：CC BY-NC-SA 72 / CC BY 46 / null 11 | `tasks.md` §Phase 4 逐本授權實測分佈；commit `4d471c8b` |
| 5 | （plan 內部前提）「加 `&fields=` 會 400，需整份 payload 解析」 | **推翻**：400 只在**未知欄位名**時觸發；且不帶 `fields=` 根本拿不到 `license_name` | `tasks.md` §⚠ Phase 4 四組探針表 |
| 6 | Standard Ebooks「**未查到**」（規模欄） | 本數**仍未查到**（待確認）；但付費牆已坐實：OPDS feed 回 401，首頁 200 | 本次 curl（含正向控制組） |
| 7 | 「可以拿某個免費識別符當跨來源主鍵」（隆含於原盤點的聚合假設） | **推翻**：ISBN/OCLC/LCCN 皆回陣列、非一對一；同書在不同來源 ID 不同。改用 `(source, source_native_id)` 複合鍵 | `design.md` DD-1；commit `65e945d8` |
| 8 | 「IA 的 `licenseurl` 可當授權來源」 | **推翻**：實測公版書被錯填 GPL，需改讀 `rights` | `design.md` DD-6 |

**附帶推翻（不在上述 8 項內，但同屬書籍側橋接假設）**：Wikidata `P2034`（Gutenberg ID）實測
不存在於已驗證案例（Q174596 Moby Dick），**覆蓋率不可信，不可當主橋接**（`design.md` DD-5）。

---

## Q-A：OA 覆蓋率真實數字（OpenAlex 當日 group_by 實查）

| 年份 | OA 率 |
|---|---|
| 2023 | **62.2%** |
| 2024 | **60.7%** |
| 2020 | 49.0% |
| 2015 | 31.7% |
| 2010 | 22.1% |
| 2005 | **18.0%** |

**近五年約 61% vs 二十年前約 18%，差 3.4 倍。** 比通說的「約 50%」樂觀，但要扣三個折扣：

1. **bronze 混在裡面** —— 無授權的免費閱讀，出版商可隨時關掉，對「不會消失」等於零
2. **2025/2026 兩列不能用** —— 顯示 90% / 68%，是索引時間差 + XPAC 造成的假象
3. **`is_oa` 本身漏標** —— 見上面 arXiv 那格的 15.7%

**未查**：分 `oa_status` 的逐年拆解。沒有它，61% 是虛的。一次 `group_by=open_access.oa_status` 就有。

---

## Q-B：版本歧義 —— 好消息，主體工作不用自己做

實測 Unpaywall 對 `10.1038/nature12373` 回傳 `oa_locations` **四元素陣列**：

```
publisher   nature.com/...pdf         publishedVersion   license: null   (bronze)
repository  arxiv.org/pdf/1304.1068   submittedVersion   license: null
repository  ncbi.../pmc/...           submittedVersion   license: null
repository  dash.harvard.edu/...      submittedVersion   license: cc-by
```

**業界做法不是替每個版本開一個 record**，而是以 DOI 為主鍵、把所有版本收成
locations 陣列並標註 `version` / `host_type`，服務端選一個 best。
OpenAlex Work 實體沿用同一結構 —— **你直接繼承，不必自己做 DOI→多 location 的歸併。**

**真正的難題是無 DOI 的物件**（arXiv 早期、機構庫獨有）。這格完全未觸及。

現成資源：**CORE Deduplication Dataset 2020**（LSH + word embeddings，62MB，ODC-BY，配 LREC 2020 論文）。查到存在，未讀方法細節。

---

## Q-C：現代書籍缺口 —— 幾乎補不起來

- **HathiTrust 18M volumes，僅 37% 公版** —— 全世界最大的學術書籍數位化工程，仍有 **63% 因版權鎖住**
- **DOAB 108,000 本** —— 這是全球所有出版社所有學科的 OA 專書**總和**

**教科書 / 技術書覆蓋率趨近於零。** 與 libgen 存在的理由完全重合，沒有技術解。

部分填補方向 —— **2026-09-02 更新**：

| 方向 | 2026-08-19 原判 | 現況 |
|---|---|---|
| **OpenStax** | 未取得 | **✅ 已接入**（129 本，逐本授權，commit `4d471c8b`）。填補量體很小但**是真的拿得到**，且授權逐本明確 |
| **NAP** | 未查 | **❌ 剔除**：`terms-of-use` 明文禁 TDM，援引 EU DSM Art. 4(3)，非技術可繞（`design.md` DD-7） |
| 圖書館 API | 未驗證 | 未變：**本質是借閱代理不是聚合**，與「不會消失」矛盾 |
| 機構訂閱代理 | 未驗證 | 未變：**聚合給第三方即違約**，不是合法路 |

**「教科書覆蓋率趨近於零」這個結論本身未被推翻** —— OpenStax 的 129 本相對於
HathiTrust 被鎖住的 11M+ volumes，量級上仍是趨近於零。改變的是「拿不拿得到」，不是「夠不夠」。

**產品上要把「找得到 + 導流」與「拿得到 + 不會消失」分開呈現**，不要讓使用者以為都能拿到檔案。

---

## 現成方案

**本次實查**：
- **Open Access Button** GitHub 組織已是空殼（0 repos），遷至 `github.com/oaworks`。**指向 OAButton 的教學全失效。**
- **CORE FastSync** —— 官方增量同步服務，**比自己輪詢 API 好**。未查定價/限制。
- **OpenAlex 官方已提供 CLI**（`/access/cli/`），不必自己寫 client。未讀內容。

**未查證**（列出但請自行驗證）：Calibre-Web / Kavita / Komga 是**閱讀/館藏管理層不是聚合層**，
三者定位不同（Komga 偏漫畫、Kavita 混合、Calibre-Web 依賴 Calibre DB），選錯會很痛。
Zotero connector 是**逐篇手動觸發**，當館藏建置層不合理。

**沒有找到任何一個「已經在做整件事」的開源專案。** 既是機會也是警訊 ——
值得先問為什麼沒有（調研者的懷疑：合法版本的價值密度不夠支撐社群，而非技術難）。

---

## 調研者標明「我沒查到的」（按重要性）

1. **PMC 新版 Cloud Service 端點與檔案結構** —— 舊的五天後死，**必須優先補**
2. **OA 逐年 × oa_status 拆解** —— 沒有它 Q-A 的 61% 是虛的
3. ~~Project Gutenberg 本數~~（**2026-09-02 已查得：79,288 列 / 78,037 本 Text**）、Standard Ebooks 本數（**仍未查到**）、DOAJ 筆數（仍未查到）
4. **HathiTrust 全部** —— 官方 403，唯一數字來自 Wikipedia，**書籍側規模最大者必須重驗**
5. Unpaywall 現行條款/dump 大小/更新頻率 —— 官網需 JS，三次渲染不出
6. OpenAlex work 合併演算法官方說明 —— help center 改版舊 URL 全 404
7. **無 DOI 物件的去重** —— Q-B 真正的難題
8. ~~OpenStax / NAP~~ —— **2026-09-02 已結案**：OpenStax 129 本已接入（逐本授權 CC BY-NC-SA 72 / CC BY 46 / 未宣告 11）；NAP 因法務明文禁 TDM 剔除，不再列為待查
9. 各來源實際 rate limit 數字（除 OpenAlex 外都只查到「要不要 key」）
10. **CORE Terms & Conditions 原文** —— 全文量第二大的來源，條款必須自己讀完再決定
