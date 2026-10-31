# QQ 群大模型聊天机器人

Windows + Python 3.11 及以上版本。接入链路：QQ群 → NapCat（OneBot 11 正向 WebSocket）→ 本程序 → 大模型 Chat Completions API → 群回复。

本仓库提供独立的 Python 机器人应用，不包含 QQ 客户端、NapCat 程序、私人群聊记录或图片素材。首次使用需要自行安装 NapCat，并填写自己的群白名单与模型 API 配置。

## 功能

- 支持 `@机器人 问题`、`/ai 问题`，以及无需 @ 的选择性自动接话；全部只在白名单群生效。
- 当自动接话模式为 `smart` 或 `keywords` 且 `REPLY_ON_NAME=true` 时，“机器人出来”“老哥在吗”这类直接叫名字的消息会直接请求回答，绕过自动判断、自动冷却及自动小时配额，仍受个人冷却与总并发限制。一般提及“机器人领域”等话题不会作为点名。
- 文字和图片回复都参考本群最近 10 分钟内最多 16 条聊天片段，包括 @、点名、`/ai` 和自动接话；图片会结合话题接话，只发图不再默认要求逐项解说。关闭自动接话时仍保留本群短期上下文，供明确提问使用。
- 自动接话由模型决定回复或沉默。默认每群每小时最多检查 60 次、尝试回复 12 次，两次自动回复至少间隔 60 秒；检查间隔至少 20 秒。明确 @别人、回复他人的消息、明显刷屏默认不插话。
- 按“群号 + 用户 QQ”隔离个人多轮对话。开启持久记忆后，最近 8 轮成功对话可保留 7 天并在重启后恢复；个人偏好、称呼与近况另存为少量长期记忆。各群另有独立的共享短期上下文；只记录程序运行期间收到的消息，不补抓启动前的 QQ 历史消息。
- `/ai reset` 只清空近期对话；`/ai memory` 查看本人的长期记忆；`/ai forgetme` 删除本群个人记忆并停止记录；`/ai remember-on` 恢复记录。`/ai help` 查看完整命令。
- 默认同一用户两次请求间隔至少 3 秒，最多同时处理 3 个请求。忙碌时忽略新请求，请等回复后再提问。
- 群回复直接发送，支持文字、QQ 自带表情和素材库图片/GIF，不自动 @ 发言者；模型生成的 CQ 代码始终按普通文本展示。
- 默认一条消息一句话，确实有必要时偶尔补充第二句；每轮最多 2 条消息，默认条间隔 0.8 秒。自动回复配额按每条实际发送尝试计数。
- 群友有疑问、求助或征求建议时优先给实际帮助，直接回答或给排查办法；调侃不代替答案，信息不足时先给可用的一步或补问一个关键问题。
- 支持断线重连、消息 ID 去重、请求超时、错误提示和滚动日志。不自动重试模型请求或群消息发送，避免重复计费/重复回复。
- 支持文字和图片：JPEG、PNG、GIF、WebP，默认单条最多 3 张、每张最多 6 MiB；模型须支持图文输入。语音、文件和合并转发中的内容不展开。
- 支持引用同群的一条图片消息后让机器人解释；程序会读取被引用消息中的图片和文字。跨群引用或已经无法读取的消息会明确提示重新发送。
- 可接入 DeepSeek 官方 Search/Fetch 工具，由模型在明确 @、点名或 `/ai` 请求中按需调用；自动插话不使用联网工具。
- 保留 @其他群友的信息，并尽量解析群名片/昵称；无法获取昵称时仍保留 QQ 号。机器人不执行模型返回的命令，不提供管理群的能力。

## 1. 安装并登录 NapCat

NapCat 是第三方 QQ 接入工具，可能触发 QQ 登录风控；建议使用专门的机器人账号。

按 [NapCat 官方 Windows 安装说明](https://doc.napneko.icu/guide/boot/Shell)安装。下载入口以官方说明为准，不需要在本项目目录里安装 QQ。启动 NapCat，按终端提示用手机 QQ 扫码登录机器人账号，并将这个账号加入目标群。

打开 NapCat 终端中显示的 WebUI 地址（通常为 `http://127.0.0.1:6099/webui/`），在**网络配置 → 新建 → WebSocket 服务端（正向 WS）**中填写：

| 配置 | 值 |
| --- | --- |
| 启用 | 是 |
| Host / 监听地址 | `127.0.0.1` |
| Port / 端口 | `3001` |
| 消息格式 / messagePostFormat | `array` |
| Token / 访问令牌 | 自己设置一串随机字符串，后面填入本程序 |
| 上报自身消息 / reportSelfMessage | 关闭 |

保存并启用。这是 **WebSocket 服务端**，不要选成客户端、HTTP 服务端，也不要把 WebUI 的 6099 端口填给机器人。

本程序和 NapCat 都在这台电脑运行时无需公网 IP。网络配置依据 [NapCat WebUI 配置指南](https://doc.napneko.icu/config/basic)。WebUI 登录口令与 OneBot WebSocket 的 Token 是两处配置。

可选的本地辅助脚本适用于将 NapCat Shell 安装到 `.runtime/napcat/` 的情况：设置 `QQ_EXE` 环境变量为 QQ 可执行文件的完整路径后，可用 `start-napcat.cmd` 启动；`open-napcat.cmd` 根据本地 WebUI 配置打开管理页面。其他安装方式请使用 NapCat 官方启动方式。这些脚本不会自动下载 QQ 或 NapCat，也不包含登录凭据。

## 2. 安装本程序依赖

双击 `install.cmd`。这会创建项目专用的 `.venv`，安装依赖，并在没有 `.env` 时复制配置模板。

开发者也可以在 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. 配置大模型和群号

双击 `configure.cmd`，依次填写 WebSocket 地址、WebSocket Token、允许回复的群号、模型 API 地址、模型名、API Key。密钥在本地终端输入时不回显；已有值回车保留。修改 `.env` 后重启机器人生效；其中 `BOT_NAMES` 和 `AUTO_REPLY_KEYWORDS` 支持保存后在下一条白名单群消息时自动加载。

默认模板使用 DeepSeek；截至本项目创建时，其官方文档列出的配置是：

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_API_KEY=填写你的密钥
```

API Key 从 [DeepSeek 控制台](https://platform.deepseek.com/)创建，接口格式和可用模型以 [DeepSeek 官方 API 文档](https://api-docs.deepseek.com/)为准。网页聊天账号不等于已开通 API 服务。

其他服务商只需使用该服务商的 **Chat Completions 兼容**基础地址、模型名和 Key；若服务商文档要求 `/v1`，基础地址中应保留 `/v1`。程序会自动追加 `/chat/completions`，不要把完整接口 URL 填入基础地址。其他协议不能直接接入。

`.env` 中 `ALLOWED_GROUPS=123456789,987654321` 表示两个允许回复的群；留空不会允许所有群。`SYSTEM_PROMPT` 可修改性格，`LLM_EXTRA_JSON` 可填写服务商要求的额外 JSON 参数。默认不指定思考模式，按模型默认行为调用。不要把密钥发送到群里或提交到 Git。

## 4. 检查和启动

先双击 `doctor.cmd`：读取登录账号和已加入的群，检查群白名单，不发群消息、不调用大模型。

再在 PowerShell 中运行下面的命令，测试一次大模型请求（会使用 API 额度）：

```powershell
.\.venv\Scripts\python.exe -m qqbot doctor --llm
```

最后双击 `start.cmd`，保持 NapCat 和机器人两个进程都在运行。用**另一个 QQ 账号**在白名单群里发送：

```text
@机器人 你好，介绍一下自己
/ai 帮我想三个周末活动
/ai reset
```

机器人忽略自身账号发出的消息，不能用机器人账号自己发消息来测试。按 Ctrl+C 停止。

## 排查问题

| 现象 | 检查方法 |
| --- | --- |
| 连接失败/一直重连 | NapCat 是否正在运行、QQ 是否登录、正向 WS 是否启用、端口与 Token 是否一致；先运行 doctor |
| 能连接但群里不回复 | 确认是其他 QQ 发消息、群号在白名单、消息包含 @机器人或独立 `/ai` 前缀、未处于忙碌或冷却期；确认连接的是正向 WS 根地址 |
| 401/403 | 检查大模型 API Key 和调用权限 |
| 402/429 | 检查 API 余额、额度和服务商限流 |
| 404 | 检查基础地址和模型名，基础地址不要重复 `/chat/completions` |
| 超时 | 检查网络；长思考模型可以适当增大 `LLM_TIMEOUT_SECONDS`、`MAX_TOKENS` |
| 群回复发送失败 | 查看 NapCat 日志，确认账号未被禁言、平台未限制发言 |

本程序日志位于 `logs/bot.log`，只记录状态、群号和用户号，不记录消息内容、API Key 或上游错误响应。NapCat 自身有独立日志设置。持久记忆开启时，部分自述及最近成功对话会保存到本地数据库；提取与回复时会发送必要内容给配置的大模型服务商。

## 发图片、表情包和多条消息

配置模板已开启 `RICH_REPLY_ENABLED=true`。DeepSeek 根据话题选择文字、QQ 自带表情或素材库图片，绝大多数时候只发一条消息、说一句话，必要时偶尔补充第二句；每轮最多 2 条消息，明确点名、`/ai` 和自动接话都支持。

```dotenv
RICH_REPLY_ENABLED=true
REPLY_MAX_MESSAGES=2
REPLY_MESSAGE_DELAY_SECONDS=0.8
MEDIA_MAX_ASSETS_PER_GROUP=100
MEDIA_CATALOGUE_LIMIT=40
```

QQ 自带表情可以马上使用，例如 `/ai 发表情 呲牙`。本地图片放入 `assets/common/`，文件名就是标签，例如 `绷不住了_笑哭.gif`，新增后无需重启。这些本地公共素材可用于全部白名单群。

在群里发送 `/ai 收藏表情 绷不住了` 并附一张图，或者引用同群图片后发送这个指令，会把原文件保存到该群独立的素材库。普通发图不会自动收藏。GIF 保留原字节。

| 指令 | 用途 |
| --- | --- |
| `/ai 素材库` | 查看本群可用的素材名称和 ID |
| `/ai 收藏表情 名称` + 附图/引用 | 收藏一张图，仅在原群使用 |
| `/ai 发图 名称或ID` | 直接发送指定素材 |
| `/ai 发表情 呲牙` | 直接发送 QQ 表情 |
| `/ai 删除表情 名称或ID` | 删除本群收藏；公共图在本机文件夹管理 |

这些管理指令不调用大模型。自然地说“大肥鱼来个表情包”“分两条讲讲”时，模型会选择合适的发送方案。初始自定义素材库为空，可以先收藏几张常用图；QQ 自带表情不依赖素材库。

素材说明见 [assets/README.md](assets/README.md)。素材图片在本机以受限 ID 解析后交给 NapCat 发送，模型拿到的是标签列表。收到素材 URL、任意路径、未知 ID 或不支持的消息类型时不会执行。部分消息发送失败时停止该轮，不自动重发；已经发出的部分会保留在本群短期上下文中。

## 图片输入和自动接话配置

在 `.env` 中配置并重启聊天程序：

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_API_KEY=填写你的密钥
VISION_ENABLED=true
AUTO_REPLY_MODE=smart
BOT_NAMES=机器人,老哥
REPLY_ON_NAME=true
AUTO_CHECK_INTERVAL_SECONDS=20
AUTO_REPLY_COOLDOWN_SECONDS=60
AUTO_MAX_CHECKS_PER_HOUR=60
AUTO_MAX_REPLIES_PER_HOUR=12
GROUP_CONTEXT_MESSAGES=16
GROUP_CONTEXT_TTL_SECONDS=600
MAX_IMAGES=3
MAX_IMAGE_BYTES=6291456
IMAGE_TIMEOUT_SECONDS=20
```

截至 2026-10-25，DeepSeek 官方文档确认 `deepseek-flash` 支持图片输入，`deepseek-v4-pro` 不支持图片。请以 [识图说明](https://api-docs.deepseek.com/guides/vision/)和[模型能力表](https://api-docs.deepseek.com/quick_start/pricing/)为准。识图默认使用非思考模式降低延迟；明确提问可通过 `LLM_EXTRA_JSON` 指定思考配置。DeepSeek 的 smart 自动判断固定使用非思考模式，以满足指定函数返回的接口要求。人设仍由 `SYSTEM_PROMPT` 决定。

`AUTO_REPLY_MODE` 支持三种值：

| 值 | 行为 |
| --- | --- |
| `smart` | 无需 @；在频率预算内把当前消息及最近群聊交给模型，一次调用同时决定是否接话及生成回复 |
| `keywords` | 无需 @；命中 `BOT_NAMES` 或 `AUTO_REPLY_KEYWORDS` 的消息才调用模型回复 |
| `off` | 仅 @机器人或 `/ai` 触发；识图仍可使用 |

自动回复有选择性，不保证每个问题都触发；达到检查/回复上限、冷却中或忙碌时会跳过，恢复后也不会补发旧消息。@机器人和 `/ai` 不受自动接话预算限制，仍受个人冷却与总并发限制。所有限流计数只在当前进程内保存，重启会重置。

DeepSeek 官方 API 的自动接话通过指定的 `group_reply_decision` 函数返回是否回复及实际内容，程序严格校验后只发送自然回复。这是一次模型请求中的数据返回，不执行外部操作、不调用联网工具，也不增加第二次模型请求。这样避免强制 JSON 模式的空返回问题，以及多轮历史下模型直接输出普通文字造成的解析失败。缺少决策、函数名不符或格式错误时会记为失败并保持沉默，不把内部决策发进群，也不自动重试。其他兼容接口保留原来的 JSON 模式。参见 [DeepSeek 接口的 tool_choice 说明](https://api-docs.deepseek.com/api/create-chat-completion/)。

明确提问的联网次数达到上限后，程序会移除搜索工具，只保留最终回复入口。模型若仍提出额外工具调用，该调用不会执行，最多补充一次最终答复引导；继续请求工具则明确报收尾失败，避免无限循环及超出搜索配额。

想让普通群聊更活跃，可以在 `.env` 中改成下面这组值，再重启聊天程序。上面的 20/60/60/12 是安装模板的保守默认值。

```dotenv
AUTO_CHECK_INTERVAL_SECONDS=5
AUTO_REPLY_COOLDOWN_SECONDS=15
AUTO_MAX_CHECKS_PER_HOUR=240
AUTO_MAX_REPLIES_PER_HOUR=60
```

四项依次表示：每群自动判断的最小间隔（秒）、两次自动回复的最小间隔（秒）、每群每小时自动判断次数上限、每群每小时自动回复尝试次数上限。前两项越小越活跃，后两项越大允许的次数越多；`smart` 判断本身也会使用 API 额度。`COOLDOWN_SECONDS=3` 单独控制同一用户的请求间隔，包括 @和名字呼叫。`BOT_NAMES` 可换成你给机器人取的名字，用英文或中文逗号分隔；`REPLY_ON_NAME=false` 则让名字呼叫继续交由自动判断。

例如 `BOT_NAMES=大肥鱼,机器人,老哥,ds,deepseek`。英文别名忽略大小写，支持“ds认识我吗”“DS今天有什么新闻”这样紧接中文的呼叫，也支持“ds 认识我吗”；“DSLR”“ds2”不会误认成 `ds`。修改 `BOT_NAMES` 或 `AUTO_REPLY_KEYWORDS` 并保存后，下一条白名单群消息会加载新值，无需重启。其他配置仍需重启。`AUTO_REPLY_KEYWORDS` 只在 `keywords` 模式筛选普通群聊；`smart` 模式下，想增加直接唤醒的称呼请改 `BOT_NAMES`。

每次调用模型都会携带本群近期文字、发送者昵称/QQ 号及机器人在回复谁，与当前用户的个人对话和长期记忆一起供模型理解。`smart` 的候选检查也会发送这些背景；即使模型最终沉默，这次判断也使用 API 额度。当前候选消息有图片时，会临时下载 QQ 附件并以 base64 发送给模型；引用消息中有图时也会读取（只展开一层引用）。正常聊天不把图片落盘、不把图片链接写进聊天记忆，后续轮次只保留“本轮含图片”的标记与模型回答。继续询问同一图片的细节时，可以引用原图片消息，或重新发图。普通网页中的图片链接不会自动下载。

只发图片时会结合前文判断是在分享乐子、吐槽、求助还是继续某个话题，以当前人设简短接话；明确说“解释这张图”“翻译图中文字”等时才按要求说明。前文不足或看不清时可简短询问，不编造共同经历。短期群聊不会写入其他成员的个人对话记录；程序重启后从新收到的消息重新积累。

`/ai reset` 会清空你在本群的近期对话，以及共享短期上下文里由你发出的片段和机器人对你的回复，保留长期信息。全部删除并停止记录用 `/ai forgetme`；其他用户的内容仍按上下文过期规则清理。

测试方法：先在目标群发送“@机器人 这张图是什么”并附图，再发普通问题观察自动接话；使用 `/ai status` 查看开关和本群计数。你也可以主动把指定的本地图片发送给模型验证（使用 API 额度）：

```powershell
.\.venv\Scripts\python.exe -m qqbot doctor --image "C:\path\to\test.png"
```

## DeepSeek 联网搜索

截至 2026-10-25，DeepSeek 提供独立的 [Search/Fetch API](https://api-docs.deepseek.com/guides/search/)，分别是 `/v1/tools/search` 和 `/v1/tools/fetch`，与聊天接口共用 DeepSeek API Key。聊天请求不会仅因为提示词要求“搜索”就执行搜索。本程序通过工具调用接入它们，工具返回后再由模型生成最终回复。

需要联网时可启用以下配置；安装模板默认关闭，其他服务商暂不支持这两个官方接口：

```dotenv
SEARCH_ENABLED=true
SEARCH_MAX_TOOL_CALLS=2
SEARCH_MAX_CALLS_PER_HOUR=60
SEARCH_TIMEOUT_SECONDS=20
```

点名后可以说“大肥鱼搜一下某某的最新消息”或“大肥鱼读一下这个网页”。模型会按需检索或读取，简短回答并附来源链接。每条请求最多调用 2 次联网工具，本进程每小时最多尝试 60 次；计数在重启后重置。普通自动接话不调用联网工具。搜索和网页读取使用额外 API 调用，服务商的计费和额度以官方文档为准。

网页内容只作为待分析资料；网页里的角色声明、工具调用指令不会被当作本程序的指令。当前仅有搜索和读取公开网页两个工具，不执行 shell、登录网站或写入外部服务。工具失败时会把错误交给模型如实说明，不假装已搜索成功。

## 持久记忆与群友识别

配置模板已开启 `MEMORY_ENABLED=true`。记忆绑定到“群号 + 发言者QQ号”，同一QQ改昵称后仍能关联，两个群里的记录分开保存。数据位于 `.runtime/memory.sqlite3`，目录已被 Git 忽略。

- 自动记住部分明确自述，如称呼、一般兴趣、正在学习或准备的事情；模型不能指定其他人的QQ号，也只能保存当前发言中逐字存在的自述片段。不会把别人的引述、图片识别结果或机器人的猜测自动记成你的事实。
- 每人默认最多 24 条，普通信息最多保留 180 天，正在进行的事最多 30 天、近况事件最多 90 天。回复时只注入最多 6 条相关记录；这仍可能漏记或选错相关内容，重要信息可明确说“记住”。
- 自动提取在后台运行，每人至少间隔 120 秒，本进程每小时最多 40 次，最多两项提取同时进行。只有像个人自述的消息才提取，提取本身使用一次额外模型调用；即使机器人没有接话，也可能记住这条自述。
- 最近成功对话默认保存最多 8 轮、7 天，重启后可继续。图片原始数据、工具结果、系统提示词及检测到的密码/密钥不保存进近期对话。
- 不会回读以前的群消息补建档案，从开启后收到的消息开始记录。记录与昵称仅在当前群、当前QQ名下使用。

群里可以使用：

| 消息 | 作用 |
| --- | --- |
| `大肥鱼记住我喜欢下围棋` 或 `/ai remember 我喜欢下围棋` | 立即保存这条简短自述，无需额外提取调用 |
| `/ai memory` 或 `大肥鱼查看记忆` | 查看自己在本群的长期记录 |
| `/ai memory 围棋` | 按关键词查看自己的记录 |
| `/ai forget 咖啡` 或 `大肥鱼忘记咖啡` | 删除自己包含该关键词的记录并清空近期对话 |
| `/ai forgetme` 或 `大肥鱼忘记我` | 清除本群个人记录和近期对话，停止继续记录；即使有旧提取正在进行也不会写回 |
| `/ai remember-on` 或 `大肥鱼恢复记忆` | 重新开启自己的本群记忆 |
| `/ai reset` | 只清空近期对话，保留长期记录 |

自然聊天可问“大肥鱼你还记得我喜欢什么吗”。模型会读取当前发言者的记录，不会把另一个QQ的档案混在一起。记忆管理指令只操作发送者本人在当前群的记录。

可在 `.env` 调整这些值并重启：

```dotenv
MEMORY_ENABLED=true
MEMORY_MAX_FACTS_PER_USER=24
MEMORY_INJECT_FACTS=6
MEMORY_FACT_DAYS=180
MEMORY_HISTORY_DAYS=7
MEMORY_EXTRACT_INTERVAL_SECONDS=120
MEMORY_MAX_EXTRACTS_PER_HOUR=40
```

## 本地验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用本机模拟 OneBot WebSocket 服务以及模拟大模型/图片下载 HTTP 响应，覆盖图片输入、自动判断沉默/回复、消息去重、频率预算、对话隔离及其他成员 @ 的保留，不调用付费 API。真实识图正确率和自动接话是否合适，仍需配置 API Key 后在群里验证。

## 代码结构

| 路径 | 用途 |
| --- | --- |
| `qqbot/bot.py` | 消息触发、上下文、限流与发送流程 |
| `qqbot/llm.py` | 模型请求、自动接话决策与联网收尾 |
| `qqbot/messages.py`、`qqbot/onebot.py` | QQ 消息解析与 OneBot 通信 |
| `qqbot/memory.py` | 按群与用户隔离的 SQLite 记忆 |
| `qqbot/images.py`、`qqbot/media.py`、`qqbot/replies.py` | 图片输入、素材库和图文回复 |
| `qqbot/config.py` | 配置读取与唤醒词更新 |
| `tests/` | 使用模拟接口的本地测试 |

## 发布与外部依赖

只提交源码、测试、脚本、文档和 `.env.example`。真实 `.env`、`.runtime/`、`logs/`、虚拟环境、记忆数据库及图片素材均由 `.gitignore` 排除；不要通过网页整目录上传本机工作目录，网页上传不会替你应用 Git 的忽略规则。

NapCat 和 QQ 需要用户独立安装。NapCat 受其[上游许可证](https://github.com/NapNeko/NapCatQQ/blob/main/LICENSE)约束；本仓库不分发该程序或其修改版本。Python 依赖通过 `requirements.txt` 安装，分别遵循各自的许可证。请只添加自己有权分享的图片和提示词。
