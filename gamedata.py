# -*- coding: utf-8 -*-
"""数据层：gitee 拉取 → 解压 → 解析 JSON → 内存数据（移植自 amiyabot-arknights-gamedata-4_0）。

数据存放布局与 Amiya-Bot 完全一致，保证 HTML 模板内的相对路径（../../../resource/gamedata/...）
无需任何修改即可工作：
    {astrbot_data}/resource/gamedata/            <- git clone 目标（version.txt/gamedata.zip/item/...）
    {astrbot_data}/resource/gamedata/gamedata/   <- gamedata.zip 解压后的 JSON（excel/ levels/）
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile
from collections import Counter
from urllib.parse import quote

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .utils import remove_punctuation, remove_xml_tag, sorted_dict

logger = logging.getLogger("astrbot")

GIT_REPO_DEFAULT = "https://gitee.com/amiya-bot/amiya-bot-assets.git"

# 数据根目录：{astrbot_data}/resource/gamedata
GAMEDATA_DIR = os.path.join(get_astrbot_data_path(), "resource", "gamedata")
# gamedata.zip 解压后的 JSON 根目录
JSON_DIR = os.path.join(GAMEDATA_DIR, "gamedata")
# 材料价值缓存文件（一图流）
MATERIAL_VALUE_CACHE = os.path.join(GAMEDATA_DIR, "material_value.json")

YITULIU_VALUE_API = "https://backend.yituliu.cn/item/v7/value"

# 与 Amiya-Bot builder/common.py 一致
config = {
    "classes": {
        "CASTER": "术师",
        "MEDIC": "医疗",
        "PIONEER": "先锋",
        "SNIPER": "狙击",
        "SPECIAL": "特种",
        "SUPPORT": "辅助",
        "TANK": "重装",
        "WARRIOR": "近卫",
    },
    "token_classes": {"TOKEN": "召唤物", "TRAP": "装置"},
    "high_star": {"5": "资深干员", "6": "高级资深干员"},
    "types": {"ALL": "不限部署位", "MELEE": "近战位", "RANGED": "远程位"},
}

html_symbol = {"<替身>": "&lt;替身&gt;", "<支援装置>": "&lt;支援装置&gt;"}


class JsonData:
    """读取 {JSON_DIR}/{folder}/{name}.json 并缓存。"""

    cache = {}

    @classmethod
    def get_json_data(cls, name: str, folder: str = "excel"):
        if name not in cls.cache:
            path = os.path.join(JSON_DIR, folder, f"{name}.json")
            if os.path.exists(path):
                with open(path, mode="r", encoding="utf-8") as src:
                    cls.cache[name] = json.load(src)
            else:
                return {}
        return cls.cache[name]

    @classmethod
    def clear_cache(cls, name: str = None):
        if name:
            cls.cache.pop(name, None)
        else:
            cls.cache = {}


class SkinIndexes:
    url_indexes: dict = {}


class GameData:
    """内存静态数据（对应 Amiya-Bot 的 ArknightsGameData）。"""

    version: str = ""
    enemies: dict = {}
    stages: dict = {}
    stages_map: dict = {}
    side_story_map: dict = {}
    operators: dict = {}  # name -> OperatorImpl
    tokens: dict = {}
    birthday: dict = {}
    materials: dict = {}
    materials_map: dict = {}
    materials_made: dict = {}
    materials_source: dict = {}
    material_values: dict = {}  # materialId -> value dict（一图流）
    term_descriptions: dict = {}  # key -> {'name', 'description'}（术语/地形说明）

    ready: bool = False


# ---------------------------------------------------------------------------
# 数据获取：git clone / update + 解压
# ---------------------------------------------------------------------------
def _git(args: list, cwd: str = None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", *args]
    logger.info(f"git: {' '.join(cmd)} (cwd={cwd})")
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)


def download_gamedata(repo: str = GIT_REPO_DEFAULT):
    """克隆或更新 gitee 数据仓库到 GAMEDATA_DIR（--depth 1）。

    已有 .git 走增量 pull；已有数据但无 .git 时初始化 git 复用现有文件；
    均失败时保留现有数据（由调用方决定是否继续）。
    """
    os.makedirs(get_astrbot_data_path(), exist_ok=True)
    if os.path.exists(os.path.join(GAMEDATA_DIR, ".git")):
        try:
            _git(["pull", "--ff-only"], cwd=GAMEDATA_DIR)
        except subprocess.CalledProcessError as e:
            logger.warning(f"gamedata git pull 失败，尝试 fetch+reset: {e}")
            _git(["fetch", "origin", "master"], cwd=GAMEDATA_DIR)
            _git(["reset", "--hard", "origin/master"], cwd=GAMEDATA_DIR)
    elif os.path.exists(GAMEDATA_DIR):
        # 已有数据但无 .git：git init 复用现有文件，避免重新下载大仓库
        try:
            _git(["init"], cwd=GAMEDATA_DIR)
            _git(["remote", "add", "origin", repo], cwd=GAMEDATA_DIR)
            _git(["fetch", "--depth", "1", "origin", "master"], cwd=GAMEDATA_DIR)
            _git(["reset", "--hard", "origin/master"], cwd=GAMEDATA_DIR)
        except subprocess.CalledProcessError as e:
            logger.warning(f"gamedata 目录无 .git 且 git 初始化失败，保留现有数据: {e}")
    else:
        _git(["clone", "--depth", "1", repo, GAMEDATA_DIR])
    logger.info(f"gamedata 仓库已就绪: {GAMEDATA_DIR}")


def extract_gamedata_zip():
    """解压 gamedata.zip 到 JSON_DIR（覆盖）。"""
    zip_path = os.path.join(GAMEDATA_DIR, "gamedata.zip")
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"未找到 {zip_path}，请先拉取数据")
    if os.path.exists(JSON_DIR):
        shutil.rmtree(JSON_DIR)
    os.makedirs(JSON_DIR, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(JSON_DIR)
    logger.info(f"gamedata.zip 已解压到 {JSON_DIR}")


# ---------------------------------------------------------------------------
# JSON 解析构建内存数据（移植自 builder/__init__.py）
# ---------------------------------------------------------------------------
def init_operators():
    from .operator_data import Collection, OperatorImpl, TokenImpl

    recruit_detail = remove_xml_tag(JsonData.get_json_data("gacha_table")["recruitDetail"])
    recruit_group = re.findall(r"★\\n(.*)", recruit_detail)
    recruit_operators = []
    for item in recruit_group:
        recruit_operators += [n.strip().strip("\r") for n in item.split("/")]

    operators_list = JsonData.get_json_data("character_table")
    operators_patch_list = JsonData.get_json_data("char_patch_table")["patchChars"]
    voice_data = JsonData.get_json_data("charword_table")
    skins_data = JsonData.get_json_data("skin_table")["charSkins"]

    operators_list.update(operators_patch_list)
    Collection.clear_all()

    for n, item in voice_data["charWords"].items():
        char_id = item["wordKey"]
        if char_id not in Collection.voice_map:
            Collection.voice_map[char_id] = []
        Collection.voice_map[char_id].append(item)

    for n, item in skins_data.items():
        char_id = item["charId"]
        skin_id = item["skinId"]
        if "char_1001_amiya2" in skin_id:
            char_id = "char_1001_amiya2"
        if "char_1037_amiya3" in skin_id:
            char_id = "char_1037_amiya3"
        if char_id not in Collection.skins_map:
            Collection.skins_map[char_id] = []
        Collection.skins_map[char_id].append(item)

    operators = []
    birth = {}
    for code, item in operators_list.items():
        if item["profession"] not in config["classes"]:
            token = TokenImpl(code, item)
            Collection.tokens_map[code] = token
            Collection.tokens_map[token.name] = token
            Collection.tokens_map[token.en_name] = token
            continue
        operator = OperatorImpl(code=code, data=item, is_recruit=item["name"] in recruit_operators)
        operators.append(operator)

    for item in operators:
        for story in item.stories():
            if story["story_title"] == "基础档案":
                r = re.search(r"\n【(生日|出厂日)】.*?(\d+)月(\d+)日\n", story["story_text"])
                if r:
                    month = int(r.group(2))
                    day = int(r.group(3))
                    if month not in birth:
                        birth[month] = {}
                    if day not in birth[month]:
                        birth[month][day] = []
                    item.birthday = f"{month}月{day}日"
                    birth[month][day].append(item)
                r = __import__("re").search(r"性别】(\S+)(\s+)?\n", story["story_text"])
                if r:
                    item.sex = r.group(1)
                break

    birthdays = {}
    for month, days in birth.items():
        birthdays[month] = sorted_dict(days)
    birthdays = sorted_dict(birthdays)

    return {item.name: item for item in operators}, Collection.tokens_map, birthdays


def init_materials():
    building_data = JsonData.get_json_data("building_data")
    item_data = JsonData.get_json_data("item_table")
    formulas = {"WORKSHOP": building_data["workshopFormulas"], "MANUFACTURE": building_data["manufactFormulas"]}

    materials = {}
    materials_map = {}
    materials_made = {}
    materials_source = {}
    for item_id, item in item_data["items"].items():
        if "p_char" in item_id:
            continue
        material_name = item["name"].strip()
        materials[item_id] = {
            "material_id": item_id,
            "material_name": material_name,
            "material_icon": item["iconId"],
            "material_desc": item["usage"],
            "meta_data": item,
        }
        materials_map[material_name] = item_id

        for drop in item["stageDropList"]:
            if item_id not in materials_source:
                materials_source[item_id] = {}
            materials_source[item_id][drop["stageId"]] = {
                "material_id": item_id,
                "source_place": drop["stageId"],
                "source_rate": drop["occPer"],
            }

        for build in item["buildingProductList"]:
            if build["roomType"] in formulas and build["formulaId"] in formulas[build["roomType"]]:
                build_cost = formulas[build["roomType"]][build["formulaId"]]["costs"]
                for build_item in build_cost:
                    if item_id not in materials_made:
                        materials_made[item_id] = []
                    materials_made[item_id].append(
                        {
                            "material_id": item_id,
                            "use_material_id": build_item["id"],
                            "use_number": build_item["count"],
                            "made_type": build["roomType"],
                        }
                    )

    return materials, materials_map, materials_made, materials_source


def init_enemies():
    enemies_info = JsonData.get_json_data("enemy_handbook_table")
    enemies_data = JsonData.get_json_data("enemy_database", folder="levels/enemydata")

    enemies_data_map = {}
    for item in enemies_data["enemies"]:
        if "Key" in item:
            enemies_data_map[item["Key"]] = item["Value"]
        else:
            enemies_data_map[item["key"]] = item["value"]

    data = {}
    for e_id, info in enemies_info["enemyData"].items():
        name = info["name"]
        if name == "-":
            continue
        counter = {}
        for k in data.keys():
            counter[k] = counter.get(k, 0) + 1
        if name in counter:
            name += f"（{counter[name]}）"
        item = {"info": info, "data": enemies_data_map.get(e_id)}
        data[name] = data[info["enemyId"]] = item

    return data


def init_stages():
    activity_table = JsonData.get_json_data("activity_table")["basicInfo"]
    operators_list = JsonData.get_json_data("character_table")
    enemies_info = JsonData.get_json_data("enemy_handbook_table")
    stage_data = JsonData.get_json_data("stage_table")["stages"]
    item_data = JsonData.get_json_data("item_table")["items"]

    def is_ss(key, item):
        if item["isReplicate"]:
            return False
        if item["type"] == "MINISTORY":
            return True
        return item["type"].endswith("SIDE") or ("displayType" in item and item["displayType"] == "SIDESTORY")

    side_story = [item for key, item in activity_table.items() if is_ss(key, item)]
    side_story.sort(key=lambda n: n["startTime"], reverse=True)

    stage_list = {}
    stage_map = {}
    side_story_map = {n["name"]: {} for n in side_story}

    for stage_id, item in stage_data.items():
        if not item["name"]:
            continue
        try:
            level_data = JsonData.get_json_data((item["levelId"] or "no_level").lower(), folder="levels")
        except Exception:
            continue

        level = ""
        if "#f#" in stage_id:
            level = "_hard"
        if "easy" in stage_id:
            level = "_easy"
        if "tough" in stage_id:
            level = "_tough"
        if "#s" in stage_id:
            level = "_sixstar"

        stage_key = item["code"] + level
        stage_key_name = remove_punctuation(item["name"].strip()) + level

        if level_data:
            enemies = {}
            for wave in level_data["waves"]:
                for fragment in wave["fragments"]:
                    for action in fragment["actions"]:
                        if action["actionType"] != "SPAWN":
                            continue
                        if action["key"] not in enemies:
                            if action["key"] in enemies_info["enemyData"]:
                                enemies[action["key"]] = {**enemies_info["enemyData"][action["key"]], "count": 0}
                            elif action["key"][:-2] in enemies_info["enemyData"]:
                                enemies[action["key"]] = {**enemies_info["enemyData"][action["key"][:-2]], "count": 0}
                            else:
                                continue
                        enemies[action["key"]]["count"] += action["count"]
            level_data["enemiesCount"] = enemies

        if item["stageDropInfo"] and item["stageDropInfo"]["displayDetailRewards"]:
            for info in item["stageDropInfo"]["displayDetailRewards"]:
                if info["type"] == "CHAR":
                    if info["id"] in operators_list:
                        info["detail"] = operators_list[info["id"]]
                else:
                    if info["id"] in item_data:
                        info["detail"] = item_data[info["id"]]

        stage_list[stage_id] = {**item, "levelData": level_data, "activity": ""}

        if item["code"].startswith("GT"):
            side_story_map["骑兵与猎人"][stage_id] = stage_list[stage_id]
        elif item["code"].startswith("OF"):
            side_story_map["火蓝之心"][stage_id] = stage_list[stage_id]
        else:
            for ss_item in side_story:
                ss_code = ss_item["id"]
                ss_name = ss_item["name"]
                if ss_code in stage_id:
                    side_story_map[ss_name][stage_id] = stage_list[stage_id]

        for key in [stage_key, stage_key_name]:
            if key not in stage_map:
                stage_map[key] = []
            if stage_id not in stage_map[key]:
                stage_map[key].append(stage_id)

    return stage_list, stage_map, side_story_map


def init_term_descriptions():
    """术语/地形说明：gamedata_const.termDescriptionDict + stage_table.tileInfo（移植自 arknights-term-description）。"""
    data = {}
    const = JsonData.get_json_data("gamedata_const")
    for item in const.get("termDescriptionDict", {}).values():
        data[item["termId"]] = {"name": item["termName"], "description": remove_xml_tag(item["description"])}
    stage_table = JsonData.get_json_data("stage_table")
    for item in stage_table.get("tileInfo", {}).values():
        data[item["tileKey"]] = {"name": item["name"], "description": remove_xml_tag(item["description"])}
    return data


def initialize_data(repo: str = GIT_REPO_DEFAULT, update: bool = True):
    """完整初始化：拉取（可选）→ 解压 → 解析 → 内存数据。"""
    if update or not os.path.exists(os.path.join(GAMEDATA_DIR, "version.txt")):
        try:
            download_gamedata(repo)
        except Exception as e:
            logger.warning(f"gamedata 拉取失败，尝试使用本地数据: {e}")

    if not os.path.exists(os.path.join(GAMEDATA_DIR, "version.txt")):
        raise RuntimeError("gamedata 数据不完整（缺少 version.txt）")

    if not os.path.exists(JSON_DIR):
        extract_gamedata_zip()

    with open(os.path.join(GAMEDATA_DIR, "version.txt"), encoding="utf-8") as f:
        GameData.version = f.read().strip("\n") or "none"

    logger.info(f"Initializing ArknightsGameData@{GameData.version}...")

    # 立绘 URL 索引
    skin_urls_path = os.path.join(GAMEDATA_DIR, "indexes", "skinUrls.json")
    if os.path.exists(skin_urls_path):
        with open(skin_urls_path, encoding="utf-8") as f:
            skin_urls = json.load(f)
        for item in skin_urls.values():
            for skin_id, url in item.items():
                SkinIndexes.url_indexes[skin_id] = url

    GameData.enemies = init_enemies()
    GameData.stages, GameData.stages_map, GameData.side_story_map = init_stages()
    GameData.operators, GameData.tokens, GameData.birthday = init_operators()
    GameData.materials, GameData.materials_map, GameData.materials_made, GameData.materials_source = init_materials()
    GameData.term_descriptions = init_term_descriptions()

    JsonData.clear_cache()
    GameData.ready = True
    logger.info(
        f"ArknightsGameData initialize completed: "
        f"{len(GameData.operators)} operators, {len(GameData.enemies)} enemies, "
        f"{len(GameData.materials)} materials, {len(GameData.term_descriptions)} terms"
    )


# ---------------------------------------------------------------------------
# 立绘 / 材料价值
# ---------------------------------------------------------------------------
async def get_skin_file(skin_data: dict, encode_url: bool = False):
    """下载立绘到 {GAMEDATA_DIR}/skin/，返回模板可用相对路径（resource/gamedata/skin/xxx.png）。"""
    skin_id = skin_data["skin_id"]
    if skin_id not in SkinIndexes.url_indexes:
        return None

    url = SkinIndexes.url_indexes[skin_id]
    skin_path = os.path.join(GAMEDATA_DIR, "skin", f"{skin_id}.png")

    if not os.path.exists(skin_path):
        os.makedirs(os.path.dirname(skin_path), exist_ok=True)
        try:
            content = await _download_url(url)
            if content:
                with open(skin_path, mode="wb") as f:
                    f.write(content)
            else:
                return None
        except Exception as e:
            logger.warning(f"下载立绘 {skin_id} 失败: {e}")
            return None

    rel = os.path.join("resource", "gamedata", "skin", f"{skin_id}.png").replace(os.sep, "/")
    if encode_url:
        rel = rel.replace("#", "%23")
    return rel


async def _download_url(url: str) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_url_sync, url)


def _download_url_sync(url: str) -> bytes:
    # prts.wiki 立绘 URL 含中文文件名与 '#'，urllib 需先做百分号编码（'#' 编码为 %23 防被当作 fragment）
    url = quote(url, safe=":/?&=%")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def load_material_value_cache():
    """从本地缓存加载一图流材料价值（无网络时也可用）。"""
    if os.path.exists(MATERIAL_VALUE_CACHE):
        try:
            with open(MATERIAL_VALUE_CACHE, encoding="utf-8") as f:
                GameData.material_values = json.load(f)
            logger.info(f"材料价值缓存已加载: {len(GameData.material_values)} 条")
        except Exception as e:
            logger.warning(f"材料价值缓存读取失败: {e}")


async def fetch_material_value():
    """从一图流拉取材料价值并写缓存（每小时可刷新一次，这里按需调用）。"""
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _fetch_material_value_sync)
        values = {}
        for item in data.get("data", []):
            values[item["itemId"]] = {
                "materialName": item["itemName"],
                "itemValue": item["itemValue"],
                "itemValueAp": item["itemValueAp"],
                "rarity": item["rarity"],
                "type": item["type"],
            }
        GameData.material_values = values
        try:
            with open(MATERIAL_VALUE_CACHE, "w", encoding="utf-8") as f:
                json.dump(values, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"材料价值缓存写入失败: {e}")
        logger.info(f"一图流材料价值已更新: {len(values)} 条")
    except Exception as e:
        logger.warning(f"一图流材料价值拉取失败: {e}")


def _fetch_material_value_sync() -> dict:
    payload = json.dumps({"expCoefficient": 0.625}).encode("utf-8")
    req = urllib.request.Request(
        YITULIU_VALUE_API, data=payload, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))
