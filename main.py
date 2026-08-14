# -*- coding: utf-8 -*-
"""明日方舟查询插件（AstrBot）。

数据自动从 gitee（amiya-bot-assets）拉取并解析到内存，复用 Amiya-Bot 的 HTML 模板
通过 Playwright 截图出图，支持 Agent 主动调用发送查询图片。

Agent 工具：
    - arknights_query_operator: 查询干员资料（详情/专精材料/技能/召唤物）
    - arknights_query_material: 查询材料（合成树/掉落/一图流价值）
    - arknights_query_enemy:    查询敌方单位资料
命令回退（需 @ 或唤醒词）：
    - 查干员 xxx / 查材料 xxx / 查敌人 xxx
"""

import asyncio
import logging
import os

from astrbot.api import star
from astrbot.api.all import (
    AstrBotConfig,
    AstrMessageEvent,
    llm_tool,
)
from astrbot.api.event import filter
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import gamedata
from . import query as akq_query
from . import render as akq_render
from .gamedata import GAMEDATA_DIR, GameData

logger = logging.getLogger("astrbot")

# 插件自身目录（安装后为 data/plugins/arknights_query/）
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(PLUGIN_DIR, "template")

# 渲染产物临时目录
TMP_DIR = os.path.join(get_astrbot_data_path(), "arknights_query", "tmp")

# 渲染等待毫秒数（Vue 渲染时间，与 Amiya-Bot 默认一致）
RENDER_TIME_MS = 500

QUERY_TYPE_HINT = {
    "info": "干员详情",
    "cost": "精英化/专精材料",
    "skills": "技能详情",
    "tokens": "召唤物",
}


def _template(name: str) -> str:
    return os.path.join(TEMPLATE_DIR, name)


async def _render_to_image(template_name: str, data: dict, width: int, timeout: int, tag: str) -> str:
    """渲染模板并保存 PNG 到临时目录，返回本地路径。"""
    png = await akq_render.html_to_image(
        _template(template_name), data, width=width, render_time=RENDER_TIME_MS, timeout=timeout
    )
    os.makedirs(TMP_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, f"{tag}.png")
    with open(path, "wb") as f:
        f.write(png)
    return path


class ArknightsQuery(star.Star):
    def __init__(self, context, config: AstrBotConfig = None):
        super().__init__(context, config)
        self.config = config or {}
        self._init_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期：数据初始化
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """插件激活时启动数据初始化（后台）。"""
        if not bool(self.config.get("akq_enabled", True)):
            return
        if GameData.ready:
            return
        # 先加载本地缓存，保证无网络时也能查询
        gamedata.load_material_value_cache()
        self._init_task = asyncio.create_task(self._init_data())

    async def _init_data(self) -> None:
        try:
            update = bool(self.config.get("akq_auto_update", True))
            repo = str(self.config.get("akq_repo_url", "") or gamedata.GIT_REPO_DEFAULT)
            # git 拉取 + JSON 解析均为阻塞操作，放入线程池
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, gamedata.initialize_data, repo, update)

            if bool(self.config.get("akq_auto_fetch_material_value", True)):
                try:
                    await gamedata.fetch_material_value()
                except Exception as e:
                    logger.warning(f"一图流材料价值拉取失败（不影响查询）: {e}")

            logger.info("明日方舟数据初始化完成")
        except Exception as e:
            logger.error(f"明日方舟数据初始化失败: {e}")

    # ------------------------------------------------------------------
    # 工具辅助
    # ------------------------------------------------------------------
    def _not_ready_message(self):
        return "博士，明日方舟数据还在初始化中（首次使用需从 gitee 拉取数据，可能耗时几分钟），请稍后再试～"

    async def _wait_ready(self, timeout: float = 600.0) -> bool:
        """等待数据就绪；超时返回 False。"""
        if GameData.ready:
            return True
        if self._init_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._init_task), timeout)
            except asyncio.TimeoutError:
                return False
        return GameData.ready

    # ------------------------------------------------------------------
    # Agent 工具
    # ------------------------------------------------------------------
    @llm_tool(name="arknights_query_operator")
    async def query_operator(self, event: AstrMessageEvent, operator_name: str, query_type: str = "info"):
        """查询明日方舟干员资料并发送图片。干员资料包括：干员详情（星级/职业/天赋/技能/属性/档案）、精英化与专精材料、技能详情、召唤物信息。请根据用户意图选择合适的 query_type；未明确时用 info。

        Args:
            operator_name(string): 干员名称，如 银灰、棘刺、W；支持中文名或英文代号（如 SilverAsh）
            query_type(string): 查询类型，可选 info(干员详情)/cost(精英化与专精材料)/skills(技能详情)/tokens(召唤物)
        """
        if not bool(self.config.get("akq_enabled", True)):
            yield event.make_result().message("博士，明日方舟查询功能当前已关闭。")
            return
        if not await self._wait_ready():
            yield event.make_result().message(self._not_ready_message())
            return

        name = akq_query.search_operator(operator_name)
        if not name:
            yield event.make_result().message(
                f"博士，没有找到干员「{operator_name}」的资料，请确认名称是否正确～"
            )
            return

        query_type = (query_type or "info").strip().lower()
        try:
            if query_type == "cost":
                data = await akq_query.get_operator_cost(name)
                template = "operatorCost.html"
            elif query_type == "skills":
                data = akq_query.get_operator_skills(name)
                template = "skillsDetail.html"
            elif query_type == "tokens":
                info, tokens = await akq_query.get_operator_detail(name)
                data = tokens
                template = "operatorToken.html"
            else:
                info, tokens = await akq_query.get_operator_detail(name)
                data = info
                template = "operatorInfo.html"

            if not data:
                yield event.make_result().message(f"博士，查询干员「{name}」的资料失败了。")
                return

            width = int(self.config.get("akq_render_width", 1280))
            timeout = int(self.config.get("akq_render_timeout", 30))
            tag = f"operator_{query_type}_{name}"
            path = await _render_to_image(template, data, width, timeout, tag)
            yield event.make_result().file_image(path)
        except Exception as e:
            logger.exception(f"干员查询渲染失败: {e}")
            yield event.make_result().message(f"博士，查询干员「{name}」时出错了：{e}")

    @llm_tool(name="arknights_query_material")
    async def query_material(self, event: AstrMessageEvent, material_name: str):
        """查询明日方舟材料/物品资料并发送图片，内容包括材料简介、合成公式（合成树）、可获取关卡与掉落概率、一图流推荐价值。用于回答「某材料怎么获得」「XX材料哪里刷」等问题。

        Args:
            material_name(string): 材料名称，如 提纯源岩、固源岩组、龙门币
        """
        if not bool(self.config.get("akq_enabled", True)):
            yield event.make_result().message("博士，明日方舟查询功能当前已关闭。")
            return
        if not await self._wait_ready():
            yield event.make_result().message(self._not_ready_message())
            return

        name = akq_query.search_material(material_name)
        if not name:
            yield event.make_result().message(
                f"博士，没有找到材料「{material_name}」的资料，请确认名称是否正确～"
            )
            return

        try:
            data = akq_query.check_material(name)
            if not data:
                yield event.make_result().message(f"博士，查询材料「{name}」的资料失败了。")
                return
            width = int(self.config.get("akq_render_width", 1280))
            timeout = int(self.config.get("akq_render_timeout", 30))
            path = await _render_to_image("material.html", data, width, timeout, f"material_{name}")
            yield event.make_result().file_image(path)
        except Exception as e:
            logger.exception(f"材料查询渲染失败: {e}")
            yield event.make_result().message(f"博士，查询材料「{name}」时出错了：{e}")

    @llm_tool(name="arknights_query_enemy")
    async def query_enemy(self, event: AstrMessageEvent, enemy_name: str):
        """查询明日方舟敌方单位资料并发送图片，内容包括敌方单位属性（血量/攻击/防御/法抗/移动速度等）、能力词条、关联单位。用于回答「XX敌人的数据」「这个敌人怎么打」等问题。

        Args:
            enemy_name(string): 敌方单位名称，如 爱国者、霜星、整合运动士兵
        """
        if not bool(self.config.get("akq_enabled", True)):
            yield event.make_result().message("博士，明日方舟查询功能当前已关闭。")
            return
        if not await self._wait_ready():
            yield event.make_result().message(self._not_ready_message())
            return

        name = akq_query.search_enemy(enemy_name)
        if not name:
            yield event.make_result().message(
                f"博士，没有找到敌方单位「{enemy_name}」的资料，请确认名称是否正确～"
            )
            return

        try:
            data = akq_query.get_enemy(name)
            if not data:
                yield event.make_result().message(f"博士，查询敌方单位「{name}」的资料失败了。")
                return
            width = int(self.config.get("akq_render_width", 1280))
            timeout = int(self.config.get("akq_render_timeout", 30))
            path = await _render_to_image("enemy.html", data, width, timeout, f"enemy_{name}")
            yield event.make_result().file_image(path)
        except Exception as e:
            logger.exception(f"敌方单位查询渲染失败: {e}")
            yield event.make_result().message(f"博士，查询敌方单位「{name}」时出错了：{e}")

    # ------------------------------------------------------------------
    # 命令回退（需 @ 或唤醒词）
    # ------------------------------------------------------------------
    async def _command_query(self, event: AstrMessageEvent, keyword: str, kind: str):
        if not bool(self.config.get("akq_enabled", True)):
            event.stop_event()
            yield event.make_result().message("博士，明日方舟查询功能当前已关闭。")
            return
        if not await self._wait_ready():
            event.stop_event()
            yield event.make_result().message(self._not_ready_message())
            return

        text = event.get_message_str() or ""
        name_part = text.replace(keyword, "", 1).strip()
        if not name_part:
            event.stop_event()
            yield event.make_result().message(
                f"博士，请在「{keyword}」后面输入要查询的名称，例如：\n{keyword} 银灰"
            )
            return

        if kind == "operator":
            result = event.make_result()
            name = akq_query.search_operator(name_part)
            if not name:
                yield result.message(f"博士，没有找到干员「{name_part}」的资料～")
                event.stop_event()
                return
            data = (await akq_query.get_operator_detail(name))[0]
            template, tag = "operatorInfo.html", f"operator_cmd_{name}"
            type_hint = "干员详情"
        elif kind == "material":
            result = event.make_result()
            name = akq_query.search_material(name_part)
            if not name:
                yield result.message(f"博士，没有找到材料「{name_part}」的资料～")
                event.stop_event()
                return
            data = akq_query.check_material(name)
            template, tag = "material.html", f"material_cmd_{name}"
            type_hint = "材料"
        else:
            result = event.make_result()
            name = akq_query.search_enemy(name_part)
            if not name:
                yield result.message(f"博士，没有找到敌方单位「{name_part}」的资料～")
                event.stop_event()
                return
            data = akq_query.get_enemy(name)
            template, tag = "enemy.html", f"enemy_cmd_{name}"
            type_hint = "敌方单位"

        if not data:
            event.stop_event()
            yield event.make_result().message(f"博士，查询{type_hint}「{name}」的资料失败了。")
            return

        try:
            width = int(self.config.get("akq_render_width", 1280))
            timeout = int(self.config.get("akq_render_timeout", 30))
            path = await _render_to_image(template, data, width, timeout, tag)
            yield event.make_result().file_image(path)
        except Exception as e:
            logger.exception(f"命令查询渲染失败: {e}")
            yield event.make_result().message(f"博士，查询「{name}」时出错了：{e}")
        event.stop_event()

    @filter.command("查干员")
    async def cmd_operator(self, event: AstrMessageEvent):
        """查干员 xxx（需 @ 或唤醒词）"""
        async for r in self._command_query(event, "查干员", "operator"):
            yield r

    @filter.command("查材料")
    async def cmd_material(self, event: AstrMessageEvent):
        """查材料 xxx（需 @ 或唤醒词）"""
        async for r in self._command_query(event, "查材料", "material"):
            yield r

    @filter.command("查敌人")
    async def cmd_enemy(self, event: AstrMessageEvent):
        """查敌人 xxx（需 @ 或唤醒词）"""
        async for r in self._command_query(event, "查敌人", "enemy"):
            yield r
