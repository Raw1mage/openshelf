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

# 關鍵字規則已遷至 app/classification/rules.py。
#
# 舊表與舊 `infer_categories_for_work()` 在此完全移除，而不是保留一份 deprecated
# 副本：它們帶著兩個已知缺陷——(1) 零命中時 fallback 到 cat_800+cat_850，
# (2) 短 ASCII 詞（ai / dc / code）赤 substring 比對。只要還有任何 import 路徑，
# 下一個呼叫端就可能把 fallback 接回來而沒人發現（這正是線上 cat_850 的 13 筆
# 全是作業系統書的成因）。移除後任何從舊路徑 import 都會當場 ImportError，
# 而不是靜默地給錯答案。

# 線上書攤漸進式雲端探測查詢詞
CATEGORY_CLOUD_SEARCH_QUERIES = {
    # 文學與小說
    "cat_800": "fiction novel",
    "cat_880": "fantasy magic",
    "cat_885": "science fiction",
    "cat_890": "mystery detective",
    "cat_850": "chinese literature",
    "cat_860": "world literature",
    "cat_830": "poetry prose",
    # 漫畫與圖像小說
    "cat_090": "comics manga",
    "cat_091": "action comic",
    "cat_092": "graphic novel",
    "cat_093": "manga",
    "cat_094": "picture book children",
    # 資訊科學與科技
    "cat_400": "computer science",
    "cat_471": "programming python algorithm",
    "cat_472": "artificial intelligence machine learning",
    "cat_473": "cybersecurity linux network",
    "cat_480": "hardware electronics",
    # 自然科學與數學
    "cat_300": "science mathematics",
    "cat_310": "calculus linear algebra statistics",
    "cat_320": "physics astronomy quantum",
    "cat_330": "chemistry material science",
    "cat_360": "biology medicine evolution",
    # 社會科學與商業
    "cat_500": "social science economics",
    "cat_540": "economics finance investment",
    "cat_550": "business management marketing",
    "cat_520": "politics law sociology",
    "cat_560": "education learning pedagogy",
    # 人文、哲學與歷史
    "cat_100": "humanities history",
    "cat_110": "philosophy logic ethics",
    "cat_170": "psychology cognitive neuroscience",
    "cat_610": "world history civilization",
    "cat_750": "geography travel exploration",
    # 藝術、設計與生活
    "cat_900": "art design lifestyle",
    "cat_940": "visual art architecture design",
    "cat_910": "music film cinema",
    "cat_990": "culinary cooking lifestyle",
}
