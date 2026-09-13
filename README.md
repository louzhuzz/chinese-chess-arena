# 中国象棋擂台 · Chinese Chess Arena

本地双大模型中国象棋对弈与评测工具。后端负责规则裁判、模型调用、对局调度和 SQLite 记录；网页用于配置模型、实时观战、回放与批量评测。

- 规则裁判：项目自有的纯 Python 内核 `xiangqi-bench-rules-1.0`，覆盖基本走子、将杀、困毙、重复局面、长将、长捉和自然限着。
- 模型接入：OpenAI Chat Completions、OpenAI Responses、Claude Messages，以及不需要密钥的本地模拟。
- 提供方管理：常用提供方只需选择名称并填写 API Key，系统自动配置地址、协议并同步模型；私有网关使用“自定义提供方”。
- 人机对弈：把一方设为「人类（网页落子）」即可在棋盘上点子落子，可玩人机、人人或 AI 对 AI。
- 自由摆棋：任意局面都能开一局，可直接改 FEN 或在棋盘上摆子。
- 中文棋谱：观战、回放、私有记忆和 JSON 导出显示“炮二平五”“前马进七”等标准记法，内部坐标仍作为辅助信息保留。
- 对局契约：模型每步收到完整棋子位置表、最近 8 步、上一手实际吃子及排序后的合法走法，只携带本方最新一条最多 80 字短计划。历史请求只归档，不重复送入模型。

## 快速开始

```powershell
cd D:\AIcodes\xiangqi-bench
.\run.ps1
```

`run.ps1` 会创建虚拟环境、安装依赖、构建网页，然后启动 <http://127.0.0.1:8000>。裁判无需 C++ 编译器、外部进程或模型文件。首次运行会把 `config\models.example.yaml` 复制为 `config\models.yaml`。

不配置任何密钥也能先跑通流程：示例配置里有本地模拟预设 `模拟·首步` 与 `模拟·末步`，选择它们即可完成对局、回放与批量评测。

接入真实模型：

```powershell
$env:OPENAI_API_KEY = "..."
$env:ANTHROPIC_API_KEY = "..."
```

密钥推荐只在配置里写环境变量名 `api_key_env`。网页与导出接口只会返回掩码，棋谱、日志和 CSV 都不包含密钥。

## 规则裁判

规则实现位于 `backend/app/rules.py`，裁判入口位于 `backend/app/arbiter.py`。每次判定都使用初始 FEN 和完整历史；内部用有界缓存增量回放，因此相同 FEN 可根据历史得到不同裁决，同时不会随着对局增长反复从头计算。

- 基础规则：车、马腿、象眼与不过河、士帅九宫、炮架、兵卒过河、将帅照面、自陷被将。
- 终局：将死和困毙均判无合法着法的一方负。
- 三次重复：普通重复和互相违规判和；单方连续将军判长将负；单方以非帅、非兵棋子连续追逐同一目标判长捉负。
- 自然限着：连续 120 个单方着数无吃子、无兵卒推进判和。
- 子力和棋：仅剩帅将、士和象时判和。

长打裁决参考中国象棋协会审定的《象棋竞赛规则（2020版）》原则实现。复杂棋例存在裁判解释空间，因此每局都会保存 `ruleset_id`，以后调整必须升级版本号，旧棋谱仍按原版本统计。

## 结果码

| 码 | 含义 |
|---|---|
| `checkmate` / `stalemate` | 将死 / 困毙（困毙判负） |
| `repetition_draw` | 三次重复局面判和 |
| `natural_limit_draw` | 自然限着（120 个单方着数无吃子、无兵卒推进）判和 |
| `insufficient_material` | 子力不足判和 |
| `perpetual_check` / `perpetual_chase` | 长将 / 长捉判负 |
| `invalid_move` / `timeout` | 非法着法或超时技术判负 |
| `api_failure` | 接口故障，不计入胜负 |
| `max_plies` / `user_stopped` / `process_restart` | 截断、手动停止、进程重启中断，均不算和棋 |

每步允许一次纠错：格式错误或非法走法会把固定提示与合法走法一起回传，再次失败才判技术负。接口错误在时限内重试一次，仍失败记接口故障。

## 对局输入契约

模型收到的公共局面（双方提示词模板一致，彼此回复不进入对方上下文）：

```json
{
  "game_id": "…", "ply": 8, "side_to_move": "red",
  "pieces": [{"side": "red", "type": "cannon", "square": "b2"}],
  "history": ["b0c2", "b9c7"],
  "legal_moves": ["a0a1", "b0a2"],
  "private_memory": [{"ply": 4, "move": "b2e2", "note": "控制中路，下一手关注黑车"}],
  "in_check": false, "ruleset_id": "xiangqi-bench-rules-1.0",
  "move_timeout_seconds": 600,
  "remaining_timeout_seconds": 119, "request_timeout_seconds": 95,
  "output_token_limit": 16384
}
```

模型只需返回：

```json
{"move": "b2e2", "note": "可选的私有计划，最多 80 字"}
```

坐标固定为红方视角 `a0`—`i9`：`a0` 是红方左下角，不随行棋方翻转。`legal_moves` 按坐标排序，不含评分或推荐。

`move_timeout_seconds` 是本回合从请求开始到提交答案的总时限，包含接口重试和一次纠错。对弈台默认 600 秒（10 分钟），可用环境变量 `XIANGQI_MOVE_TIMEOUT`（5-3600 秒）改默认值，也可在对弈台直接改单局数值。`output_token_limit` 是本次推理和最终答案合计的输出上限。内置提示词要求模型选定合法候选后立即提交，不重新枚举规则或证明全局最优。提示词不构成硬性推理长度保证。

模型输入只保留一份完整 `pieces`，不再同时发送 FEN 和字符棋盘；逐步 FEN 仍在棋谱和回放数据中。提示词版本为 `xiangqi-move-v6-submit-once`。首答的思考强度和输出上限仍使用对局所选配置。

红黑双方的 `private_memory` 完全隔离，只携带本方最近一次成功走法的短计划（最多 80 字）；失败回复不进入记忆，原始回复完整保存在棋谱。笔记不能覆盖当前棋盘事实。

每次请求只发送固定系统提示词和当前局面，不追加旧棋盘及旧回答。`history` 为最近 8 个半回合，`history_start_ply` 标识起点；完整历史仍存于棋谱。首次请求可用「整步剩余时限 − min(剩余 20%, 30 秒)」的纠错余量：120 秒档首答 96 秒（与旧行为一致），600 秒档首答 570 秒——慢速推理模型不再被固定的 80% 掐断，超时前仍留 30 秒走纠错重试；纠错请求输出上限为 512 token，并请求关闭思考。各协议实际发送的模型参数保存在 `actual_request`，不含密钥；是否支持关闭思考取决于提供方协议。每次请求还明确告知整步剩余时间与本次请求时限。

缓存策略按提供方能力适配。DeepSeek 使用相同前缀自动命中；官方 Anthropic 请求在稳定系统提示和本轮消息设置 5 分钟 `cache_control`；官方 OpenAI Responses 请求使用按连接、模型和提示词版本稳定生成的 `prompt_cache_key`（不把单局 ID 放进键，允许同一模型跨局复用前缀）。每步保存 `total_input_tokens`、`cache_read_tokens`、`cache_write_tokens` 和 `cache_miss_tokens`，报告中的缓存命中率为 `cache_read_tokens / total_input_tokens`；提供方未返回用量时显示未知，不把未知当作 0。

创建对局时会冻结红黑双方的模型 ID、生成参数、推理参数、单价和提示词版本。之后修改 YAML 不会改变已经创建的对局；导出 JSON 会包含冻结配置、每次请求、原始回复和私有笔记。

## 对弈模式：一次定一步 / 规则工具

对弈台可以选择两种模式，规则裁判、棋谱、回放和数据库完全共用，只有「模型怎么决定这一步」不同。所选模式同时作用于「开始一局」「从当前盘面继续」和「批量评测」——评测的每一局都按当前模式运行，轮数一并冻结在 `benchmarks.settings_json` 里，报告页会标明本批用的是哪种模式。

| 模式 | 提示词版本 | 一手之内 |
|---|---|---|
| 一次定一步（`direct`，默认） | `xiangqi-move-v6-submit-once` | 一次请求给出一步；非法或格式错误按固定提示纠错一次 |
| 规则工具（`agent`） | `xiangqi-agent-v1-tools` | 可连续调用规则工具查询、试走、校验、修正，直到正式提交（默认最多 6 轮请求） |

智能体模式把「棋盘事实」交给程序，把「选哪一步」留给模型。开局快照**不含合法着法列表**：模型要么直接给出自己想走的着法，要么先用工具问清规则事实，平台不推荐、不评分、不替它展开搜索。

| 工具 | 作用 |
|---|---|
| `get_position()` | 完整棋盘、行棋方、将军状态与 `position_id` |
| `get_legal_moves(from?)` | 全部合法着法，或某枚棋子的合法着法（按坐标排序） |
| `check_move(move)` | 检查一步是否合法，并说明炮架、马腿、阻挡等具体原因 |
| `simulate_line(moves)` | 在副本里执行模型提出的变化（最多 12 步，保留真实历史），返回吃子与终局判定 |
| `get_history(start, end)` | 按段查询真实棋谱（最多 40 个半回合） |
| `read_note()` / `write_note(text)` | 读取、替换本方短笔记（≤400 字，写入即替换，附手数） |
| `submit_move(move, position_id)` | 正式落子；再次校验，成功后本手立即结束 |

约定与边界：

- **一手最多生效一次落子。** `submit_move` 之外的任何工具都不会改动真实棋盘；重复提交、`position_id` 过期（手数已前进）、对局已停止的迟到提交都会被拒绝。正式提交非法只给一次纠正机会：第二次正式提交再非法即按非法着法判负，`check_move`/`simulate_line` 试出非法属于正常探索，不占用这个次数。
- **截止时间对落子同样有效。** 整手时限一到，工具层直接拒绝落子（`expired`），正文里给出的着法也不再受理，本手按 `timeout` 结束——时限边界上不会出现「先判超时、又把这一步写进棋谱」。
- **没有成功提交就没有落子。** 记录 `agent_rounds_exhausted`、`agent_no_submit`、`agent_output_limit`、`timeout` 等精确原因并判该方超时，平台不擅自代选一步棋。
- **预算按整手管理。** 时间、轮数、输出用量都在这一手累计：每轮请求的 `max_tokens` 只能是「对局冻结的输出上限 − 本手已用输出」，用尽即以 `agent_output_limit` 收手，不会每轮都重新拿满额度；单次请求不超过整手的 40%（最低 30 秒），剩余 20%（上限 30 秒）留给提交窗口，进入提交窗口后只保留 `submit_move` 并提示模型立即提交；这一手已经实测出单次请求耗时后，只要剩余时间不够「实测耗时 × 1.2」，就直接进入提交窗口而不必先发出去再超时。一次请求超时或接口故障不会立刻判负：整手保留一次「只提交」的重试（对应直接模式的纠错一次），这次请求拿到剩下的全部时间。轮数用尽即结束本手。可用 `XIANGQI_AGENT_MAX_ROUNDS`（默认 6）、`XIANGQI_AGENT_SUBMIT_RESERVE`（0.2）、`XIANGQI_AGENT_SUBMIT_RESERVE_CAP`（30 秒）、`XIANGQI_AGENT_REQUEST_CAP`（0.4）、`XIANGQI_AGENT_REQUEST_CAP_FLOOR`（30 秒）、`XIANGQI_AGENT_REQUEST_MARGIN`（1.2）、`XIANGQI_AGENT_MIN_REQUEST`（15 秒）调整。实践上建议每步时限 ≥200 秒：工具循环要发多次请求，120 秒档容易被一次慢请求吃掉大半预算。
- **双方记忆各自独立。** 每手开头自动给一份紧凑快照（完整棋子表、最近 8 个半回合、对手上一手的移动与吃子、本方短笔记、`position_id`），跨手只带这些，不搬运旧棋盘与试走过程；一手之内完整保留工具调用与返回，便于连续判断。笔记只属于写它的那一方，永远不进入对方上下文。
- **推理续接字段按协议回传。** 工具轮次的助手消息会把提供方的推理字段原样带回下一次请求（OpenAI 兼容的 `reasoning_content`、Responses 的 reasoning item、Anthropic 的 thinking block），否则开启思考的严格接口会在第二轮直接拒绝。
- **行动回放与费用。** 每次请求、每次工具调用、每次提交都写进 `agent_actions`；网页「智能体行动回放」按手数列出「查询合法着法 → 试走变化 → 校验一步 → 正式落子 a0a1」这样的链条，展开可看输入、返回、耗时、连接与首字节耗时、Provider Request ID、本轮实际发出的 `max_tokens` 与时限、提供方返回的结束原因（`finish_reason`）；每手费用按该手全部请求（含重试与失败）累计 token 与估算费用，单价或用量未知时显示「未知」，不把未知当作 0。每一轮实际发出的请求参数（已去密钥）与提供方原始回复都完整落盘在 `agent_actions.request_json` / `raw_json`，整手汇总写进 `moves.actual_request_json`，用于事后区分「输出耗尽」「参数没生效」还是「接口异常」。

## 配置

`config\models.yaml` 在内部仍分两层，网页统一称为“提供方”和“模型”：

- **连接**：`id`、`name`、`protocol`、`base_url`、`api_key` 或 `api_key_env`、`extra_headers`；通过内置目录添加的连接还会保存 `builtin_provider_id`，便于使用该提供方的本地模型目录。
- **预设**：`id`、`name`、`connection_id`、`model`、`temperature`、`max_tokens`、`structured_output`、`reasoning_effort`、输入/输出单价。

一个连接可以挂多个预设；红黑双方可选同一预设自我对弈，也可分别配置。`protocol: mock` 时 `base_url` 就是策略：`first`、`last`、`invalid_once`、`prefer:a1d1`。`protocol: human` 表示这一方由网页落子，不需要密钥。

网页的“添加提供方”只收录能直接使用 OpenAI Chat Completions、OpenAI Responses 或 Anthropic Messages 兼容协议的 API Key 提供方。当前内置目录包括：DeepSeek、OpenAI、Anthropic、Ant Ling、Baseten、Cerebras、Groq、Hugging Face、Kimi For Coding、MiniMax（国际/中国）、Moonshot AI（国际/中国）、NVIDIA、OpenRouter、Qwen Token Plan（国际/中国/Individual）、Together、Vercel AI Gateway、xAI、Xiaomi（API 与 Token Plan 区域）、Z.AI（国际/Coding CN）。选择后只需输入密钥，地址、协议、密钥环境变量和常用模型会自动填入；模型目录不依赖提供方 `/models` 接口，未列出的模型可在“手动添加模型 ID”中补充。自定义提供方仍可使用同样的三种协议，并通过“同步模型”请求其 `/models`。

思考强度在对弈台按红黑双方分别选择：提供方默认、关闭、低、高、最高。设置随对局冻结；批量交换先后时，强度跟随模型而不是跟随棋子颜色。

DeepSeek 走 OpenAI 兼容协议（`base_url: https://api.deepseek.com/v1`）。当前官方模型 ID 是 `deepseek-flash`（V4.1 Flash）和 `deepseek-v4-pro`；`deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` 仍可作为兼容旧名称手动填写，但对应旧模型已经退役。该接口暂不支持 `response_format: json_schema`，预设里 `structured_output` 要关掉；推理模型会先把 `max_tokens` 花在思考上，预算给小了会返回空正文（引擎侧现在会明确记为「被 max_tokens 截断」，并在纠错时自动关掉思考重试一次）。`deepseek-v4-pro` 计划在 2026 年 9 月 14 日起暂时路由到 V4.1 Flash；如需控制耗时，建议在对弈台选择较低思考强度或关闭思考。

## 人机对弈与自由摆棋

- 把执红或执黑选成 `我执红` / `我执黑`（`protocol: human`），点「开始一局」：轮到人类时棋盘可点，先点自己的棋子显示可走位置，再点目标格落子；非法走法不会提交，规则由裁判判定。
- 人类一方的等待时间默认 30 分钟（`XIANGQI_HUMAN_TIMEOUT` 可调），超时按超时判负处理，与模型方一致。
- 点「自由摆棋」进入摆棋模式：左侧选棋子后点棋盘落子，再点同一格清除；可直接编辑 FEN（失焦时校验），切换红先/黑先，然后「以此开局」。开局前双方必须各有一个将/帅，否则裁判会拒绝。
- 摆棋模式也可以从当前局面开始改（进入时默认载入当前棋盘）。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务状态、当前自有规则版本 |
| GET | `/api/config` | 连接（掩码）、预设与内置提供方目录 |
| GET | `/api/providers` | 只读的内置兼容提供方目录（不含密钥） |
| PUT | `/api/connections/{id}`、`PUT /api/presets/{id}` | 新增或更新 |
| POST | `/api/connections/{id}/test` | 探测提供方连接 |
| POST | `/api/connections/{id}/sync-models` | 内置连接从本地目录同步；自定义连接从 `/models` 同步；加 `?refresh=true` 强制请求端点 |
| POST | `/api/games` | 创建并启动对局 |
| POST | `/api/games/{id}/move` | 人类方提交一步（仅在该方行棋且等待输入时有效） |
| POST | `/api/analyze` | 裁判分析任意局面：给 `fen`，或给 `initial_fen` + `history` |
| GET | `/api/games`、`/api/games/{id}` | 列表与明细（含每次尝试、上下文统计、等待方与合法着法） |
| POST | `/api/games/{id}/stop` | 停止 |
| GET | `/api/games/{id}/events` | SSE：快照、逐步走子、终局 |
| GET | `/api/games/{id}/export.json` | 完整归档（含每次请求、原始回复与双方上下文） |
| GET | `/api/games/{id}/export.fen` | **标准棋谱**（`?positions=true` 返回纯 FEN 序列），带下载文件名 |
| GET | `/api/games/{id}/timeline` | 智能体行动回放：每手的请求、工具调用、落子与费用 |
| GET | `/api/games/{id}/timeline.txt` | 同一份回放的文本版，带下载文件名 |
| POST | `/api/benchmarks` | 创建 N 对交换先后的评测（共 2N 局）；与单局同样接受 `mode`（`direct`/`agent`）与 `agent_max_rounds` |
| GET | `/api/benchmarks/{id}` | 报告：胜负、技术判负、首答合法率、纠错成功率、耗时、token、费用；`settings` 里保留本批用的是哪种模式 |
| GET | `/api/benchmarks/{id}/export.csv` | 汇总导出 |

命令行：

```powershell
.\.venv\Scripts\python.exe -m backend.app.cli game --red <红方预设ID> --black <黑方预设ID>
.\.venv\Scripts\python.exe -m backend.app.cli game --red heilovehei-gpt-6 --black heilovehei-gpt-6-astra --mode agent --agent-rounds 6
.\.venv\Scripts\python.exe -m backend.app.cli timeline --game <对局ID>
.\.venv\Scripts\python.exe -m backend.app.cli benchmark --a <预设A> --b <预设B> --pairs 5
.\.venv\Scripts\python.exe -m backend.app.cli benchmark --a <预设A> --b <预设B> --pairs 3 --mode agent --agent-rounds 6
.\.venv\Scripts\python.exe -m backend.app.cli export --game <对局ID> [--out 目录]
```

预设 ID 取自 `config\models.yaml`（或网页「模型」页已有的模型）。不配任何密钥时，选 `我执红` / `我执黑` 就能在网页上人机对弈；测试与离线联调用的是各自独立的临时配置，不受这里影响。

## 棋谱格式（标准）

一局棋对外交换用的就是 FEN 棋谱，由判决即可复现的原始数据生成，不依赖渲染。网页「行棋记录」右上角的「FEN 棋谱」按钮、`GET /api/games/{id}/export.fen` 和 CLI `export` 三条路径产出的内容完全一致（同一渲染器 `backend/app/record.py`）。

文件名固定为 `game-<对局ID前8位>-fen.txt`（棋谱）与 `game-<对局ID前8位>-positions.fen`（纯局面序列）。

```text
中国象棋擂台 · FEN 棋谱
==============================================================================
对局 ID  : 486429429fa2436fab22af29c09ac8d2
执红/执黑: heilovehei-gpt-5-6-sol / ds-flash
结果     : 黑方胜 · 将死    共 80 半回合
规则版本 : xiangqi-bench-rules-1.0    每步时限 600 秒
==============================================================================
格式: 序号 行棋方 着法(内部坐标) 中文记法 | 走子后的 FEN

  0 开局                      | rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1
  1 红 炮二平五(h2e2)         | rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR b - - 1 1
  2 黑 马八进七(h9g7)         | rnbakab1r/9/1c4nc1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR w - - 2 2
  …
 80 黑 车五平四(e3d3)         | 2b2ab2/4k4/3a5/p5P1p/2p6/9/P2r5/3K5/9/9 w - - 1 41

终局局面已由裁判确认: 将死
```

- 每个半回合一行，FEN 是该手走完后的局面；第 0 行是起始局面。序号从 1 开始，与「第 N 手」在网页上的编号一致。
- 内部坐标固定红方视角 `a0`—`i9`，不随行棋方翻转；中文记法为空时（例如摆棋局面无法记谱）只保留坐标。
- 结果码中文说法见 `backend/app/record.py` 的 `REASON_LABELS`，与网页使用同一套措辞。
- 只有真正走成的半回合会进棋谱；失败尝试、原始回复和双方私有记忆留在 `export.json` 里。
- `-positions.fen` 是一行一个 FEN（含起始局面），可直接粘进「自由摆棋」的 FEN 框或喂给其它程序。
- 换行符随写盘平台（Windows 上为 CRLF），内容行与本文档一致。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q     # 规则、裁判、调度、HTTP 端到端
cd frontend; npm run build                  # TypeScript 检查与网页构建
```

测试用一个本地假模型服务器同时模拟 OpenAI Chat、OpenAI Responses 和 Claude Messages，覆盖：将杀终局、纠错一次、技术判负、超时、接口故障、停止、步数截断、批量评测换先与统计、密钥掩码、SSE 与导出、提示词契约与双方上下文隔离，以及人机对弈。规则用例覆盖马腿、炮架、将帅照面、相同 FEN 不同历史、三次重复、长将、长捉、自然限着、子力和棋、将死、困毙和非法输入。智能体模式另有一组用例（`tests/test_agent.py`、`tests/test_timeline.py`），覆盖一手内多轮请求只落一子、非法探针不中断本手、正式提交非法后纠正、过期 `position_id`、轮数与预算上限、停止后迟到提交作废、双方笔记隔离、正文兜底提交，以及三种线上协议的工具调用往返与行动回放/费用统计。

## 许可与来源

规则运行时为本项目独立实现，不链接、调用或分发任何外部棋力引擎。旧版差分验证用的引擎源码与构建产物只保留在开发机，不属于本项目发行内容。

网页棋盘与棋子素材来自 [lengyanyu258/xiangqiboardjs](https://github.com/lengyanyu258/xiangqiboardjs)（MIT License，chessboard.js 的中国象棋分支）：`frontend\src\assets\xiangqi\board.svg` 与 `{r,b}{A,B,C,K,N,P,R}.svg`。棋子造型源自 Wikimedia Commons 的中国象棋棋子 SVG。许可全文与来源说明见该目录的 `LICENSE.md` 与 `SOURCE.md`，对外分发时请一并保留。

## 已知边界

- 首版聚焦固定开局的双模型比较；残局题库、多模型循环赛与等级分排名尚未实现。
- 网页支持连接新增、更新、测试与预设编辑；复杂自定义请求头仍可直接编辑 `config\models.yaml`。
- 对局串行执行；同一时刻的手动对局与评测互不干扰，规则判定直接在后端进程内完成。
- 摆棋模式只做合法性校验（裁判接口），不做「双方子力是否可能达成」之类的可达性检查；摆出不合法局面时开局会被拒绝。
- 批量评测不接受人类预设（会一直等人输入，无法计入统计）。
- 智能体模式只提供规则事实工具，不提供评分、推荐着法或搜索；如果将来加入代码工作区，需要与规则工具模式分开报告棋力。
- 智能体模式的每手费用按 preset 的输入/输出单价估算；未填单价的预设只显示 token 与请求次数，不显示金额。
