# BR-20260820_223000 — `dispatch_br` 寫進容器 ephemeral 目錄，自動產生的 BR 全部靜默丟失

Status: OPEN
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4
Family: openshelf/container-mount-boundary
Severity: 使用者可感知（自動診斷產出物遺失 + 前端清單恆空）

## **Related**

- `BR-20260820_210000-async-routes-sync-io-on-event-loop-family.md` — **同一條執行路徑**：
  本缺陷是在驗收 210000 的 D/F 節時，查 `list_dispatched_issues` 回 `total=0` 的原因而發現的。
  D 節把 `dispatch_br` 移進 `asyncio.to_thread`、F 節改寫 `list_dispatched_issues` 的讀取方式，
  兩者都在這條路徑上，但**都不觸及本缺陷**（它們改的是「怎麼寫/怎麼讀」，本缺陷是「寫到哪裡」）。
- `BR-20260820_160000-live-sqlite-on-nfs-latency-undecided-and-multihost-risk.md` — **同一種失效類別**：
  `容器內路徑` 與 `host 路徑` 的掛載邊界認知落差。160000 是 DB 落在 NFS 而程式以為是本地，
  本案是 `issues/` 根本沒掛而程式以為它在 repo 裡。**類別名：掛載邊界未被程式碼感知。**

## 症狀

前端「鏡像驗證 → 已派發 BR」清單**永遠是空的**，即使 host 的 `issues/` 有 3 份 BR。

## 證據（dispatcher 實測）

```
容器內 /proc/mounts 過濾 /app 與 /data：
    /data/parsed  nfs4
    /data/db      ext4
    /app/app      ext4     ← 只有這一個
    /data/raw     nfs4
                           ← 沒有 /app/issues

docker-compose.yml src:13-22 volumes 四條，無 issues 掛載

app/crawler/validator.py src:23
    self.issues_dir = issues_dir or (Path(__file__).parent.parent.parent / "issues")
app/crawler/validator.py src:24
    self.issues_dir.mkdir(parents=True, exist_ok=True)      ← 靜默造出一個 ephemeral 目錄
app/crawler/validator.py src:211
    file_path = self.issues_dir / f"{br_id}.md"

容器內解析：
    RESOLVED = /app/issues
    BEFORE_instantiation exists = False
    AFTER_instantiation  exists = True      ← mkdir 造出來的
    FILES_IN_IT = 0
    CONTROL /app/app 存在 = True            ← 證明探針讀得到掛載進來的目錄

host 端：
    issues/*.md = 3                          ← 兩邊不是同一個目錄

app/api/settings_routes.py src:116
    issues_dir = Path(__file__).parent.parent.parent / "issues"   ← 讀的是同一個 ephemeral 目錄

前端確實在用這支端點：
    app/static/js/app.js:2011
      const res = await fetch(`${BASE_PATH}/api/settings/libgen-mirrors/issues`);
    CONTROL 假 pattern grep rc=1                ← 證明 grep 有鑑別力
```

## 為什麼一直沒被發現

`mkdir(parents=True, exist_ok=True)`（`src:24`）讓「目錄不存在」變成「目錄存在但是空的」。
於是：

```
真的沒有任何 BR 被派發過      →  total=0
BR 全部寫進 ephemeral 目錄     →  total=0        ← 兩者共用同一個輸出
容器 rebuild 把它們清掉了      →  total=0
```

**沒有任何錯誤、沒有任何 log**。端點回 HTTP 200，前端渲染一個空清單，看起來就像「還沒有 BR」。

這正是本 repo 反覆記載的失效形狀：**缺席態與失敗態共用同一個輸出**。

## 影響

1. **鏡像失效時自動產生的診斷 BR 全部丟失** —— `validate_mirror(auto_dispatch_br=True)` 是這個
   系統偵測 libgen 鏡像改版的主要機制，它的產出物寫進一個 rebuild 就沒的地方。
2. **前端那份清單是死的** —— 使用者看到的永遠是空的，卻沒有任何訊號說「這裡本來該有東西」。
3. **`report.br_path` 回傳一個 host 上不存在的路徑** —— API 回應宣稱 BR 在 `/app/issues/...`。

## 修復方向（未定，需決策）

三條路，各有取捨，**不要在沒拍板前動手**：

- **A. 掛進去** —— `docker-compose.yml` 加 `- ./issues:/app/issues`。
  最小改動，但**讓容器對 repo 的 issues 目錄有寫入權**，agent 手寫的 BR 與程式自動產生的
  混在同一個目錄。
- **B. 改落點** —— 讓 `issues_dir` 預設走 `DATA_DIR`（已掛載），例如 `/data/br/`。
  程式產出物與人寫的 BR 分離，但要同步改 `settings_routes.py:116` 的讀取端，
  且既有 `report.br_path` 的語意會變。
- **C. 出聲** —— 保留現狀但在 `src:24` 偵測到「目錄是我剛造出來的」時 `log.warning`。
  不修根因，只讓缺席態與失敗態不再共用輸出。可與 A/B 併行。

**判準（無論走哪條）**：修復後必須有一個測試能區分「真的沒有 BR」與「BR 寫到別的地方去了」。
目前 `total=0` 同時是兩者的答案。

## 沒驗證的

- **沒實際觸發 `auto_dispatch_br=True`** —— 上述是靜態解析 + 容器內路徑解析，
  沒有真的跑一次鏡像驗證讓它寫一份 BR 出來。推論鏈完整（路徑解析 + 掛載表 + mkdir 行為
  三者都實測），但「端到端真的丟失」這格是推論不是實測。
- **沒查 `report.br_path` 的下游消費者** —— 只確認 API 會回傳它，沒查前端拿它做什麼。
- **沒查是否有其他模組也在寫 repo 相對路徑** —— 只查了 `validator.py` 與 `settings_routes.py`
  兩處 `Path(__file__).parent.parent.parent`。
