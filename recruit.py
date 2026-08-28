# -*- coding: utf-8 -*-
"""公开招募（公招）计算逻辑（移植自 amiyabot-arknights-recruit-3_1/main.py）。

与 Amiya-Bot 原版差异：
- 不依赖 jieba 分词：标签匹配改为「特殊标签优先 + 普通标签最长优先包含匹配」；
- 纯 Python，无框架依赖；数据源为 gamedata.py 的 GameData.operators
  （OperatorImpl，含 id/name/rarity/tags/is_recruit）。
"""

from itertools import combinations

from .gamedata import GameData

# 公招约定用语 → 标准标签（资深干员=5星池，高级资深干员=6星池）
SPECIAL_TAGS = {
    "高级资深干员": "高级资深干员",
    "高级资深": "高级资深干员",
    "高资": "高级资深干员",
    "资深干员": "资深干员",
    "资深": "资深干员",
}

# 特殊标签按长度降序处理，先消费长的再处理短的，避免子串误配
SPECIAL_WORDS = ["高级资深干员", "高级资深", "高资", "资深干员", "资深"]

# 口语缩写 → 标准标签
ABBREV_TAGS = {"近战": "近战位", "远程": "远程位"}

# 公招全部可能标签（含特殊标签「新手」）；用于视觉识别幻觉检测
ALL_TAGS = [
    "近战位", "远程位", "先锋", "近卫", "重装", "狙击", "医疗", "辅助", "术师", "特种",
    "资深干员", "高级资深干员", "新手", "控场", "爆发", "支援", "支援机械", "削弱",
    "快速复活", "位移", "召唤", "生存", "防护", "群攻", "治疗", "输出",
    "费用回复", "减速", "牵制", "元素",
]

# 公招截图真实的标签槽位通常不超过 6 个；识别结果超过该数量即判定为幻觉（背模板）
MAX_VISION_TAGS = 9


def suspect_vision_hallucination(caption_text: str) -> bool:
    """判断公招截图视觉识别结果是否为「模板全集」幻觉输出。

    视觉模型容易把提示词示例里的标签清单原样背出来（十几个甚至二十几个），
    而截图实际只有几个标签槽位。判定为幻觉时上层应丢弃该结果，
    避免污染 LLM 转述的真实标签。
    """
    if not caption_text:
        return False
    for sep in ("、", "，", ",", " ", "／", "/", "；", ";"):
        if sep in caption_text:
            parts = [p for p in caption_text.split(sep) if p.strip()]
            return len(parts) > MAX_VISION_TAGS
    count = sum(1 for tag in ALL_TAGS if tag in caption_text)
    return count > MAX_VISION_TAGS


_tags_cache = None
_tags_cache_signature = None


def build_tags_list() -> list:
    """构建候选标签列表：特殊标签 + 所有可公招干员的 tags（去重，带缓存签名校验）。"""
    global _tags_cache, _tags_cache_signature
    signature = len(GameData.operators)
    if _tags_cache is not None and _tags_cache_signature == signature:
        return _tags_cache
    tags = ["资深", "高资", "高级资深"]
    for item in GameData.operators.values():
        if not item.is_recruit:
            continue
        for tag in item.tags:
            if tag not in tags:
                tags.append(tag)
    _tags_cache = tags
    _tags_cache_signature = signature
    return tags


def clear_tags_cache():
    """数据重新初始化后清空缓存（干员列表变化时）。"""
    global _tags_cache, _tags_cache_signature
    _tags_cache = None
    _tags_cache_signature = None


def parse_tags(text: str):
    """从文本中提取公招标签。

    Returns:
        (tags: list[str], max_rarity: int): 匹配到的标签列表与最高稀有度池（5/6）。
    """
    if not text:
        return [], 5

    text = text.replace("公招", "").replace("公开招募", "").strip()
    tags = []
    max_rarity = 5

    # 1) 特殊标签：从长到短匹配并消费（避免"高级资深干员"又误配出"资深干员"）
    for word in SPECIAL_WORDS:
        if word in text:
            tag = SPECIAL_TAGS[word]
            text = text.replace(word, " ")
            if tag == "高级资深干员":
                max_rarity = 6
            if tag not in tags:
                tags.append(tag)

    # 1.5) 口语缩写 → 标准标签（"近战"→"近战位"、"远程"→"远程位"）
    for short, standard in ABBREV_TAGS.items():
        if short in text and standard not in text:
            text = text.replace(short, standard)

    # 2) 普通标签：最长优先包含匹配
    candidates = sorted(build_tags_list(), key=len, reverse=True)
    matched = []
    for tag in candidates:
        if tag in text and tag not in matched:
            matched.append(tag)
    # 过滤被更长标签完全包含的词（如文本含"近战位"时丢弃"近战"）
    filtered = []
    for tag in matched:
        if any(tag in other and tag != other for other in matched):
            continue
        filtered.append(tag)

    for tag in filtered:
        if tag not in tags:
            tags.append(tag)

    return tags, max_rarity


def find_operator_tags_by_tags(tags: list, max_rarity: int) -> list:
    """找出可公招且稀有度<=max_rarity、命中任一 tag 的干员（稀有度降序）。"""
    res = []
    for item in GameData.operators.values():
        if not item.is_recruit or item.rarity > max_rarity:
            continue
        for tag in item.tags:
            if tag in tags:
                res.append(
                    {
                        "operator_id": item.id,
                        "operator_name": item.name,
                        "operator_rarity": item.rarity,
                        "operator_tags": tag,
                    }
                )
    return sorted(res, key=lambda n: -n["operator_rarity"])


def find_combinations(_list: list) -> list:
    """生成标签的全部组合（1-3 个一组，按标签数从多到少），排除「高级资深+资深」同组。"""
    result = []
    for i in range(3):
        for n in combinations(_list, i + 1):
            n = list(n)
            if n and not ("高级资深干员" in n and "资深干员" in n):
                result.append(n)
    result.reverse()
    return result


def _all_match(tags_str: str, comb: list) -> bool:
    """comb 中每个标签都包含在拼接后的 operator_tags 字符串中。"""
    return all(tag in tags_str for tag in comb)


def build_groups(tags: list, max_rarity: int):
    """计算推荐组合列表。

    Returns:
        list[dict]: 组合列表（已排序，含 tags/max_rarity/operators）；无结果时返回 []；
        完全没有命中干员时返回 None。
    """
    result = find_operator_tags_by_tags(tags, max_rarity=max_rarity)
    if not result:
        return None

    operators = {}
    for item in result:
        name = item["operator_name"]
        if name not in operators:
            operators[name] = item
        else:
            operators[name]["operator_tags"] += item["operator_tags"]

    groups = []
    for comb in [tags] if len(tags) == 1 else find_combinations(tags):
        lst = []
        max_r = 0
        for item in operators.values():
            rarity = item["operator_rarity"]
            if not _all_match(item["operator_tags"], comb):
                continue
            if rarity == 6 and "高级资深干员" not in comb:
                continue
            if rarity >= 4 or rarity == 1:
                if rarity > max_r:
                    max_r = rarity
                lst.append(item)
            else:
                # 按稀有度降序遍历，首个低星即可停止
                break
        if lst:
            groups.append({"tags": comb, "max_rarity": max_r, "operators": lst})

    if not groups:
        return []

    return sorted(groups, key=lambda n: (-len(n["tags"]), -n["max_rarity"]))


def summarize(groups: list, tags: list, max_rarity: int) -> str:
    """生成给用户的推荐摘要（作为工具消息链中的文字部分，让"AI 说话"而不只是一张图）。

    保持简洁：只说最优组合与最高稀有度，详细内容看图。
    """
    if not groups:
        return f"根据标签【{'、'.join(tags)}】没找到能锁定稀有干员的组合，建议再补几个标签试试～"

    best = groups[0]
    ops = best["operators"]
    names = "、".join(o["operator_name"] for o in ops[:4])
    if len(ops) > 4:
        names += f" 等 {len(ops)} 位"
    return (
        f"博士，【{'、'.join(tags)}】最优组合是【{'、'.join(best['tags'])}】"
        f"，最高可锁 {'★' * best['max_rarity']}：{names}，详细见下图～"
    )