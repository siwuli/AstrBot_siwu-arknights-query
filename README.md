# AstrBot_siwu-arknights-query

明日方舟（Arknights）游戏数据查询插件，适用于 [AstrBot](https://github.com/Soulter/AstrBot)。由 Amiya-Bot 的同名查询插件移植而来，复用其 HTML 模板与数据源，通过 Playwright 无头浏览器截图出图。

支持 **Agent 主动调用**：在对话中让 AI 助手（Agent）自主判断并调用工具，自动发送查询生成的图片。

## 功能

- **干员查询**：星级/职业/天赋/技能/属性/档案（详情页）、精英化与专精材料、技能详情、召唤物信息
- **材料查询**：材料简介、合成公式（合成树）、可获取关卡与掉落概率、一图流推荐价值
- **敌方单位查询**：属性（血量/攻击/防御/法抗/移动速度等）、能力词条、关联单位
- **代号记录**：支持登记干员/敌方单位的社区外号（如「夏游洁」→「予愿安洁莉」），查询时自动用代号记录解析用户输入，Agent 可自助登记/更新/删除

数据自动从 gitee 拉取（`amiya-bot-assets`）并解析到内存，无需手动更新。

## 安装

1. 在 AstrBot 管理面板 → 插件管理 → 安装插件 → 上传 zip（本仓库 Release 中的 `siwu-arknights-query-<version>.zip`）
2. 插件依赖 `playwright`，需在 AstrBot 环境安装 Chromium 浏览器内核：

   ```bash
   # 在 AstrBot 的 Python 环境执行（Windows 示例）
   python -m playwright install chromium
   ```

   > 依赖已固定为 `playwright==1.53.0`，与该内核版本匹配，请勿随意升级。

3. 插件激活后会自动在后台从 gitee 拉取游戏数据（首次拉取需要几分钟），完成后即可查询。

## 使用

### Agent 主动调用（推荐）

安装后，在支持 Agent 的 AstrBot 对话中直接提问，AI 会自动调用工具并发送图片：

- 「查一下干员 银灰」
- 「棘刺的专精材料要什么」
- 「W 的技能详情」
- 「银灰有召唤物吗」
- 「提纯源岩哪里刷」
- 「爱国者这个敌人的数据」
- 「查一下夏游洁」（若「夏游洁」是已登记的代号，会命中「予愿安洁莉」）

查询流程中的代号处理：

1. 查询工具会**先查代号记录**，命中社区外号/历史代号时直接解析为规范干员名
2. 未命中且本地无此名时，工具会提示 Agent 可**联网搜索**确认规范名
3. Agent 确认后调用代号管理工具**登记**（`夏游洁 → 予愿安洁莉`），后续查询直接命中
4. 用户反馈不对时，Agent 会**更新/删除**代号记录

### 命令回退（需 @ 机器人或唤醒词）

- `查干员 xxx` — 干员详情
- `查材料 xxx` — 材料资料
- `查敌人 xxx` — 敌方单位资料

## 配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `akq_enabled` | 启用明日方舟查询（关闭后工具与命令不再响应） | `true` |
| `akq_auto_update` | 启动时自动拉取/更新游戏数据（关闭则使用本地已有数据） | `true` |
| `akq_repo_url` | 游戏数据仓库地址（Amiya-Bot 官方数据源，一般无需修改） | `https://gitee.com/amiya-bot/amiya-bot-assets.git` |
| `akq_auto_fetch_material_value` | 自动拉取材料价值数据（一图流 yituliu.cn） | `true` |
| `akq_render_width` | 渲染图片宽度（px）。HTML 模板为 1280px 固定画布，无需修改 | `1280` |
| `akq_render_timeout` | 渲染超时时间（秒） | `30` |

## 数据来源

- 游戏数据：<https://gitee.com/amiya-bot/amiya-bot-assets>（Amiya-Bot 官方数据仓库，随游戏版本自动更新）
- 材料价值：一图流 <https://yituliu.cn>

## 移植注意

- **Chromium 内核**：本机若缺少匹配的 Playwright 浏览器内核会渲染失败，必须执行 `python -m playwright install chromium`
- **git 依赖**：首次安装（无本地数据时）需通过 `git clone` 拉取数据源，机器需可用 git；已有数据时失败会回退本地缓存
- 渲染出图需要本机能访问本地文件与正常启动 Chromium（无头模式）

## 版本记录

| 版本 | 内容 |
| --- | --- |
| 1.0.2 | 新增干员/敌方单位代号（别名）记录：Agent 可查询/登记/删除社区外号，查询工具自动先用代号记录解析输入，未命中时提示联网搜索确认 |
| 1.0.1 | 修复专精材料图片缺失精英 1/2 图标问题（补齐 level/rank/classify 资源目录）；修复渲染视口高度 720 消除底部空白；默认渲染宽度统一为 1280 |
| 1.0.0 | 初版，从 Amiya-Bot 查询插件移植到 AstrBot，支持 Agent 主动调用 |

## 鸣谢

- 模板与数据源源自 [Amiya-Bot](https://github.com/AmiyaBot/Amiya-Bot) 的 arknights 查询插件
- 数据与图片版权归游戏厂商（Hypergryph）所有，仅用于学习交流
