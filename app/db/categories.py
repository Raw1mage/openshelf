import re
from typing import List, Dict, Any, Optional

DEFAULT_CATEGORY_TREE = [
    {
        "id": "cat_800",
        "name": "文學與小說",
        "slug": "literature",
        "icon": "📚",
        "level": 1,
        "sort_order": 1,
        "children": [
            {"id": "cat_880", "name": "奇幻與魔法", "slug": "fantasy", "icon": "🧙‍♂️", "level": 2, "sort_order": 1},
            {"id": "cat_885", "name": "科幻與冒險", "slug": "sci-fi", "icon": "🚀", "level": 2, "sort_order": 2},
            {"id": "cat_890", "name": "懸疑與推理", "slug": "mystery", "icon": "🔍", "level": 2, "sort_order": 3},
            {"id": "cat_850", "name": "華文經典小說", "slug": "chinese-fiction", "icon": "🏮", "level": 2, "sort_order": 4},
            {"id": "cat_860", "name": "世界翻譯文學", "slug": "world-literature", "icon": "🌍", "level": 2, "sort_order": 5},
            {"id": "cat_830", "name": "散文與詩歌", "slug": "prose-poetry", "icon": "✒️", "level": 2, "sort_order": 6},
        ]
    },
    {
        "id": "cat_090",
        "name": "漫畫與圖像小說",
        "slug": "comics",
        "icon": "🎨",
        "level": 1,
        "sort_order": 2,
        "children": [
            {"id": "cat_091", "name": "冒險動作漫畫", "slug": "action-comics", "icon": "⚡", "level": 2, "sort_order": 1},
            {"id": "cat_092", "name": "歐美經典圖像小說", "slug": "graphic-novels", "icon": "🎭", "level": 2, "sort_order": 2},
            {"id": "cat_093", "name": "日系漫畫與輕小說", "slug": "manga", "icon": "🌸", "level": 2, "sort_order": 3},
            {"id": "cat_094", "name": "童書與繪本", "slug": "picture-books", "icon": "🧸", "level": 2, "sort_order": 4},
        ]
    },
    {
        "id": "cat_400",
        "name": "資訊科學與科技",
        "slug": "technology",
        "icon": "💻",
        "level": 1,
        "sort_order": 3,
        "children": [
            {"id": "cat_471", "name": "程式設計與軟體開發", "slug": "programming", "icon": "⌨️", "level": 2, "sort_order": 1},
            {"id": "cat_472", "name": "人工智慧與機器學習", "slug": "ai-ml", "icon": "🤖", "level": 2, "sort_order": 2},
            {"id": "cat_473", "name": "網路、雲端與資訊安全", "slug": "security-cloud", "icon": "🛡️", "level": 2, "sort_order": 3},
            {"id": "cat_480", "name": "電子工程與硬體", "slug": "hardware-engineering", "icon": "🔌", "level": 2, "sort_order": 4},
        ]
    },
    {
        "id": "cat_300",
        "name": "自然科學與數學",
        "slug": "science",
        "icon": "🔬",
        "level": 1,
        "sort_order": 4,
        "children": [
            {"id": "cat_310", "name": "高等數學與統計", "slug": "mathematics", "icon": "📐", "level": 2, "sort_order": 1},
            {"id": "cat_320", "name": "物理學與天文宇宙", "slug": "physics-astronomy", "icon": "🌌", "level": 2, "sort_order": 2},
            {"id": "cat_330", "name": "化學與材料科學", "slug": "chemistry-materials", "icon": "⚗️", "level": 2, "sort_order": 3},
            {"id": "cat_360", "name": "生物、演化與醫學", "slug": "biology-medicine", "icon": "🧬", "level": 2, "sort_order": 4},
        ]
    },
    {
        "id": "cat_500",
        "name": "社會科學與商業",
        "slug": "social-business",
        "icon": "📈",
        "level": 1,
        "sort_order": 5,
        "children": [
            {"id": "cat_540", "name": "經濟學與金融投資", "slug": "economics-finance", "icon": "💰", "level": 2, "sort_order": 1},
            {"id": "cat_550", "name": "企業管理與行銷", "slug": "management", "icon": "📊", "level": 2, "sort_order": 2},
            {"id": "cat_520", "name": "政治、法律與社會學", "slug": "politics-law", "icon": "⚖️", "level": 2, "sort_order": 3},
            {"id": "cat_560", "name": "教育與自主學習", "slug": "education", "icon": "🎓", "level": 2, "sort_order": 4},
        ]
    },
    {
        "id": "cat_100",
        "name": "人文、哲學與歷史",
        "slug": "humanities-history",
        "icon": "🏛️",
        "level": 1,
        "sort_order": 6,
        "children": [
            {"id": "cat_110", "name": "哲學思想與邏輯", "slug": "philosophy", "icon": "🤔", "level": 2, "sort_order": 1},
            {"id": "cat_170", "name": "心理學與認知科學", "slug": "psychology", "icon": "🧠", "level": 2, "sort_order": 2},
            {"id": "cat_610", "name": "歷史與文明演進", "slug": "history", "icon": "📜", "level": 2, "sort_order": 3},
            {"id": "cat_750", "name": "地理、探險與旅行", "slug": "geography-travel", "icon": "🧭", "level": 2, "sort_order": 4},
        ]
    },
    {
        "id": "cat_900",
        "name": "藝術、設計與生活",
        "slug": "arts-lifestyle",
        "icon": "🎭",
        "level": 1,
        "sort_order": 7,
        "children": [
            {"id": "cat_940", "name": "視覺藝術與建築設計", "slug": "art-design", "icon": "📐", "level": 2, "sort_order": 1},
            {"id": "cat_910", "name": "音樂、電影與表演藝術", "slug": "music-film", "icon": "🎬", "level": 2, "sort_order": 2},
            {"id": "cat_990", "name": "飲食料理與生活風格", "slug": "culinary-lifestyle", "icon": "☕", "level": 2, "sort_order": 3},
        ]
    }
]

# 關鍵字自動推導分類規則
CATEGORY_KEYWORDS = {
    "cat_880": ["哈利波特", "魔法", "魔戒", "奇幻", "巫師", "納尼亞", "霍格華茲", "精靈", "龍", "魔獸"],
    "cat_885": ["科幻", "三體", "太空", "星際", "時空", "銀河", "機器人", "賽博朋克", "末日"],
    "cat_890": ["福爾摩斯", "推理", "偵探", "謀殺", "懸疑", "東野圭吾", "柯南", "密室"],
    "cat_091": ["丁丁", "歷險記", "漫画", "漫畫", "連環畫", "comic", "manga", "海賊王", "火影"],
    "cat_092": ["埃爾熱", "圖像小說", "marvel", "dc", "蝙蝠俠", "超人"],
    "cat_471": ["python", "javascript", "golang", "rust", "c++", "演算法", "算法", "架構", "編程", "程式", "代碼", "code"],
    "cat_472": ["機器學習", "深度學習", "人工智慧", "ai", "llm", "神經網絡", "pytorch", "tensorflow"],
    "cat_473": ["linux", "docker", "k8s", "網路", "安全", "資安", "web", "資料庫", "sql"],
    "cat_310": ["微積分", "線性代數", "機率", "統計", "數學", "幾何", "離散數學"],
    "cat_320": ["物理", "量子", "相對論", "天文", "宇宙", "熱力學", "力學"],
    "cat_540": ["經濟", "投資", "股票", "金融", "貨幣", "理財", "巴菲特", "資本"],
    "cat_170": ["心理", "認知", "大腦", "行為", "情緒", "潛意識", "佛洛伊德"],
    "cat_610": ["歷史", "帝國", "戰爭", "古代", "羅馬", "三國", "朝代", "史記", "文明"]
}


def infer_categories_for_work(title: str, author: Optional[str] = None) -> List[str]:
    """根據書名與作者自動推導所屬的分類 ID 清單。"""
    text = f"{title} {author or ''}".lower()
    matched_cats = set()

    for cat_id, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                matched_cats.add(cat_id)
                # 自動關聯至父層分類
                parent_prefix = cat_id.split("_")[1][:2]
                if parent_prefix == "88" or parent_prefix == "89" or parent_prefix == "85" or parent_prefix == "86" or parent_prefix == "83":
                    matched_cats.add("cat_800")
                elif parent_prefix == "09":
                    matched_cats.add("cat_090")
                elif parent_prefix == "47" or parent_prefix == "48":
                    matched_cats.add("cat_400")
                elif parent_prefix == "31" or parent_prefix == "32" or parent_prefix == "33" or parent_prefix == "36":
                    matched_cats.add("cat_300")
                elif parent_prefix == "54" or parent_prefix == "55" or parent_prefix == "52" or parent_prefix == "56":
                    matched_cats.add("cat_500")
                elif parent_prefix == "11" or parent_prefix == "17" or parent_prefix == "61" or parent_prefix == "75":
                    matched_cats.add("cat_100")
                elif parent_prefix == "94" or parent_prefix == "91" or parent_prefix == "99":
                    matched_cats.add("cat_900")
                break

    # 預設分類：若無任何匹配，預設歸入文學小說
    if not matched_cats:
        matched_cats.add("cat_800")
        matched_cats.add("cat_850")

    return list(matched_cats)
