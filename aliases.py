# -*- coding: utf-8 -*-
"""干员/敌方单位代号（别名）记录管理。

Agent 在查询时遇到用户使用非规范名（如社区外号「夏游洁」→「予愿安洁莉」）时，
可通过 main.py 的 arknights_query_alias 工具登记；查询层会自动用别名表解析输入。
别名持久化到 data/arknights_query/aliases.json，支持 register（新增/覆盖）、
update（只改已存在）、remove（删除）、query（列出）。

文件结构为 {operator: {...}, enemy: {...}, _removed: {...}}：`_removed` 记录被显式
删除的默认代号，避免 `_load` 合并默认表时把用户刚删掉的默认代号又写回来。
"""

import json
import logging
import os
import threading

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

logger = logging.getLogger("astrbot")

ALIAS_FILE = os.path.join(get_astrbot_data_path(), "arknights_query", "aliases.json")

VALID_KINDS = ("operator", "enemy")

# 默认代号表（社区公认高频外号）。首次加载或文件缺失时写入；
# 已有文件时也会合并（只补缺，不覆盖用户/Agent 已登记的内容）。
DEFAULT_ALIASES = {
    "operator": {
        "大哥": "重岳",
        "二哥": "望",
        "臭棋篓子": "望",
        "银老板": "银灰",
        "小羊": "艾雅法拉",
        "42姐": "史尔特尔",
        "蒂蒂": "斯卡蒂",
        "拉狗": "拉普兰德",
        "德狗": "德克萨斯",
        "煌猫猫": "煌",
        "洁哥": "安洁莉娜",
        "杰哥": "安洁莉娜",
        "白咕咕": "白面鸮",
        "驴": "阿米娅",
        # 安洁莉娜异格（予愿安洁莉娜）的社区称呼（谐音：予愿/芋圆）
        "芋圆": "予愿安洁莉娜",
        "芋圆安洁莉娜": "予愿安洁莉娜",
        "夏游洁": "予愿安洁莉娜",
    },
    "enemy": {
        "大爹": "爱国者",
    },
}

_lock = threading.Lock()
_cache = None  # {"operator": {alias: name}, "enemy": {alias: name}}
# 被显式删除的默认代号 {"operator": set(), "enemy": set()}，持久化在文件的 _removed 键下
_removed = {kind: set() for kind in VALID_KINDS}

REMOVED_KEY = "_removed"


def _empty():
    return {"operator": {}, "enemy": {}}


def _load() -> dict:
    """加载别名表（带缓存），并在加载时合并默认代号（跳过硬删除的代号）。"""
    global _cache, _removed
    if _cache is not None:
        return _cache
    data = _empty()
    removed = {kind: set() for kind in VALID_KINDS}
    if os.path.exists(ALIAS_FILE):
        try:
            with open(ALIAS_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            for kind in VALID_KINDS:
                table = raw.get(kind) or {}
                if isinstance(table, dict):
                    data[kind] = {str(k): str(v) for k, v in table.items() if str(k)}
            raw_removed = raw.get(REMOVED_KEY) or {}
            if isinstance(raw_removed, dict):
                for kind in VALID_KINDS:
                    aliases = raw_removed.get(kind) or []
                    if isinstance(aliases, list):
                        removed[kind] = {str(a) for a in aliases if str(a)}
        except Exception as e:
            logger.warning(f"别名文件解析失败，将重建: {e}")
    # 合并默认代号（只补缺，不覆盖已有记录，也不复活被显式删除的代号）
    changed = False
    for kind, table in DEFAULT_ALIASES.items():
        for alias, name in table.items():
            if alias not in data[kind] and alias not in removed[kind]:
                data[kind][alias] = name
                changed = True
    _removed = removed
    _cache = data
    if changed:
        _save()
    return data


def _save() -> None:
    os.makedirs(os.path.dirname(ALIAS_FILE), exist_ok=True)
    payload = {**_cache, REMOVED_KEY: {k: sorted(v) for k, v in _removed.items() if v}}
    with open(ALIAS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def resolve(kind: str, text: str):
    """在别名表中查找 text 对应的规范名。

    支持精确命中与「输入包含别名」（取最长别名），未命中返回 None。
    """
    if kind not in VALID_KINDS or not text:
        return None
    table = _load().get(kind) or {}
    if text in table:
        return table[text]
    candidates = []
    for alias, name in table.items():
        if len(alias) > 1 and alias in text:
            candidates.append((len(alias), name))
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]
    return None


def register(kind: str, alias: str, name: str) -> bool:
    """登记/覆盖一条别名记录（代号已存在即覆盖）。"""
    if kind not in VALID_KINDS:
        return False
    alias = (alias or "").strip()
    name = (name or "").strip()
    if not alias or not name:
        return False
    with _lock:
        data = _load()
        data[kind][alias] = name
        _removed[kind].discard(alias)
        _save()
    logger.info(f"已登记代号 {kind}: {alias} -> {name}")
    return True


def update(kind: str, alias: str, name: str) -> bool:
    """修改已登记代号的规范名；代号不存在时返回 False（不新建记录）。"""
    if kind not in VALID_KINDS:
        return False
    alias = (alias or "").strip()
    name = (name or "").strip()
    if not alias or not name:
        return False
    with _lock:
        data = _load()
        if alias not in data[kind]:
            return False
        data[kind][alias] = name
        _save()
    logger.info(f"已修改代号 {kind}: {alias} -> {name}")
    return True


def remove(kind: str, alias: str) -> bool:
    """删除一条别名记录。删除默认代号会记入 _removed，避免下次加载又被合并回来。"""
    if kind not in VALID_KINDS:
        return False
    alias = (alias or "").strip()
    if not alias:
        return False
    with _lock:
        data = _load()
        if alias in data[kind]:
            del data[kind][alias]
            if alias in DEFAULT_ALIASES.get(kind, {}):
                _removed[kind].add(alias)
            _save()
            logger.info(f"已删除代号 {kind}: {alias}")
            return True
    return False


def all_aliases(kind: str = None) -> dict:
    """返回全部别名表；kind 为空时返回 {operator: ..., enemy: ...}。"""
    if kind in VALID_KINDS:
        return dict(_load().get(kind) or {})
    return {k: dict(v) for k, v in _load().items()}
