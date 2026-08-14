# -*- coding: utf-8 -*-
"""查询逻辑（移植自 amiyabot-arknights-operator-6_2 / -enemy-3_7 / -material-2_8）。

返回的 dict 直接作为对应 HTML 模板的 init(data) 数据契约使用。
"""

import re

from . import aliases as akq_aliases
from .gamedata import GameData, get_skin_file
from .utils import find_most_similar, integer, remove_punctuation, snake_case_to_pascal_case


# ---------------------------------------------------------------------------
# 干员
# ---------------------------------------------------------------------------
def search_operator(text: str, use_similar: bool = True):
    """干员名匹配：代号记录 → 精确 → 英文名 → 包含 → 相似。返回规范名称或 None。

    use_similar=False 时跳过相似度匹配（相似度对社区外号易误匹配，
    如「夏游洁」会被误配到「烈夏」），由调用方先走自动联网解析代号。
    """
    text = (text or "").strip()
    if not text:
        return None

    # 先查历史代号记录（如 夏游洁 -> 予愿安洁莉）
    resolved = akq_aliases.resolve("operator", text)
    if resolved:
        return resolved

    if text in GameData.operators:
        return text

    # 英文名映射
    for op in GameData.operators.values():
        if op.en_name == text:
            return op.name

    # 包含匹配（干员名出现在输入中）
    candidates = []
    for name in GameData.operators.keys():
        if len(name) > 1 and name in text:
            candidates.append(name)
    if candidates:
        candidates.sort(key=len, reverse=True)
        return candidates[0]

    if use_similar:
        # 相似度匹配
        names = list(GameData.operators.keys())
        res = find_most_similar(remove_punctuation(text), names)
        if res:
            return res
    return None


async def get_operator_detail(operator_name: str):
    """干员详情数据（operatorInfo.html + operatorToken.html）。"""
    operator = GameData.operators.get(operator_name)
    if not operator:
        return None, None

    real_name = operator.origin_name
    detail, trust = operator.detail()
    modules = operator.modules()

    module_attrs = []
    if modules:
        for module in modules:
            module_attr = {}
            if module["detail"]:
                attrs = module["detail"]["phases"][-1]["attributeBlackboard"]
                for attr in attrs:
                    module_attr[snake_case_to_pascal_case(attr["key"])] = integer(attr["value"])
            module_attrs.append({**module, "attrs": module_attr})

    skills, skills_id, skills_cost, skills_desc = operator.skills()
    skins = operator.skins()

    infos = [
        "id", "cv", "type", "tags", "range", "rarity", "number", "name", "en_name",
        "wiki_name", "index_name", "origin_name", "classes", "classes_sub",
        "classes_code", "race", "drawer", "team", "group", "nation", "birthday",
        "profile", "impression", "limit", "unavailable", "potential_item",
        "is_recruit", "is_sp",
    ]

    operator_info = {
        "info": {"real_name": real_name, **{n: getattr(operator, n) for n in infos}},
        "skin": (await get_skin_file(skins[0], encode_url=True)) if skins else "",
        "trust": trust,
        "detail": detail,
        "modules": module_attrs,
        "talents": operator.talents(),
        "potential": operator.potential(),
        "building_skills": operator.building_skills(),
        "skill_list": skills,
        "skills_cost": skills_cost,
        "skills_desc": skills_desc,
    }
    tokens = {"id": operator.id, "name": operator.name, "tokens": operator.tokens()}
    return operator_info, tokens


async def get_operator_cost(operator_name: str):
    """精英化/专精材料数据（operatorCost.html）。"""
    operator = GameData.operators.get(operator_name)
    if not operator:
        return None

    materials = GameData.materials
    evolve_costs = operator.evolve_costs()
    evolve_costs_list = {}
    for item in evolve_costs:
        material = materials[item["use_material_id"]]
        if item["evolve_level"] not in evolve_costs_list:
            evolve_costs_list[item["evolve_level"]] = []
        evolve_costs_list[item["evolve_level"]].append(
            {
                "material_name": material["material_name"],
                "material_icon": material["material_icon"],
                "use_number": item["use_number"],
            }
        )

    skills, skills_id, skills_cost, skills_desc = operator.skills()
    skills_cost_list = {}
    for item in skills_cost:
        material = materials[item["use_material_id"]]
        skill_no = item["skill_no"] or "common"
        if skill_no and skill_no not in skills_cost_list:
            skills_cost_list[skill_no] = {}
        if item["level"] not in skills_cost_list[skill_no]:
            skills_cost_list[skill_no][item["level"]] = []
        skills_cost_list[skill_no][item["level"]].append(
            {
                "material_name": material["material_name"],
                "material_icon": material["material_icon"],
                "use_number": item["use_number"],
            }
        )

    skins = operator.skins()
    skin = ""
    if skins:
        skin = await get_skin_file(skins[1] if len(skins) > 1 else skins[0], encode_url=True) or ""

    return {"skin": skin, "evolve_costs": evolve_costs_list, "skills": skills, "skills_cost": skills_cost_list}


def get_operator_skills(operator_name: str):
    """技能详情数据（skillsDetail.html）。"""
    operator = GameData.operators.get(operator_name)
    if not operator:
        return None
    skills, skills_id, skills_cost, skills_desc = operator.skills()
    return {"skills": skills, "skills_desc": skills_desc}


# ---------------------------------------------------------------------------
# 敌方单位
# ---------------------------------------------------------------------------
ENEMY_KEY_MAP = {
    "attributes.maxHp": "maxHp",
    "attributes.atk": "atk",
    "attributes.def": "def",
    "attributes.magicResistance": "magicResistance",
    "attributes.moveSpeed": "moveSpeed",
    "attributes.baseAttackTime": "baseAttackTime",
    "attributes.hpRecoveryPerSec": "hpRecoveryPerSec",
    "attributes.massLevel": "massLevel",
    "attributes.stunImmune": "stunImmune",
    "attributes.silenceImmune": "silenceImmune",
    "attributes.sleepImmune": "sleepImmune",
    "attributes.frozenImmune": "frozenImmune",
    "attributes.levitateImmune": "levitateImmune",
    "attributes.disarmedCombatImmune": "disarmedCombatImmune",
    "attributes.fearedImmune": "fearedImmune",
    "attributes.palsyImmune": "palsyImmune",
    "attributes.attractImmune": "attractImmune",
    "rangeRadius": "rangeRadius",
    "lifePointReduce": "lifePointReduce",
}


def find_enemies(name: str):
    result = []
    name = name.lower()
    for e_name, item in GameData.enemies.items():
        if name == e_name.lower() or (len(name) > 1 and name in e_name.lower()):
            result.append([e_name, item])
    return result


def get_value(key, source):
    for item in key.split("."):
        if item in source:
            source = source[item]
    return source["m_defined"], integer(source["m_value"])


def get_enemy(name: str, get_links: bool = True):
    """敌方单位数据（enemy.html）。"""
    enemy = GameData.enemies.get(name)
    attrs = {}
    link_items = []
    if not enemy:
        return None

    if enemy["data"]:
        key_map = {k: {"title": v, "value": ""} for k, v in ENEMY_KEY_MAP.items()}
        for item in enemy["data"]:
            attrs[item["level"]] = {}
            detail_data = item["enemyData"]
            for key in ENEMY_KEY_MAP:
                defined, value = get_value(key, detail_data)
                if defined:
                    key_map[key]["value"] = value
                else:
                    value = key_map[key]["value"]
                attrs[item["level"]][key_map[key]["title"]] = value

    if get_links:
        for link_id in enemy["info"]["linkEnemies"]:
            res = get_enemy(link_id, get_links=False)
            if res:
                link_items.append(res)

    return {**enemy, "attrs": attrs, "link_items": link_items}


def search_enemy(text: str, use_similar: bool = True):
    """敌方名匹配：代号记录 → 精确 → 索引号 → 相似度。返回规范名称或 None。

    use_similar=False 时跳过相似度匹配，由调用方先走自动联网解析代号。
    """
    text = (text or "").strip()
    if not text:
        return None

    # 先查历史代号记录
    resolved = akq_aliases.resolve("enemy", text)
    if resolved:
        return resolved

    if text in GameData.enemies:
        return text

    # 敌方单位索引号匹配
    for item in GameData.enemies.values():
        if str(item["info"].get("enemyIndex")) == text:
            return item["info"]["name"]

    res = find_enemies(text)
    if res:
        if len(res) == 1:
            return res[0][0]
        # 多个结果：优先完整名/排序最匹配
        names = [r[0] for r in res]
        best = find_most_similar(text, names)
        return best

    if use_similar:
        return find_most_similar(remove_punctuation(text), list(GameData.enemies.keys()))
    return None


def search_enemy_index_list(text: str):
    """返回匹配的敌方单位列表（用于结果列表页）。"""
    res = find_enemies(text)
    return {item[0]: item[1] for item in res}


# ---------------------------------------------------------------------------
# 材料
# ---------------------------------------------------------------------------
def find_material_children(material_id: str, parent_id: str = ""):
    children = []
    if material_id in GameData.materials_made:
        for item in GameData.materials_made[material_id]:
            children.append(
                {
                    **item,
                    **GameData.materials[item["use_material_id"]],
                    "children": (
                        find_material_children(item["use_material_id"], material_id)
                        if item["use_material_id"] != parent_id
                        else []
                    ),
                }
            )
    return children


def check_material(name: str):
    """材料数据（material.html）。"""
    if name not in GameData.materials_map:
        return None

    material = GameData.materials[GameData.materials_map[name]]
    material_id = material["material_id"]

    result = {
        "name": name,
        "info": material,
        "children": find_material_children(material_id),
        "source": {"main": [], "act": []},
        "value": None,
    }

    value_data = GameData.material_values.get(material_id)
    if value_data:
        result["value"] = {
            "materialName": value_data.get("materialName"),
            "itemValue": round(float(value_data.get("itemValue", 0)), 2),
            "itemValueAp": round(float(value_data.get("itemValueAp", 0)), 2),
            "type": value_data.get("type"),
        }

    if material_id in GameData.materials_source:
        source = GameData.materials_source[material_id]
        for code in source.keys():
            if code not in GameData.stages:
                continue
            stage = GameData.stages[code]
            info = {"code": stage["code"], "name": stage["name"], "rate": source[code]["source_rate"]}
            if "main" in code:
                result["source"]["main"].append(info)
            else:
                result["source"]["act"].append(info)

    return result


def search_material(text: str):
    """材料名匹配：精确 → 包含 → 相似度。返回规范名称或 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if text in GameData.materials_map:
        return text

    candidates = []
    for name in GameData.materials_map.keys():
        if name in text:
            candidates.append(name)
    if candidates:
        candidates.sort(key=len, reverse=True)
        return candidates[0]

    return find_most_similar(remove_punctuation(text), list(GameData.materials_map.keys()))
