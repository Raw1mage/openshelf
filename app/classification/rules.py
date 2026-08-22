"""規則分類器（零成本第一層）。

與舊 `infer_categories_for_work` 的三個關鍵差異：

1. **移除零命中 fallback**。舊版在完全無命中時硬塞 `cat_800 + cat_850`，
   把「判不出」偽裝成「華文經典小說」——線上 `cat_850` 的 13 筆全是作業系統
   與電腦架構書就是這條路徑造成的。新版零命中就是零命中。

2. **ASCII 關鍵字改用詞元邊界比對**。舊版對 `ai`、`dc`、`code` 這類短詞用裸
   substring，於是 "Gaines" 命中 `ai`、"Introduction" 命中 `dc`（-duc-）。
   CJK 無詞元邊界概念，仍用 substring（中文本來就無空白分詞）。

3. **只回葉節點**。父節點關聯由 DAO 寫入時依 taxonomy 推導，不由關鍵字表
   維護——舊版用 `cat_id.split("_")[1][:2]` 的前綴 if-else 鏈手工映射父層，
   那是 taxonomy 的第二份副本，必然漂移。
"""

import re
from typing import Dict, List, Optional, Set, Tuple

from app.classification.result import (
    ClassificationOutcome,
    ClassificationSource,
    ClassificationState,
)
from app.classification.taxonomy import leaf_category_ids

# 葉節點關鍵字表。只列葉節點；父層由 taxonomy 推導。
#
# 短 ASCII 詞（ai / dc / k8s / c++）刻意保留，但比對走詞元邊界，不是 substring。
# 移除的項目與原因：
#   - cat_471 的 "架構"：與 "電腦架構"（作業系統/硬體）大量重疊，且 "架構" 在
#     中文書名裡是泛用詞（"企業架構"、"知識架構"），精確度不足以做直出。
#   - cat_473 的 "網路"：與 cat_610 的 "網路時代"、社會學書名衝突；保留
#     "網路安全" 等複合詞。
#   - cat_885 的 "龍"（原在 cat_880）：單字 "龍" 在華文書名中極常見（成龍、
#     龍應台、九龍），是誤命中主要來源。
#   - cat_890 的 "密室"：實測誤命中《哈利波特：消失的密室》（Chamber of
#     Secrets 不是密室推理）。改用複合詞 "密室殺人" / "密室推理"。
#   - cat_091 的 "歷險記"：《格列佛歷險記》《浯樹歷險記》是文學不是漫畫；
#     丁丁改由作者名 "埃爾熱"（cat_092）認定，那才是高信心訊號。
CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "cat_880": ["哈利波特", "魔法", "魔戒", "奇幻", "巫師", "納尼亞", "霍格華茲", "精靈", "魔獸",
                "fantasy", "wizard", "sorcerer"],
    "cat_885": ["科幻", "三體", "太空", "星際", "銀河", "賽博朋克", "cyberpunk",
                "science fiction", "sci-fi"],
    "cat_890": ["福爾摩斯", "推理", "偵探", "謀殺", "懸疑", "東野圭吾",
                "密室殺人", "密室推理",
                "sherlock", "detective", "whodunit"],
    "cat_850": ["紅樓夢", "水滸傳", "西遊記", "金瓶梅", "儒林外史", "聊齋", "三國演義",
                "魯迅", "老舍", "沈從文", "張愛玲", "白先勇", "金庸", "古龍", "華文小說"],
    "cat_091": ["漫画", "漫畫", "連環畫", "comic", "comics", "manga",
                "海賊王", "火影"],
    # "dc" 刻意保留（而非刪除）：它是邊界比對的活體控制組。邊界保護一旦
    # 退化成裸 substring，Handcuffs / Grandchild / Windcheater 會立刻誤命中，
    # 測試就會紅。把它刪掉反而讓那條負向測試變成空洞的——邊界壞了也仍然綠。
    "cat_092": ["埃爾熱", "圖像小說", "graphic novel", "marvel", "dc", "batman", "superman",
                "蝙蝠俠", "超人"],
    "cat_471": ["python", "javascript", "typescript", "golang", "rust", "c++", "java",
                "演算法", "算法", "編程", "程式設計", "程式", "代碼", "refactoring",
                "clean code", "design patterns", "compiler", "編譯器"],
    # "ai" 同樣是邊界比對的活體控制組：Gaines / Maine / said / Chair 都含
    # "ai" 子字串，退化成 substring 就全部誤命中。
    "cat_472": ["機器學習", "深度學習", "人工智慧", "神經網絡", "神經網路", "pytorch",
                "tensorflow", "machine learning", "deep learning",
                "artificial intelligence", "neural network", "ai", "llm",
                "reinforcement learning"],
    "cat_473": ["linux", "docker", "kubernetes", "k8s", "資安", "網路安全", "資訊安全",
                "資料庫", "sql", "cybersecurity", "cryptography", "firewall",
                "operating system", "operating systems", "作業系統",
                "computer network", "computer networks", "distributed systems"],
    "cat_480": ["computer architecture", "電腦架構", "計算機組成", "計算機結構",
                "digital design", "verilog", "vhdl", "fpga", "embedded systems",
                "微處理器", "microprocessor", "電子電路"],
    "cat_310": ["微積分", "線性代數", "機率", "統計", "數學", "幾何", "離散數學",
                "calculus", "linear algebra", "probability", "statistics",
                "discrete mathematics"],
    "cat_320": ["物理", "量子", "相對論", "天文", "宇宙", "熱力學", "力學",
                "physics", "quantum", "relativity", "astronomy", "thermodynamics"],
    "cat_540": ["經濟", "投資", "股票", "金融", "貨幣", "理財", "巴菲特",
                "economics", "investment", "finance", "portfolio"],
    "cat_170": ["心理", "認知", "潛意識", "佛洛伊德",
                "psychology", "cognitive", "freud", "psychoanalysis"],
    "cat_610": ["歷史", "帝國", "戰爭", "古代", "羅馬", "三國志", "朝代", "史記", "文明",
                "history", "empire", "civilization"],
}

# ASCII 詞元邊界比對用：只有「全部由 ASCII 字母/數字/+/-/空白」組成的關鍵字
# 才需要邊界保護。含 CJK 的關鍵字沿用 substring（中文無空白分詞）。
_ASCII_KEYWORD_RE = re.compile(r"^[a-z0-9+\-. ]+$")


def _is_ascii_keyword(kw: str) -> bool:
    return bool(_ASCII_KEYWORD_RE.match(kw))


def _compile_boundary(kw: str) -> re.Pattern:
    """把 ASCII 關鍵字編成詞元邊界 pattern。

    不能直接用 `\\b`：`c++` 的結尾是 `+`（非 word char），`\\b` 在那裡不成立。
    改用「前後不得是 word 字元」的 lookaround，對 `c++`、`k8s`、`sci-fi` 都成立。
    """
    return re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")


# 預編譯，避免每本書重編一次。
_COMPILED: Dict[str, List[Tuple[str, Optional[re.Pattern]]]] = {
    cat_id: [
        (kw, _compile_boundary(kw) if _is_ascii_keyword(kw) else None)
        for kw in keywords
    ]
    for cat_id, keywords in CATEGORY_KEYWORDS.items()
}


def _validate_keyword_table() -> None:
    """關鍵字表只能指向 taxonomy 的葉節點。

    在 import 時就炸，而不是等到某本書恰好命中那個壞掉的條目——後者會讓
    「表寫錯」偽裝成「這本書剛好沒分類」。
    """
    leaves = leaf_category_ids()
    unknown = sorted(set(CATEGORY_KEYWORDS) - leaves)
    if unknown:
        raise ValueError(f"CATEGORY_KEYWORDS 指向非葉節點或不存在的分類: {unknown}")


_validate_keyword_table()


def match_rule_categories(title: str, author: Optional[str] = None) -> Dict[str, List[str]]:
    """回傳 {category_id: [命中的關鍵字, ...]}。零命中時回空 dict。

    回傳命中詞而非只回 ID，是為了讓「為什麼命中」可稽核——測試與 debug 都需要
    分辨「命中 python」與「命中 c++」，而只回 ID 的話兩者無法區分。
    """
    text = f"{title or ''} {author or ''}".lower()
    matched: Dict[str, List[str]] = {}
    for cat_id, entries in _COMPILED.items():
        hits: List[str] = []
        for kw, pattern in entries:
            if pattern is not None:
                if pattern.search(text):
                    hits.append(kw)
            elif kw in text:
                hits.append(kw)
        if hits:
            matched[cat_id] = hits
    return matched


class RuleClassifier:
    """高信心規則層：只有**單一葉節點**命中時才直出。"""

    def classify(self, title: str, author: Optional[str] = None) -> ClassificationOutcome:
        matched = match_rule_categories(title, author)

        if len(matched) == 1:
            cat_id = next(iter(matched))
            return ClassificationOutcome(
                state=ClassificationState.CLASSIFIED,
                category_ids=[cat_id],
                source=ClassificationSource.RULE,
                confidence=1.0,
                reason=f"rule hit: {', '.join(matched[cat_id])}",
            )

        # 零命中與多類衝突都交給模型層。兩者的 reason 不同，讓後續可分辨
        # 「沒有任何線索」與「線索互相矛盾」——它們對模型 prompt 的意義不同。
        if not matched:
            reason = "rule: no keyword hit"
        else:
            reason = "rule: conflicting hits on " + ", ".join(sorted(matched))
        return ClassificationOutcome(
            state=ClassificationState.PENDING,
            reason=reason,
        )


def rule_candidate_ids(title: str, author: Optional[str] = None) -> Set[str]:
    """規則層的候選集合（含衝突時的多個）。供模型 prompt 當提示用。"""
    return set(match_rule_categories(title, author))
