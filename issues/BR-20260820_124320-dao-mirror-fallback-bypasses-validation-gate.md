# BR-20260820_124320 — dao 的 mirror fallback 繞過驗證閘：「驗證失敗」與「尚未驗證」共用同一個輸出

Status: FIXED-UNCOMMITTED
Owner: ses_fe7b5cbadffeSlxj0dv1Z740O4（值星官）
Family: crawler-mirror-health
Filed: 2026-08-20 by ses_fe29bb665ffeDEhHsHdW0rFuSi（handler）
**Related**: BR-20260820_111523-mirror-resolver-dead-mirrors — 同一種「缺席態與失敗態共用同一個輸出」失效類別，在 dao 層；本次修復期間由 handler 發現

## 一句話

`app/db/dao.py` 的 `get_active_libgen_mirror_urls()` 在「無任何鏡像通過驗證」時，
回退到「所有 `enabled` 的預設鏡像」且**完全不看 `validation_status`**——
於是「全部驗證失敗」與「全部尚未驗證」收斂成同一個輸出，
而那個輸出是**全部放行**，等於繞過它自己上方剛執行完的驗證閘。

## 修復前的原始碼

```python
mirrors = self.get_libgen_mirrors()
active = []
for m in sorted(mirrors, key=lambda x: x.get("priority", 999)):
    if not m.get("enabled", True):
        continue
    status = m.get("validation_status", "unverified")
    if status != "verified":          # ← 閘在這裡
        continue
    ...
    active.append(url)

if not active:
    # 安全防線：若無任何通過驗證之自訂鏡像，回傳預設啟用鏡像
    active = [m["url"].rstrip("/") for m in DEFAULT_LIBGEN_MIRRORS
              if m.get("enabled", True)]     # ← 閘在這裡被繞過，且靜默
return active
```

## 為何這是缺陷而非「安全防線」

註解自稱「安全防線」，但它防的是「回空清單」，代價是**放行未經驗證的鏡像**。
三個獨立問題：

1. **繞過驗證閘**：上方迴圈剛把 `validation_status != "verified"` 全部濾掉，
   fallback 立刻把「所有 enabled 的預設鏡像」原樣放行，不論它們是
   `offline` / `incompatible_layout` / `unverified`。閘的存在被抵銷。

2. **兩種截然不同的狀態共用同一個輸出**：
   - 「使用者的鏡像全部驗證失敗」（異常，應告警）
   - 「使用者尚未跑過任何驗證」（正常初始態）

   兩者都得到「全部放行」，呼叫端無從分辨，日誌上也看不出差別。

3. **靜默**：沒有任何 log。從外部觀察，「正常從 verified 清單過濾出結果」
   與「驗證全滅、正在放行未驗證鏡像」長得一模一樣。

## 實測影響（2026-08-20）

修復前的 `DEFAULT_LIBGEN_MIRRORS` 有 9 個條目**全部** `enabled=True` +
`validation_status="verified"`，其中四個實測已死
（`libgen.rocks` 查封 / `libgen.gs` NXDOMAIN / `libgen.pm` NXDOMAIN / `library.lol` 查封，
證據見 BR-20260820_111523）。

所以這條 fallback 的實際效果是：**只要使用者的鏡像全部驗證失敗，
系統就會回退到一份含四個死網域的清單，並把它們當成可用鏡像。**

## 與 BR-20260820_111523 的關係

同一種失效類別（缺席態與失敗態共用同一個輸出）在**兩個不同層**各出現一次：

| 層 | 表現 | 兩個被混淆的狀態 |
|---|---|---|
| crawler（原 BR 的 D3） | 查封頁回 HTTP 200，解析不到元素 → `return None` | 「這本書沒有」vs「這個鏡像已非書庫」 |
| dao（本 BR） | 無 verified 資料 → 回退全放行 | 「驗證失敗」vs「尚未驗證」 |

值得留痕的原因：這不是同一個 bug 的兩個症狀，是**同一種思考缺口在不同人寫的不同層各犯一次**。

## 修復（已實作，尚未 commit）

於 BR-20260820_111523 的續作工作包中一併修復，工作樹基線 commit `6a0f795`：

1. **fallback 仍套用驗證閘**：改為只回 `enabled=True` **且** `validation_status == "verified"`
   **且** 非已知死亡的預設鏡像。
2. **fallback 不再靜默**：真的走到這條路徑時 `log.warning` 記錄實際回傳的清單
   （或明記「空，無可用鏡像」）。
3. 附帶：新增 `KNOWN_DEAD_MIRROR_HOSTS` + `is_known_dead_mirror()`，
   在讀取側過濾，使**常數路徑與既存 DB 路徑兩條都生效**
   （只改 `DEFAULT_LIBGEN_MIRRORS` 常數救不了已寫入使用者 DB 的資料列）。

修復後：

```python
if not active:
    active = [
        m["url"].rstrip("/")
        for m in DEFAULT_LIBGEN_MIRRORS
        if m.get("enabled", True)
        and m.get("validation_status") == "verified"
        and not is_known_dead_mirror(m.get("url", ""))
    ]
    log.warning("無任何通過驗證的鏡像，已回退至預設清單中已驗證且非已知死亡的鏡像：%s",
                active or "（空，無可用鏡像）")
```

## 驗收判準

- [x] fallback 路徑不再回傳 `validation_status != "verified"` 的條目
- [x] fallback 路徑不再回傳已知死亡的網域
- [x] 走到 fallback 時留下 `log.warning`
- [x] 兩條路徑（有 verified 資料 / 走 fallback）**都有測試覆蓋**
      — `tests/test_mirror_health.py::test_dao_active_urls_path_a_normal_filtering`
        與 `::test_dao_active_urls_path_b_fallback_does_not_bypass_gate`

## 沒驗證的

- **未驗證真實使用者 DB 的既存資料列**：修復採讀取側過濾（不改寫 DB），
  邏輯上對既存列生效，但未在含真實資料的 DB 上實跑過。
- **未評估「回空清單」的下游行為**：修復後若預設清單也全滅，
  `get_active_libgen_mirror_urls()` 會回空 list。呼叫端
  （`mirror_resolver.active_mirrors`）在 `if verified:` 為假時會退回
  `BASE_MIRRORS`，所以不會炸；但其他呼叫端未逐一追。
- **`get_libgen_mirrors()`（dao.py:772-784）的 `except Exception: pass`** 未處理：
  JSON 損毀與「setting 不存在」同樣回退預設清單，也是同一種失效類別的第三個實例。
  不在本次授權範圍，未修。
