# -*- coding: utf-8 -*-
"""明日方舟查询插件（AstrBot）。

数据自动从 gitee（amiya-bot-assets）拉取并解析到内存，复用 Amiya-Bot 的 HTML 模板
通过 Playwright 截图出图。工具统一遵循「数据回传 LLM」模式：文字结果 yield 文本给 LLM
组织语言回复；资料图片 LLM 无法展示，由工具自动直发用户，同时返回简短摘要供 LLM 收尾。

Agent 工具：
    - arknights_query_operator: 查询干员资料（详情/专精材料/技能/召唤物）
    - arknights_query_material: 查询材料（合成树/掉落/一图流价值）
    - arknights_query_enemy:    查询敌方单位资料
    - arknights_query_stage:    查询关卡（地图/敌人/掉落/生息演算地图）
    - arknights_query_term:     查询术语/地形说明
    - arknights_query_recruit:  公招标签组合推荐（推荐语+组合图，支持截图视觉模型识别）
命令回退（需 @ 或唤醒词）：
    - 查干员 xxx / 查材料 xxx / 查敌人 xxx / 查关卡 xxx / 查术语 xxx / 查公招 xxx
"""

import asyncio
import json
import logging
import os
import re
import uuid

from astrbot.api import star
from astrbot.api.all import (
    AstrBotConfig,
    AstrMessageEvent,
    MessageChain,
    llm_tool,
)
from astrbot.api.event import filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Image, Reply
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import aliases as akq_aliases
from . import gamedata
from . import query as akq_query
from . import recruit as akq_recruit
from . import render as akq_render
from .gamedata import GAMEDATA_DIR, GameData
from .utils import remove_punctuation

logger = logging.getLogger("astrbot")

# 插件自身目录（安装后为 data/plugins/arknights_query/）
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(PLUGIN_DIR, "template")

# 渲染产物临时目录
TMP_DIR = os.path.join(get_astrbot_data_path(), "arknights_query", "tmp")

# 渲染等待毫秒数（Vue 渲染时间，与 Amiya-Bot 默认一致）
RENDER_TIME_MS = 500

# 多区块地图关卡（与 Amiya-Bot stages 插件一致）：地图用 {stageId}_1 / {stageId}_2 两张图
MULTIPLE_ZONE_STAGE = {"CF-9": 2, "CF-EX-8": 2, "CF-S-1": 2}

# 生息演算地图（sxys.json 记录的名称 → COS 资源 key）
SXYS_MAP_URL = "https://amiyabot-1302462817.cos.ap-guangzhou.myqcloud.com/resource/maps/{key}.jpg"

QUERY_TYPE_HINT = {
    "info": "干员详情",
    "cost": "精英化/专精材料",
    "skills": "技能详情",
    "tokens": "召唤物",
    "module": "模组",
    "skin": "皮肤",
}

# 查询意图关键词（Agent 强制工具调用钩子使用）：命中则认为用户在请求明日方舟数据查询。
QUERY_INTENT_RE = re.compile(
    r"查|查询|专精|技能|召唤物|材料|干员|敌人|敌方|怎么打|哪里刷|数据|资料|属性|关卡|地图|术语|地形|活动|突袭|磨难|公招|招募|词条|立绘|皮肤|模组|时装|造型",
    re.I,
)

# 注入到 LLM 请求的强制指令（位于系统提示词末尾，优先级最高）。
# 目的：让 Agent 在收到查询请求时"必须调用查询工具"，而不是只回文字；
# 当查询工具返回"未找到"引导时，必须按引导执行「联网搜索 → 登记代号 → 查询出图」三步。
# 输出约定：文字结果由工具 yield 返回给 Agent 组织语言；资料图片由工具自动直发用户，
# 工具同时返回简短摘要，Agent 据此收尾即可（不得重复发送图片）。
FORCE_QUERY_TOOL_PROMPT = """\
[明日方舟查询任务指令]
用户正在请求查询明日方舟数据。本机器人提供以下查询工具：
- arknights_query_operator：干员资料（详情/精英化专精材料/技能/召唤物/模组/皮肤），返回资料图片（直发）与摘要文本；模组用 query_type=module，皮肤用 query_type=skin（可带 skin_index 指定第几张）
- arknights_query_material：材料资料（合成/掉落/价值），返回资料图片（直发）与摘要文本
- arknights_query_enemy：敌方单位资料，返回资料图片（直发）与摘要文本
- arknights_query_stage：关卡资料（地图/敌方单位/掉落/生息演算地图），单关卡返回地图图片（直发）与摘要文本；同名多关卡/活动列表返回文字由你转达
- arknights_query_term：术语/地形说明，返回文字由你整理后回复
- arknights_query_recruit：公开招募（公招）标签组合推荐，返回推荐组合图（直发）与推荐语文本；用户发送公招界面截图时也会自动识别图中标签

执行规则（必须严格遵守）：
1.【第一步必须调用查询工具】把用户消息中出现的名称【原样】作为参数调用对应查询工具
   （干员→arknights_query_operator：默认 info 详情；明确要专精材料/技能/召唤物/模组/皮肤时
   分别传 query_type=cost/skills/tokens/module/skin；敌方→arknights_query_enemy，材料→arknights_query_material，
   关卡/地图/活动→arknights_query_stage，术语/地形→arknights_query_term，
   公招/招募→arknights_query_recruit，标签文本原样传入）。
   禁止先联网搜索、禁止直接文字回答。
2.【名称原样传参】用户明确给出名称就原样传入（如用户说"大哥"就传"大哥"，说"1-7 突袭"就传"1-7 突袭"，
   说"奎隆这关的地图"就传"奎隆"），不要自行联想、翻译、替换成其他名称
   （例如绝不要自作主张传成别的干员名/关卡代号）。
   仅当用户没有给出名称时（如「再查一下他的专精材料」），才用对话上下文推断。
   委派给子代理（transfer_to_subagent_arknights）时同样必须把用户原话中的名称原样放进委派输入，
   禁止把「芋圆安洁莉娜」「夏游洁」等谐音/社区称呼拆解改写成人名+皮肤名之类的猜测组合
   ——工具内置代号表可自动解析，解析不了也会返回引导，轮不到你替用户改名。
3.【禁止反问关卡代号】用户询问关卡/地图时，即使你认为该名称可能对应多个关卡
   （如肉鸽结局、多结局BOSS、同名活动关卡），也必须【立即调用 arknights_query_stage 并原样传参】，
   由工具返回候选列表或"未找到"引导；绝不允许先回复"请告诉我具体关卡代号"之类的反问再查。
   工具结果才是唯一依据，禁止用你的记忆预判关卡数量或名称。
4.【未命中引导】若查询工具返回"本地未找到"的引导，严格按引导执行：第1步调用 web_search_tavily
   确认官方名称，第2步调用 arknights_query_alias 登记代号，第3步调用查询工具（传确认后的官方名称）出图。
   在通过联网搜索确认官方名称之前，禁止用任何猜测的名称直接查询。
5.【图片直发、文字自组织】资料图片会由查询工具直接发送给用户（你无法看到图片内容），
   工具返回的"图片已发送"摘要只是告诉你图片已经发过去了；收到后自然简短收尾即可：
   确认图片已发送并提醒用户查看上方图片，可结合查询对象与对话上下文自然补一句
   （如询问用户还想看什么、确认资料是否对得上需求），不必使用固定句式、不要机械复读同一句话。
   严禁复述/转述/编造图片中的任何具体数值或材料清单，不要重复发送图片，
   不要调用 send_message_to_user 等通用发消息工具。
   文字类结果（术语、候选列表、活动列表）由工具返回给你，由你整理成自然语言回复用户；
   其中候选列表请列给用户并让其回复序号，再用原名称加 stage_index 重新调用 arknights_query_stage。
6.【输出要求】干员/材料/敌方/单关卡查询，用户要的是资料图片，最终必须通过查询工具交付图片；
   只回复文字总结而跳过工具都视为任务失败。\
"""


def _image_sent_text(subject: str) -> str:
    """资料图片直发后返回给 Agent 的简短摘要：图片已直发、让用户直接看图，Agent 自然简短收尾。"""
    return (
        f"已向用户直接发送「{subject}」的资料图片，图片已展示在用户面前，用户现在可直接查看。"
        "你无法看到图片内容。请自然简短地收尾：可确认图片已发送并提醒用户查看上方图片，"
        "也可结合查询对象与对话上下文自然地补一句（例如询问用户还想看哪个技能/材料、或确认资料是否对得上需求），"
        "不必使用固定句式。但回复必须简短（一两句话即可），严禁转述、列出或编造图片中的任何具体数值、"
        "属性、材料清单、掉落概率等内容，不要重复发送图片。"
    )

# 公招截图标签识别提示词（视觉模型）。重点：逐格查看、全部列出、禁止只挑几个。
RECRUIT_VISION_PROMPT = """这是一张《明日方舟》公开招募(公招)界面的截图，图中通常有 5 个左右的标签槽位（每个槽位一个标签），可能包含「资深干员」「高级资深干员」「新手」等特殊标签。

请识别截图里【实际占据槽位】的标签：
1. 只输出截图中真实出现（点亮/占据槽位）的标签，截图里没有的标签【绝对不要】输出；
2. 不要罗列所有可能的标签，更不要照抄下面举例；
3. 每个标签只输出名称，用中文顿号（、）分隔，只输出标签列表。

可能出现的标签举例（仅用于帮助你识别文字，不是清单，请勿全数输出）：
近战位、远程位、先锋、近卫、重装、狙击、医疗、辅助、术师、特种、资深干员、高级资深干员、新手、控场、爆发、支援、支援机械、削弱、快速复活、位移、召唤、生存、防护、群攻、治疗、输出、费用回复、减速、牵制、元素。"""


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
        self._sxys_maps: dict | None = None

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

    def _alias_miss_message(self, kind: str, user_input: str) -> str:
        """本地查找未命中时，返回给 Agent 的引导文本（直接 yield 字符串，Agent 才能读到）。

        注意：不要用 event.make_result().message() 发用户消息，那会让 Agent 收到
        "没有返回值"固定文案而看不到本引导。必须 yield 字符串作为工具返回值。
        """
        label = "干员" if kind == "operator" else "敌方单位"
        alias_tool = "arknights_query_alias"
        query_tool = "arknights_query_operator" if kind == "operator" else "arknights_query_enemy"
        return (
            f"本地数据与代号记录中未找到{label}「{user_input}」（可能是社区外号/谐音，"
            f"如 大爹 指 爱国者）。请立即按以下三步顺序执行，不要用文字直接回复用户，"
            "最终必须通过查询工具发送图片：\n"
            f"第1步：调用 web_search_tavily 搜索「明日方舟 {label} {user_input}」，"
            f"确认其对应的官方名称（未确认前禁止用你猜测的名称直接查询）；\n"
            f"第2步：调用 {alias_tool} 登记该代号（action=register, kind={kind}, "
            f"alias={user_input}, name=官方名称）；\n"
            f"第3步：调用 {query_tool} 查询（传入第1步确认的官方名称），工具会自动发送资料图片。"
        )

    # ------------------------------------------------------------------
    # Agent 工具
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def force_agent_query_tool(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """检测到明日方舟查询意图时，强制 Agent 调用查询工具出图。

        部分模型（如 mimo）收到查询请求时倾向于只回复文字、或收到"未找到"引导后
        跳过工具调用。此钩子在 LLM 请求发出前拦截：
        1) 在系统提示词末尾追加强制指令（位于安全提示词之后，优先级更高）；
        2) 确保三个查询工具在本请求的工具列表中。
        """
        if not bool(self.config.get("akq_enabled", True)):
            return
        text = event.get_message_str() or ""
        if not QUERY_INTENT_RE.search(text):
            return

        # 1) 系统提示词追加强制指令
        req.system_prompt = f"{req.system_prompt}\n\n{FORCE_QUERY_TOOL_PROMPT}"

        # 2) 确保查询工具在本次请求中可用
        if req.func_tool is not None:
            manager = self.context.get_llm_tool_manager()
            for tool_name in (
                "arknights_query_operator",
                "arknights_query_material",
                "arknights_query_enemy",
                "arknights_query_stage",
                "arknights_query_term",
                "arknights_query_recruit",
            ):
                tool = manager.get_func(tool_name)
                if tool:
                    req.func_tool.add_tool(tool)

        # 3) 查询场景移除内置发消息工具：资料图片由查询工具自动直发，
        #    若 Agent 再调 send_message_to_user 会与已直发的图片/最终回复重复，作为硬性兜底。
        if req.func_tool is not None:
            try:
                if "send_message_to_user" in req.func_tool.names():
                    req.func_tool.remove_tool("send_message_to_user")
            except Exception:
                pass

    @llm_tool(name="arknights_query_operator")
    async def query_operator(self, event: AstrMessageEvent, operator_name: str, query_type: str = "info", skin_index: int = 0):
        """查询明日方舟干员资料并发送图片。干员资料包括：干员详情（星级/职业/天赋/技能/属性/档案）、精英化与专精材料、技能详情（全部等级与专精数据）、召唤物信息、模组（解锁条件/任务/属性提升/效果/升级材料）、皮肤（立绘/系列/画师/获取途径/台词）。请根据用户意图选择合适的 query_type；未明确时用 info。注意：用户明确给出的干员名请【原样】传入本参数，不要自行替换/猜名（若未命中本地数据，本工具会返回引导，请按引导联网确认官方名称后再查询）。仅当用户未给名称时，才可根据上下文推断干员。

        Args:
            operator_name(string): 干员名称，用户明确给出时请原样传入（如用户说"二哥"就传"二哥"）；未给出时可用上下文推断，如 银灰、棘刺、W；支持中文名或英文代号（如 SilverAsh）
            query_type(string): 查询类型，可选 info(干员详情)/cost(精英化与专精材料)/skills(技能详情)/tokens(召唤物)/module(模组)/skin(皮肤)
            skin_index(int): 可选，仅 query_type=skin 时生效：1 起表示查看第 N 张皮肤，0（默认）表示最新一张
        """
        if not bool(self.config.get("akq_enabled", True)):
            yield "博士，明日方舟查询功能当前已关闭。"
            return
        if not await self._wait_ready():
            yield self._not_ready_message()
            return

        # Agent 路径不信任相似度匹配（社区外号易误匹配），未命中交给 LLM 判断
        name = akq_query.search_operator(operator_name, use_similar=False)
        if not name:
            # 未命中引导必须 yield 字符串作为工具返回值，Agent 才能读到三步引导
            yield self._alias_miss_message("operator", operator_name)
            return

        query_type = (query_type or "info").strip().lower()
        query_type = {"modules": "module", "skins": "skin"}.get(query_type, query_type)
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
            elif query_type == "module":
                data = akq_query.get_operator_modules(name)
                template = "operatorModule.html"
            elif query_type == "skin":
                card, skins, chosen = await akq_query.get_operator_skin_card(name, int(skin_index or 0))
                data = card
                template = "operatorSkin.html"
            else:
                info, tokens = await akq_query.get_operator_detail(name)
                data = info
                template = "operatorInfo.html"

            if not data:
                if query_type == "module":
                    yield f"博士，干员「{name}」目前没有已解锁/可查询的模组。"
                elif query_type == "skin":
                    yield f"博士，干员「{name}」目前没有可用皮肤。"
                else:
                    yield f"博士，查询干员「{name}」的资料失败了。"
                return

            width = int(self.config.get("akq_render_width", 1280))
            timeout = int(self.config.get("akq_render_timeout", 30))
            tag = f"operator_{query_type}_{name}"
            path = await _render_to_image(template, data, width, timeout, tag)
            # 资料图片直发用户（LLM 无法展示）：必须用 event.send 直发并正常 yield 文本摘要，
            # 不能用 yield event.make_result()（会让 agent 工具循环短路 DONE，SubAgent 委派时抛
            # "Agent did not produce a final LLM response"，主 Agent 收 error 后只能自行编造回复）
            await event.send(MessageChain().file_image(path))
            if query_type == "module":
                module_names = "、".join(
                    m.get("uniEquipName", "未知模组") for m in data
                )
                yield (
                    f"已向用户直接发送干员「{name}」的模组资料图片（共 {len(data)} 个模组：{module_names}），图片已展示在用户面前。"
                    "你无法看到图片内容。请自然简短收尾；严禁转述图片中的属性数值、材料清单等内容，不要重复发送图片。"
                )
            elif query_type == "skin":
                skin_list = "、".join(f"{i + 1}.{s['skin_name']}" for i, s in enumerate(skins))
                yield (
                    f"已向用户直接发送干员「{name}」第 {chosen}/{len(skins)} 张皮肤「{data['data']['skin_name']}」的立绘图片，"
                    f"该干员共有 {len(skins)} 张皮肤：{skin_list}。"
                    "你无法看到图片内容。请自然简短收尾；若用户想查看其他皮肤，可说明还有哪几张，"
                    "并可在用户要求后用 query_type=skin + skin_index 参数继续查询指定皮肤。"
                    "严禁转述图片细节或重复发送图片。"
                )
            else:
                yield _image_sent_text(f"干员「{name}」{QUERY_TYPE_HINT.get(query_type, '资料')}")
        except Exception as e:
            logger.exception(f"干员查询渲染失败: {e}")
            yield f"博士，查询干员「{name}」时出错了：{e}"

    @llm_tool(name="arknights_query_material")
    async def query_material(self, event: AstrMessageEvent, material_name: str):
        """查询明日方舟材料/物品资料并发送图片，内容包括材料简介、合成公式（合成树）、可获取关卡与掉落概率、一图流推荐价值。用于回答「某材料怎么获得」「XX材料哪里刷」等问题。

        Args:
            material_name(string): 材料名称，如 提纯源岩、固源岩组、龙门币
        """
        if not bool(self.config.get("akq_enabled", True)):
            yield "博士，明日方舟查询功能当前已关闭。"
            return
        if not await self._wait_ready():
            yield self._not_ready_message()
            return

        name = akq_query.search_material(material_name)
        if not name:
            yield f"博士，没有找到材料「{material_name}」的资料，请确认名称是否正确～"
            return

        try:
            data = akq_query.check_material(name)
            if not data:
                yield f"博士，查询材料「{name}」的资料失败了。"
                return
            width = int(self.config.get("akq_render_width", 1280))
            timeout = int(self.config.get("akq_render_timeout", 30))
            path = await _render_to_image("material.html", data, width, timeout, f"material_{name}")
            # 资料图片直发用户（LLM 无法展示），正常 yield 文本摘要（勿用 make_result，见 query_operator）
            await event.send(MessageChain().file_image(path))
            yield _image_sent_text(f"材料「{name}」")
        except Exception as e:
            logger.exception(f"材料查询渲染失败: {e}")
            yield f"博士，查询材料「{name}」时出错了：{e}"

    @llm_tool(name="arknights_query_enemy")
    async def query_enemy(self, event: AstrMessageEvent, enemy_name: str):
        """查询明日方舟敌方单位资料并发送图片，内容包括敌方单位属性（血量/攻击/防御/法抗/移动速度等）、能力词条、关联单位。用于回答「XX敌人的数据」「这个敌人怎么打」等问题。注意：用户明确给出的名称请【原样】传入本参数，不要自行替换/猜名（若未命中本地数据，本工具会返回引导，请按引导联网确认官方名称后再查询）。

        Args:
            enemy_name(string): 敌方单位名称，用户明确给出时请原样传入；如 爱国者、霜星、整合运动士兵
        """
        if not bool(self.config.get("akq_enabled", True)):
            yield "博士，明日方舟查询功能当前已关闭。"
            return
        if not await self._wait_ready():
            yield self._not_ready_message()
            return

        # Agent 路径不信任相似度匹配（社区外号易误匹配），未命中交给 LLM 判断
        name = akq_query.search_enemy(enemy_name, use_similar=False)
        if not name:
            # 未命中引导必须 yield 字符串作为工具返回值，Agent 才能读到三步引导
            yield self._alias_miss_message("enemy", enemy_name)
            return

        try:
            data = akq_query.get_enemy(name)
            if not data:
                yield f"博士，查询敌方单位「{name}」的资料失败了。"
                return
            width = int(self.config.get("akq_render_width", 1280))
            timeout = int(self.config.get("akq_render_timeout", 30))
            path = await _render_to_image("enemy.html", data, width, timeout, f"enemy_{name}")
            await event.send(MessageChain().file_image(path))
            yield _image_sent_text(f"敌方单位「{name}」")
        except Exception as e:
            logger.exception(f"敌方单位查询渲染失败: {e}")
            yield f"博士，查询敌方单位「{name}」时出错了：{e}"

    @llm_tool(name="arknights_query_stage")
    async def query_stage(
        self, event: AstrMessageEvent, stage_name: str, stage_index: str = ""
    ):
        """查询明日方舟关卡资料。单个关卡自动发送图片（含地图/敌方单位/掉落详情）；生息演算地图直接发送地图图片；同名/同代号多关卡返回候选列表（配合 stage_index 选择）；输入活动名返回该活动关卡列表。用于回答「1-7 怎么打」「CE-6 掉落」「SV-3 突袭」「骑兵与猎人的关卡」「生息演算 寻觅道路」「奎隆这关的地图」等问题。重要：只要用户提到关卡/地图/活动相关名称，就【立即】调用本工具并把用户原话传入 stage_name，是否命中多个关卡由本工具判断并返回候选列表，绝不要先反问用户"具体是哪个关卡"；若返回候选列表，请把列表发给用户并让用户回复序号，再用同样的名称加上 stage_index 重新调用本工具出图。

        Args:
            stage_name(string): 关卡代号或名称，原样传入，如 1-7、CE-6、SV-3、暴君、骑兵与猎人、寻觅道路、奎隆；可带难度词如 1-7 突袭、SV-3 磨难
            stage_index(string): 当关卡名命中多个同名关卡时，用于指定序号（1,2,3...），默认为空
        """
        if not bool(self.config.get("akq_enabled", True)):
            yield "博士，明日方舟查询功能当前已关闭。"
            return
        if not await self._wait_ready():
            yield self._not_ready_message()
            return

        stage_name = (stage_name or "").strip()
        if not stage_name:
            yield "博士，请提供要查询的关卡代号或名称，例如：1-7、CE-6、SV-3。"
            return

        try:
            # 0) 生息演算地图（sxys.json，COS 下载图片）
            sxys_path = await self._query_sxys(stage_name)
            if sxys_path:
                await event.send(MessageChain().file_image(sxys_path))
                yield _image_sent_text(f"生息演算地图「{stage_name}」")
                return

            # 1) 关卡匹配（代号/名称 + 难度）
            stage_ids, level, level_str = akq_query.search_stage(stage_name)
            if stage_ids:
                stage_id = None
                if len(stage_ids) == 1:
                    stage_id = stage_ids[0]
                else:
                    idx = None
                    if str(stage_index or "").strip():
                        try:
                            idx = int(str(stage_index).strip()) - 1
                        except ValueError:
                            idx = None
                    if idx is not None and 0 <= idx < len(stage_ids):
                        stage_id = stage_ids[idx]
                if not stage_id:
                    # 同名/同代号多关卡：候选列表返回给 Agent 转达用户选择
                    yield self._stage_candidates_text(stage_ids, level_str)
                    return

                data = self._build_stage_data(stage_id, level, level_str)
                if not data:
                    yield f"博士，查询关卡「{stage_name}」的资料失败了。"
                    return
                width = int(self.config.get("akq_render_width", 1280))
                timeout = int(self.config.get("akq_render_timeout", 30))
                path = await _render_to_image("stage.html", data, width, timeout, f"stage_{stage_id}")
                await event.send(MessageChain().file_image(path))
                yield _image_sent_text(f"关卡「{data['name']}」")
                return

            # 2) 活动名匹配 → 活动关卡列表（文字返回给 Agent 整理）
            story_name, ss_ids = akq_query.search_side_story(stage_name)
            if story_name:
                yield self._side_story_list_text(story_name, ss_ids)
                return

            # 3) 活动列表（文字返回给 Agent 整理）
            if "活动" in stage_name:
                yield self._activity_list_text()
                return

            # 4) 未命中
            yield (
                f"本地关卡数据中未找到「{stage_name}」（支持关卡代号如 1-7/CE-6、关卡名如 暴君、活动名）。"
                "请回复用户：未找到该关卡，请确认关卡代号或名称是否正确，可尝试带上活动名（如「别传 SV-3」）。"
                "不要用猜测的代号直接重复调用本工具。"
            )
        except Exception as e:
            logger.exception(f"关卡查询渲染失败: {e}")
            yield f"博士，查询关卡「{stage_name}」时出错了：{e}"

    @llm_tool(name="arknights_query_term")
    async def query_term(self, event: AstrMessageEvent, term_name: str):
        """查询明日方舟游戏术语/地形说明并直接发送文字结果，如「眩晕」「束缚」「沉睡」「闪避」「干员阻挡」「地形」「草丛」「高台」等。用于回答「XX术语是什么意思」「XX地形有什么用」等问题。

        Args:
            term_name(string): 术语/地形名称，如 眩晕、束缚、睡莲、草丛
        """
        if not bool(self.config.get("akq_enabled", True)):
            yield "博士，明日方舟查询功能当前已关闭。"
            return
        if not await self._wait_ready():
            yield self._not_ready_message()
            return

        term_name = (term_name or "").strip()
        if not term_name:
            yield "博士，请提供要查询的术语名称，例如：眩晕、束缚、草丛。"
            return

        results = akq_query.search_term(term_name)
        if not results:
            yield (
                f"本地术语库未找到「{term_name}」。请回复用户未找到该术语，并建议换一个说法；"
                "常见术语示例：眩晕、束缚、沉睡、闪避、位移、地形、草丛、高台。"
            )
            return

        text = f"博士，通过【{term_name}】查找到以下术语：\n"
        for item in results[:12]:
            text += f"【{item['name']}】\n{item['description']}\n"
        if len(results) > 12:
            text += f"\n（共找到 {len(results)} 条，仅显示前 12 条，可换更精确的名称查询）"
        yield text

    @llm_tool(name="arknights_query_recruit")
    async def query_recruit(self, event: AstrMessageEvent, tags_text: str = ""):
        """查询明日方舟公开招募（公招）标签组合推荐并发送「推荐语+组合图」。用于回答「公招」「帮我看看这几个标签」「生存防护怎么选」「高资 公招」「近战 生存 快速复活」等公招相关请求。用户发来公招界面截图时（即使不带标签文字），也必须调用本工具，工具会自动识别截图中的标签。注意：标签文本请【原样】传入，不要自行增删改；工具会自己判断标签有效性并给出推荐组合，若识别出多个标签会推荐最优组合（最高稀有度）。

        Args:
            tags_text(string): 公招标签文本，用户原样提供的标签或粘贴的标签组合，如 "生存防护"、"近战 生存"、"资深 快速复活"、"高资 群攻 输出"；用户仅发截图时可留空
        """
        if not bool(self.config.get("akq_enabled", True)):
            yield "博士，明日方舟查询功能当前已关闭。"
            return
        if not await self._wait_ready():
            yield self._not_ready_message()
            return

        text = (tags_text or "").strip()
        # 用户发送了公招截图（含引用截图）时，用视觉模型识别图中标签，
        # 并与 LLM 转述的标签合并——避免 SubAgent 委派/转述过程中丢失标签
        if bool(self.config.get("akq_vision_enabled", True)):
            caption_text = await self._caption_event_image(event)
            if caption_text:
                if akq_recruit.suspect_vision_hallucination(caption_text):
                    # 视觉模型把提示词示例全背出来（模板全集幻觉）时丢弃，
                    # 避免污染 LLM 转述的真实标签
                    logger.warning(
                        "公招截图视觉识别疑似模板全集幻觉，已丢弃: %s",
                        caption_text[:150],
                    )
                    caption_text = ""
                text = f"{caption_text} {text}".strip()

        tags, max_rarity = akq_recruit.parse_tags(text)
        if not tags:
            yield (
                f"从「{text or '(无文字)'}」中未识别出有效的公招标签。请回复用户：请提供公招标签"
                "（如 生存、防护、近战位、治疗、高资 等）或直接发送公招界面截图。"
            )
            return

        try:
            groups = akq_recruit.build_groups(tags, max_rarity)
            if groups is None:
                yield f"无法查询到标签【{'、'.join(tags)}】所拥有的稀有干员，请回复用户让 TA 确认标签是否正确。"
                return
            if not groups:
                yield (
                    f"根据标签【{'、'.join(tags)}】没有找到可以锁定稀有干员的组合。"
                    "请回复用户：建议提供更多标签（如 生存 防护 近战位），或发送公招界面截图。"
                )
                return

            data = {"groups": groups, "tags": tags}
            width = int(self.config.get("akq_render_width", 1280))
            timeout = int(self.config.get("akq_render_timeout", 30))
            path = await _render_to_image(
                "operatorRecruit.html", data, width, timeout, f"recruit_{'_'.join(tags)}"
            )

            # 推荐组合图直发用户（LLM 无法展示），推荐语返回给 Agent 组织语言（勿用 make_result，见 query_operator）
            await event.send(MessageChain().file_image(path))
            yield (
                f"{akq_recruit.summarize(groups, tags, max_rarity)}\n\n"
                "以上推荐组合图已直接发送给用户，请基于这份推荐语组织语言向用户呈现推荐结果。"
            )
        except Exception as e:
            logger.exception(f"公招查询渲染失败: {e}")
            yield f"博士，查询公招组合时出错了：{e}"

    # ------------------------------------------------------------------
    # 公招截图标签识别（视觉模型）
    # ------------------------------------------------------------------
    def _find_event_image(self, event: AstrMessageEvent):
        """从事件消息中取第一张图片，同时遍历引用消息（Reply.chain），
        保证用户「引用之前截图」时也能拿到图片。无图返回 None。"""

        def _iter_images(comp):
            if isinstance(comp, Image):
                yield comp
            elif isinstance(comp, Reply) and comp.chain:
                # Reply.chain 可能是 MessageChain 或 list，统一取组件列表
                sub_chain = getattr(comp.chain, "chain", None)
                if sub_chain is None:
                    sub_chain = comp.chain
                for sub in sub_chain:
                    yield from _iter_images(sub)

        # 组件列表：AstrBot 4.27.x 的 event.get_messages() 返回
        # list[BaseMessageComponent]（= message_obj.message），event.message 不存在
        msg_chain = event.get_messages()
        for comp in msg_chain:
            image_comp = next(_iter_images(comp), None)
            if image_comp is not None:
                return image_comp
        return None

    async def _caption_event_image(self, event: AstrMessageEvent) -> str:
        """从事件消息中取第一张图片（含引用消息），调用视觉模型识别公招标签。

        识别失败或没有图片时返回空串（不阻塞公招文字查询）。提示词见
        RECRUIT_VISION_PROMPT，重点要求「全部标签一个不漏」，防止只识别出其中几个。
        """
        image_comp = self._find_event_image(event)
        if image_comp is None:
            return ""
        img_ref = image_comp.url or image_comp.file
        if not img_ref:
            try:
                img_ref = await image_comp.convert_to_file_path()
            except Exception:
                return ""
        if not img_ref:
            return ""

        try:
            # 1) 优先使用配置的视觉模型 Provider；留空则回退到全局
            #    image_caption_provider_id（面板已配置时无需再改插件配置）
            prov_id = self.config.get("akq_vision_provider_id", "")
            if not prov_id:
                try:
                    cfg = self.context.get_config()
                    prov_id = (
                        cfg.get("provider_ltm_settings", {})
                        .get("image_caption_provider_id", "")
                        or ""
                    )
                except Exception:
                    prov_id = ""
            provider = None
            if prov_id:
                provider = self.context.get_provider_by_id(prov_id)
            if provider is None:
                provider = await self.context.get_using_provider_async()
            if provider is None:
                logger.warning("未找到可用的视觉模型 Provider，跳过公招截图识别")
                return ""
            resp = await provider.text_chat(
                prompt=RECRUIT_VISION_PROMPT,
                session_id=uuid.uuid4().hex,
                image_urls=[img_ref],
                persist=False,
            )
            text = (resp.completion_text or "").strip()
            logger.info(f"公招截图视觉模型识别结果: {text}")
            return text
        except Exception as e:
            logger.warning(f"公招截图视觉模型识别失败: {e}")
            return ""

    # ------------------------------------------------------------------
    # 关卡辅助
    # ------------------------------------------------------------------
    async def _query_sxys(self, text: str):
        """生息演算地图：匹配 sxys.json 中的名称并下载 COS 图片，返回本地路径；未命中返回 None。"""
        sxys_path = os.path.join(PLUGIN_DIR, "sxys.json")
        if not os.path.exists(sxys_path):
            return None
        if self._sxys_maps is None:
            with open(sxys_path, encoding="utf-8") as f:
                self._sxys_maps = json.load(f) or {}
        titles = {}
        for item, key in self._sxys_maps.items():
            titles[item] = key
            titles[remove_punctuation(item, ["-"])] = key
        best, best_key = "", None
        for title, key in titles.items():
            if title and title in text and len(title) > len(best):
                best, best_key = title, key
        if not best_key:
            return None
        cache_dir = os.path.join(TMP_DIR, "sxys")
        os.makedirs(cache_dir, exist_ok=True)
        target = os.path.join(cache_dir, f"{best_key}.jpg")
        if not os.path.exists(target):
            try:
                content = await gamedata._download_url(SXYS_MAP_URL.format(key=best_key))
                if content:
                    with open(target, "wb") as f:
                        f.write(content)
                else:
                    return None
            except Exception as e:
                logger.warning(f"生息演算地图下载失败 {best_key}: {e}")
                return None
        return target

    def _build_stage_data(self, stage_id: str, level: str, level_str: str):
        """构建 stage.html 渲染数据（对应 Amiya-Bot stages 插件的组装逻辑）。"""
        stage = GameData.stages.get(stage_id)
        if not stage:
            return None
        res = {
            **stage,
            "name": stage["name"] + level_str,
            "zones": MULTIPLE_ZONE_STAGE.get(stage["code"], 0),
        }
        if level == "_easy":
            main_level = GameData.stages.get(stage_id.replace("easy", "main"))
            if main_level:
                res["levelData"] = main_level["levelData"]
        # 地图缺失时降级到 main 关卡地图（与 Amiya-Bot 一致）
        if not os.path.exists(os.path.join(GAMEDATA_DIR, "map", f"{res['stageId'].replace('#f#', '')}.png")):
            res["stageId"] = res["stageId"].replace("tough", "main").replace("easy", "main")
        # 兜底：缺关卡数据/掉落数据时保证模板不报错
        ld = res.get("levelData") or {}
        res["levelData"] = {
            "options": ld.get("options") or {},
            "enemyDbRefs": ld.get("enemyDbRefs") or [],
            "enemiesCount": ld.get("enemiesCount") or {},
        }
        res.setdefault("stageDropInfo", {"displayDetailRewards": []})
        return res

    def _stage_candidates_text(self, stage_ids: list, level_str: str) -> str:
        lines = ["博士，找到以下同名/同代号关卡，请回复序号查询对应的关卡：\n"]
        for index, sid in enumerate(stage_ids):
            stage = GameData.stages.get(sid)
            if not stage:
                continue
            lines.append(f"[{index + 1}] {stage['code']} {stage['name']}{level_str}")
        return "\n".join(lines)

    def _side_story_list_text(self, story_name: str, stage_ids: list) -> str:
        lines = [f"博士，以下是活动【{story_name}】的关卡列表：\n|关卡代号|关卡名|关卡代号|关卡名|", "|---|---|---|---|"]
        for index, sid in enumerate(stage_ids):
            stage = GameData.stages.get(sid)
            if not stage:
                continue
            code = stage["code"] + ("**突袭**" if stage.get("difficulty") == "FOUR_STAR" else "")
            name = stage["name"] + ("**（突袭）**" if stage.get("difficulty") == "FOUR_STAR" else "")
            line = f"|{code}|{name}"
            if (index + 1) % 2 == 0:
                line += "|"
            lines.append(line)
        return "\n".join(lines)

    def _activity_list_text(self) -> str:
        lines = ["博士，以下是活动列表：\n|活动名|活动名|活动名|活动名|", "|---|---|---|---|"]
        for index, name in enumerate(reversed(list(GameData.side_story_map.keys()))):
            lines.append(f"|{name}{'|' if (index + 1) % 4 == 0 else ''}")
        return "\n".join(lines)

    @llm_tool(name="arknights_query_alias")
    async def manage_alias(self, event: AstrMessageEvent, action: str, kind: str, alias: str, name: str = ""):
        """管理干员/敌方单位的代号（别名）记录。用户查询时经常使用社区外号/谐音（如 夏游洁 指 予愿安洁莉），查询工具会自动先用别名记录解析输入。当查询工具返回「未找到」、而你能通过联网搜索或对话上下文确认用户指的是哪个干员/敌人时，用本工具登记代号，登记后后续查询可直接命中；也可查询已登记的别名或删除错误记录。

        Args:
            action(string): 操作类型，可选 query(查询已登记的别名)/register(登记别名)/remove(删除别名)
            kind(string): 类别，可选 operator(干员)/enemy(敌方单位)
            alias(string): 用户使用的代号，如 夏游洁
            name(string): 对应的规范名称，仅 register 时必填，如 予愿安洁莉娜
        """
        action = (action or "").strip().lower()
        kind = (kind or "").strip().lower()
        alias = (alias or "").strip()

        if not bool(self.config.get("akq_enabled", True)):
            # 工具返回值（yield 字符串）给 Agent，由 Agent 转达用户
            yield "博士，明日方舟查询功能当前已关闭。"
            return

        if action == "register":
            if not name:
                yield "登记代号需要提供规范名称（name），如：夏游洁 → 予愿安洁莉娜。"
                return
            # 校验并规范化为搜索命中的规范名，避免登记非规范名
            canonical = None
            if kind == "operator":
                canonical = akq_query.search_operator(name)
            elif kind == "enemy":
                canonical = akq_query.search_enemy(name)
            if not canonical:
                yield (
                    f"登记失败：{kind} 类中找不到与「{name}」对应的干员/敌人，"
                    "请确认名称正确（可用联网搜索确认官方名称后再登记）。"
                )
                return
            if akq_aliases.register(kind, alias, canonical):
                yield f"已登记代号：{kind}「{alias}」→「{canonical}」。以后直接查询「{alias}」即可命中。"
            else:
                yield "登记失败：kind 需为 operator 或 enemy。"
        elif action == "remove":
            if akq_aliases.remove(kind, alias):
                yield f"已删除代号「{alias}」。"
            else:
                yield f"代号「{alias}」不存在，无需删除。"
        elif action == "query":
            table = akq_aliases.all_aliases(kind if kind in ("operator", "enemy") else None)
            if not any(table.values()):
                yield "暂无已登记的代号记录。"
                return
            lines = []
            for k in ("operator", "enemy"):
                if table.get(k):
                    lines.append(f"{k}：")
                    for a, n in table[k].items():
                        lines.append(f"  {a} → {n}")
            yield "\n".join(lines)
        else:
            yield "未知操作，action 可选 query / register / remove。"

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

    @filter.command("查关卡")
    async def cmd_stage(self, event: AstrMessageEvent):
        """查关卡 xxx（需 @ 或唤醒词）：关卡代号/名称/活动/生息演算地图"""
        if not bool(self.config.get("akq_enabled", True)):
            event.stop_event()
            yield event.make_result().message("博士，明日方舟查询功能当前已关闭。")
            return
        if not await self._wait_ready():
            event.stop_event()
            yield event.make_result().message(self._not_ready_message())
            return

        text = (event.get_message_str() or "").replace("查关卡", "", 1).strip()
        if not text:
            event.stop_event()
            yield event.make_result().message(
                "博士，请在「查关卡」后面输入要查询的关卡，例如：\n查关卡 1-7\n查关卡 CE-6 突袭\n查关卡 骑兵与猎人"
            )
            return

        try:
            sxys_path = await self._query_sxys(text)
            if sxys_path:
                event.stop_event()
                yield event.make_result().file_image(sxys_path)
                return

            stage_ids, level, level_str = akq_query.search_stage(text)
            if stage_ids:
                stage_id = stage_ids[0] if len(stage_ids) == 1 else None
                if not stage_id:
                    event.stop_event()
                    yield event.make_result().message(self._stage_candidates_text(stage_ids, level_str))
                    return
                data = self._build_stage_data(stage_id, level, level_str)
                if data:
                    width = int(self.config.get("akq_render_width", 1280))
                    timeout = int(self.config.get("akq_render_timeout", 30))
                    path = await _render_to_image("stage.html", data, width, timeout, f"stage_cmd_{stage_id}")
                    event.stop_event()
                    yield event.make_result().file_image(path)
                    return

            story_name, ss_ids = akq_query.search_side_story(text)
            if story_name:
                event.stop_event()
                yield event.make_result().message(self._side_story_list_text(story_name, ss_ids))
                return

            if "活动" in text:
                event.stop_event()
                yield event.make_result().message(self._activity_list_text())
                return

            event.stop_event()
            yield event.make_result().message(
                f"博士，没有找到关卡「{text}」的资料，请确认关卡代号或名称是否正确，可尝试带上活动名（如「别传 SV-3」）。"
            )
        except Exception as e:
            logger.exception(f"命令关卡查询失败: {e}")
            event.stop_event()
            yield event.make_result().message(f"博士，查询关卡「{text}」时出错了：{e}")

    @filter.command("查术语")
    async def cmd_term(self, event: AstrMessageEvent):
        """查术语 xxx（需 @ 或唤醒词）"""
        if not bool(self.config.get("akq_enabled", True)):
            event.stop_event()
            yield event.make_result().message("博士，明日方舟查询功能当前已关闭。")
            return
        if not await self._wait_ready():
            event.stop_event()
            yield event.make_result().message(self._not_ready_message())
            return

        text = (event.get_message_str() or "").replace("查术语", "", 1).strip()
        if not text:
            event.stop_event()
            yield event.make_result().message("博士，请在「查术语」后面输入术语名称，例如：\n查术语 眩晕")
            return

        results = akq_query.search_term(text)
        if not results:
            event.stop_event()
            yield event.make_result().message(f"博士，没有找到术语「{text}」的资料，请换一个说法试试～")
            return
        msg = f"博士，通过【{text}】查找到以下术语：\n"
        for item in results[:12]:
            msg += f"【{item['name']}】\n{item['description']}\n"
        if len(results) > 12:
            msg += f"\n（共找到 {len(results)} 条，仅显示前 12 条，可换更精确的名称查询）"
        event.stop_event()
        yield event.make_result().message(msg)

    @filter.command("查公招")
    async def cmd_recruit(self, event: AstrMessageEvent):
        """查公招 标签组合（需 @ 或唤醒词）：支持文字标签与公招截图识别"""
        if not bool(self.config.get("akq_enabled", True)):
            event.stop_event()
            yield event.make_result().message("博士，明日方舟查询功能当前已关闭。")
            return
        if not await self._wait_ready():
            event.stop_event()
            yield event.make_result().message(self._not_ready_message())
            return

        text = (event.get_message_str() or "").replace("查公招", "", 1).strip()
        if not text:
            caption_text = await self._caption_event_image(event)
            if not caption_text:
                event.stop_event()
                yield event.make_result().message(
                    "博士，请在「查公招」后输入公招标签（例如：查公招 生存防护），或发送公招界面截图。"
                )
                return
            text = caption_text

        tags, max_rarity = akq_recruit.parse_tags(text)
        if not tags:
            event.stop_event()
            yield event.make_result().message(
                f"博士，没有从「{text}」中识别出有效的公招标签，请提供如 生存、防护、近战位、高资 等标签～"
            )
            return

        try:
            groups = akq_recruit.build_groups(tags, max_rarity)
            if not groups:
                event.stop_event()
                yield event.make_result().message(
                    f"博士，根据标签【{'、'.join(tags)}】没有找到可以锁定稀有干员的组合，建议提供更多标签再试试～"
                )
                return

            data = {"groups": groups, "tags": tags}
            width = int(self.config.get("akq_render_width", 1280))
            timeout = int(self.config.get("akq_render_timeout", 30))
            path = await _render_to_image(
                "operatorRecruit.html", data, width, timeout, f"recruit_cmd_{'_'.join(tags)}"
            )
            result = event.make_result()
            result.message(akq_recruit.summarize(groups, tags, max_rarity))
            result.file_image(path)
            event.stop_event()
            yield result
        except Exception as e:
            logger.exception(f"命令公招查询失败: {e}")
            event.stop_event()
            yield event.make_result().message(f"博士，查询公招组合时出错了：{e}")
