# -*- coding: utf-8 -*-
"""干员/敌方单位代号（别名）记录管理。

存储位置是**插件配置**（AstrBot 面板「插件配置」里可见、可直接网页增删改）：
    akq_alias_operator / akq_alias_enemy —— 每项一条「代号=规范名」。
插件启动时 configure() 绑定 AstrBotConfig；register/update/remove 改完立即写回配置并落盘，
所以 AI 自己修正的映射，用户刷新面板就能看到、也能自己改。

旧版独立文件 data/arknights_query/aliases.json 在首次加载时自动并入配置，随后改名为
.merged 归档——只并入一次，避免用户之后在面板里删掉的记录又被合并回来。
"""

import json
import logging
import os
import threading

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

logger = logging.getLogger("astrbot")

LEGACY_FILE = os.path.join(get_astrbot_data_path(), "arknights_query", "aliases.json")

VALID_KINDS = ("operator", "enemy")
CONFIG_KEYS = {"operator": "akq_alias_operator", "enemy": "akq_alias_enemy"}
# 「代号=规范名」的分隔符，兼容手写的全角等号与箭头写法
SEPARATORS = ("=", "＝", "→", "->")

_lock = threading.Lock()
_config = None  # AstrBotConfig（AstrBot 注入的插件配置对象）
_cache = None  # {"operator": {alias: name}, "enemy": {alias: name}}


def configure(config) -> None:
    """绑定插件配置对象并迁移旧文件（插件 __init__ 调用一次）。"""
    global _config, _cache
    _config = config if config is not None else {}
    _cache = None
    _migrate_legacy()


def _split_entry(entry):
    """解析一条「代号=规范名」；空行/注释/不合格式返回 None。"""
    text = str(entry or "").strip()
    if not text or text.startswith("#"):
        return None
    for sep in SEPARATORS:
        if sep in text:
            alias, _, name = text.partition(sep)
            alias, name = alias.strip(), name.strip()
            return (alias, name) if alias and name else None
    return None


def _parse(entries) -> dict:
    table = {}
    if isinstance(entries, list):
        for entry in entries:
            text = str(entry or "").strip()
            if not text or text.startswith("#"):
                continue
            pair = _split_entry(text)
            if pair:
                table[pair[0]] = pair[1]
            else:
                logger.warning(f"代号表里有无法解析的条目（应为「代号=规范名」）: {entry}")
    elif isinstance(entries, dict):
        # 兼容用户在配置文件里直接写成对象：{"代号": "规范名"}
        for alias, name in entries.items():
            alias, name = str(alias).strip(), str(name).strip()
            if alias and name:
                table[alias] = name
    return table


def _load() -> dict:
    global _cache
    if _cache is None:
        cfg = _config or {}
        _cache = {kind: _parse(cfg.get(key)) for kind, key in CONFIG_KEYS.items()}
    return _cache


def _persist() -> None:
    """把内存表写回插件配置并落盘（面板刷新即可看到 AI 的改动）。"""
    save = getattr(_config, "save_config", None)
    if not callable(save):
        logger.warning("代号表未能持久化：未绑定插件配置对象，重启后会丢失")
        return
    try:
        for kind, key in CONFIG_KEYS.items():
            _config[key] = [f"{a}={n}" for a, n in _load()[kind].items()]
        save()
    except Exception as e:
        logger.warning(f"代号表写入插件配置失败: {e}")


def _migrate_legacy() -> None:
    """把旧版 aliases.json 的记录并入插件配置（只做一次，随后改名归档）。"""
    if not _config or not os.path.exists(LEGACY_FILE):
        return
    try:
        with open(LEGACY_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning(f"旧代号文件解析失败，跳过迁移: {e}")
        return
    added = 0
    for kind in VALID_KINDS:
        legacy = raw.get(kind) or {}
        if not isinstance(legacy, dict):
            continue
        for alias, name in legacy.items():
            alias, name = str(alias).strip(), str(name).strip()
            if alias and name and alias not in _load()[kind]:
                _load()[kind][alias] = name
                added += 1
    if added:
        _persist()
    archived = LEGACY_FILE + ".merged"
    try:
        os.replace(LEGACY_FILE, archived)
    except OSError as e:
        logger.warning(f"旧代号文件改名失败: {e}")
        return
    logger.info(f"旧代号文件已并入插件配置（新增 {added} 条），原文件归档为 {os.path.basename(archived)}")


def resolve(kind: str, text: str):
    """在代号表中查找 text 对应的规范名。

    支持精确命中与「输入包含代号」（取最长代号），未命中返回 None。
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
    """登记/覆盖一条代号记录（代号已存在即覆盖），并写回插件配置。"""
    if kind not in VALID_KINDS:
        return False
    alias = (alias or "").strip()
    name = (name or "").strip()
    if not alias or not name:
        return False
    with _lock:
        data = _load()
        if alias in data[kind]:
            logger.info(f"代号已存在，覆盖更新 {kind}: {alias} -> {name}")
        data[kind][alias] = name
        _persist()
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
        _persist()
    logger.info(f"已修改代号 {kind}: {alias} -> {name}")
    return True


def remove(kind: str, alias: str) -> bool:
    """删除一条代号记录，并写回插件配置（不会被默认表重新写回）。"""
    if kind not in VALID_KINDS:
        return False
    alias = (alias or "").strip()
    if not alias:
        return False
    with _lock:
        data = _load()
        if alias not in data[kind]:
            return False
        del data[kind][alias]
        _persist()
    logger.info(f"已删除代号 {kind}: {alias}")
    return True


def all_aliases(kind: str = None) -> dict:
    """返回全部代号表；kind 为空时返回 {operator: ..., enemy: ...}。"""
    if kind in VALID_KINDS:
        return dict(_load().get(kind) or {})
    return {k: dict(v) for k, v in _load().items()}
