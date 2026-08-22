"""分類 taxonomy 的唯一真實來源投影。

`DEFAULT_CATEGORY_TREE` 是分類樹的定義處；本模組把它投影成分類器需要的三種
形狀（合法 ID 集合、葉節點集合、給模型看的目錄），**不另外維護一份副本**——
副本會與樹漂移，而漂移的症狀是「模型回了一個看起來合理但不存在的 ID」。
"""

from typing import Dict, List, Set

from app.db.categories import DEFAULT_CATEGORY_TREE


def all_category_ids() -> Set[str]:
    """taxonomy 中所有存在的 category_id（含父節點）。"""
    ids: Set[str] = set()
    for parent in DEFAULT_CATEGORY_TREE:
        ids.add(parent["id"])
        for child in parent.get("children", []):
            ids.add(child["id"])
    return ids


def leaf_category_ids() -> Set[str]:
    """只含葉節點（無 children）的 category_id。

    「父節點」與「葉節點」必須可分辨：模型若回父節點 ID，那是一個**存在但不
    可用**的答案，與「ID 不存在」是不同的失敗原因，驗證層必須分開回報。
    """
    leaves: Set[str] = set()
    for parent in DEFAULT_CATEGORY_TREE:
        children = parent.get("children", [])
        if children:
            for child in children:
                leaves.add(child["id"])
        else:
            # 無子節點的頂層分類本身即為葉節點。
            leaves.add(parent["id"])
    return leaves


def parent_of(category_id: str) -> str:
    """回傳某葉節點的父分類 ID；本身即頂層時回傳自己。"""
    for parent in DEFAULT_CATEGORY_TREE:
        if parent["id"] == category_id:
            return category_id
        for child in parent.get("children", []):
            if child["id"] == category_id:
                return parent["id"]
    raise KeyError(f"未知的 category_id: {category_id}")


def category_name(category_id: str) -> str:
    for parent in DEFAULT_CATEGORY_TREE:
        if parent["id"] == category_id:
            return parent["name"]
        for child in parent.get("children", []):
            if child["id"] == category_id:
                return child["name"]
    raise KeyError(f"未知的 category_id: {category_id}")


def taxonomy_catalog() -> List[Dict[str, str]]:
    """給模型看的葉節點目錄：[{id, name, parent, parent_name}, ...]。

    只列葉節點，因為契約規定模型只能選葉節點；把父節點也列出去等於邀請它回
    一個必然被拒的答案。
    """
    catalog: List[Dict[str, str]] = []
    for parent in DEFAULT_CATEGORY_TREE:
        children = parent.get("children", [])
        if not children:
            catalog.append({
                "id": parent["id"],
                "name": parent["name"],
                "parent": parent["id"],
                "parent_name": parent["name"],
            })
            continue
        for child in children:
            catalog.append({
                "id": child["id"],
                "name": child["name"],
                "parent": parent["id"],
                "parent_name": parent["name"],
            })
    return catalog
