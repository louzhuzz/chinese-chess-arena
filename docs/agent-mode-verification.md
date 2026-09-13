# 智能体模式（规则工具）实现与验证

日期：2026-09-13。提示词版本：`xiangqi-agent-v1-tools`（直接模式仍为 `xiangqi-move-v6-submit-once`）。
设计依据：把「棋盘维护和计算」留在平台，把「选哪一步」留给模型；一手之内允许连续行动。

## 实现概览

| 部分 | 位置 | 作用 |
|---|---|---|
| 规则工具 | `backend/app/tools.py` | `get_position`、`get_legal_moves(from?)`、`check_move`、`simulate_line`、`get_history`、`read_note`/`write_note`、`submit_move`；每个工具绑定本局、本方与当前手数 |
| 一手循环 | `backend/app/agent.py` | `AgentTurn`：模型请求 → 工具执行 → 结果回传，直到正式提交、轮数用尽或预算耗尽 |
| 落子与记录 | `backend/app/runner.py` | `_agent_move()`、`_record_action()`：每条行动落盘并推 SSE 事件；只有 `submit_move` 会真正落子 |
| 行动回放 | `backend/app/timeline.py` | 每手的请求、工具、落子与费用；文本回放与网页共用同一份数据 |
| 协议适配 | `backend/app/models.py` | `choose_with_tools()` 把中性消息转换成 OpenAI Chat / OpenAI Responses / Anthropic Messages 三种工具调用格式 |
| 数据 | `backend/app/db.py` | `agent_actions`（一手内逐条行动）、`agent_notes`（双方各自短笔记）、`games.mode` / `games.agent_max_rounds` |

约定：

- 开局快照**不含** `legal_moves`：模型可以直接提交，也可以先用工具核实规则事实；平台不评分、不推荐、不替它展开搜索。
- 「一手最多生效一次落子」由 `SubmitState` + `position_id` 保证：重复提交、过期提交、停止后的迟到提交全部被拒。
- 一手内的所有请求、工具调用、每次提交（含被拒的）都写进 `agent_actions`，带耗时、token、连接耗时、首字节耗时与 Provider Request ID。
- 跨手只带：完整棋子表、最近 8 个半回合、上一手与吃子、本方短笔记（≤400 字，写入即替换）、`position_id`。

## 验收 1：单手工具循环

`tests/test_agent.py` 与 `tests/test_timeline.py` 覆盖：

- 查询 → 校验 → 提交：一手 3 轮请求只落一子，`moves` 表每个手数只有一行；
- `check_move`/`simulate_line` 探出非法属于正常探索，本手继续并照常提交；
- 正式提交非法被拒后才进入「纠正一次」，随后合法提交生效；
- 过期 `position_id`、重复提交被拒；
- 模型始终不提交：记录 `agent_no_submit` / `agent_rounds_exhausted` 并判该方超时，平台不代选着法；
- 轮数上限、预算窗口、按实测请求耗时自适应进入提交窗口；
- 停止后本手不再继续、迟到提交作废；
- 工具参数容错（定义写 `from`、实现叫 `source`）、非法原因讲走子规则（马走日字、路径阻挡、自将）；
- 三种线上协议（OpenAI Chat / Responses / Anthropic）的工具调用往返、事件推送与行动记录；
- 进行中的那一手在没有 `moves` 行时也能回放（token 取已记录部分，不假装是 0）。

`pytest -q`：**118 项通过**（含既有 87 项）。

## 验收 2：双方记忆与真实协议

- 记忆隔离：红方 `write_note` 后，黑方的局面消息里既没有笔记字段也没有笔记正文；红方下一手能读回自己的笔记（`test_own_note_returns_next_move_but_never_leaks_to_the_opponent`）。
- 真实模型协议（同一台网关的三条路由，均以工具调用方式落子）：

| 对局 | 协议 | 结果 | 证据 |
|---|---|---|---|
| ds-flash vs heilovehei-gpt-6 | OpenAI Chat（红）+ Responses（黑） | 红方 1 轮提交 `b2e2`；黑方网关 404 | `run/agent-real-1.txt` |
| ds-flash vs heilovehei-claude-opus-5 | OpenAI Chat + Anthropic Messages | 4 手全部一次提交（`h2e2`/`h9g7`/`h0g2`/`b9c7`），28.9 s，缓存读 2 640 token | `run/agent-real-2.txt`，对局 `b14d1d36` |
| heilovehei-gpt-5-6-sol vs ds-flash | Responses + OpenAI Chat | 4 手全部一次提交，29.3 s，缓存读 2 560 token | `run/agent-real-3.txt`，对局 `c7af78c2` |

三个真实模型在开局都选择「直接提交」，没有机械地走工具流程——与系统提示里「不要为了好看反复试走」一致。

## 验收 3：行动回放与费用

- 接口：`GET /api/games/{id}`（智能体对局多了 `timeline`）、`GET /api/games/{id}/timeline`、`GET /api/games/{id}/timeline.txt`（带下载文件名）。
- 命令行：`python -m backend.app.cli game --mode agent`、`timeline --game <ID>`。
- 网页：「智能体行动回放」按手数列出链条（例如「查询合法走法 → 校验一步 → 正式落子 a0a1」），展开看每次工具调用的输入、返回、耗时、连接与首字节耗时、Provider Request ID；每手显示请求次数、工具次数、输入/缓存读/输出 token、耗时与估算费用。
- 费用口径：每手费用 = 该手**全部**请求（含重试与失败请求）累计 token × preset 单价；未填单价时显示「未知」，不用 0 冒充。

## 验收 4：出错局面对照（同一局面、同一模型、同一时限，只切换模式）

局面取自真实对局里模型出错的那几手；每格 1 手，跑完即判「是否按时给出合法着法」。

**修复前（120 秒）**：

| 局面（模型） | direct | agent | agent 平均请求 / 工具 |
|---|---|---|---|
| 曾连续截断 + 非法 b0e0（ds-flash） | 1/2 | 0/2 | 2.5 / 3 |
| 曾超时 + 预算耗尽（gpt-5.6-sol） | 1/2 | 0/2 | 2.5 / 3 |
| 曾给出非法 e1e8（gpt-6-astra） | 2/2 | 2/2 | 1.5 / 0.5 |
| 合计 | 4/6 | 2/6 | — |

明细：`run/ab-report.md`。智能体模式确实用上了工具（平均 2–4 次调用，`check_move`/`simulate_line`/`get_legal_moves` 都出现过），但在 120 秒里被请求耗时吃掉预算：两次失败都是「一轮请求 60 秒超时 → 本手没有提交」。

**定位到并通过测试修掉的问题**（都在 `tests/` 里有对应用例）：

1. `get_legal_moves` 的 JSON Schema 写的是 `from`，实现却是 `source`：真实模型调用时直接拿到「参数不匹配」，白白浪费一轮。现在分发层做等价别名，并保留对真正错参数的报错。
2. 一次请求超时直接判本手失败：与直接模式「接口错误在时限内重试一次」不一致。现在整手保留一次「只提交」的重试，且这次请求拿到剩余的全部时间。
3. 单次请求可占整手一半（60 秒）：两次慢轮就没有提交机会。现在上限是整手的 40%（最低 30 秒）。
4. 提交窗口只按固定 20% 算：模型实际很慢时会把请求发出去再超时。现在按「实测请求耗时 × 1.2」抬高窗口门槛（`XIANGQI_AGENT_REQUEST_MARGIN`）。
5. 非法原因只说「不在 legal_moves 中」：现在区分路径阻挡、走子规则（马走日字、兵卒不能横走…）与自将/将帅照面。
6. 工具行动只在整手结束时落盘：现在逐条落盘 + 推 SSE，网页能看到行动链条实时增长。

**修复后复测（同一批局面，只改策略）**：

| 轮次 | 策略 | 时限 | 智能体模式结果 | 明细 |
|---|---|---|---|---|
| 中间版 | 自适应提交窗口 + 重试只拿部分剩余时间，单次请求上限 50% | 120 s | 0/4（全是请求超时） | `run/ab-report-post120.md` |
| 最终版 | 单次请求上限 40%（最低 30 秒）+ 重试拿剩余全部时间 + 重试门槛降到提交预留的 1/4 | 120 s | 3/4 | `run/ab-report-final120.md` |
| 最终版 | 同上 | 200 s | **4/4** | `run/ab-report-agent200.md` |

最终版 200 秒档的两个局面（各 2 次采样）：

- ds-flash（曾连续截断 + 给出非法 b0e0 的局面）：2/2 按时合法提交（`c6c5`），平均 98.4 s、2 次请求；
- gpt-5.6-sol（曾超时 + 预算耗尽）：2/2 按时合法提交（`g7e8`、`i7i3`），平均 56.3 s、2 次请求、平均 2.5 次工具调用。

一处需要说明的样本：`run/ab-report-final120.md` 里 case 1 的第 1 次运行（对局 `902fde74`）是**被我在同一时刻重启服务打断**的（服务启动会把 `queued/running` 的对局标记为 `interrupted/process_restart`），不是模型失败；该行按失败统计，实际完成的 3 次全部按时合法提交。

**顺带看到的**：智能体模式的失败几乎都是「一轮请求把时限吃满」，不是「模型拒绝落子」。这也是最终把单次请求上限从 50% 收到 40%、并让重试拿满剩余时间的原因。

## 结论与边界

- 工具循环本身可用、可回放、可审计：一手内多轮请求只落一子，正式提交是唯一落子点，模型没提交就记失败原因而不代选。
- **工具化不会自动提高按时合法提交率，预算给够才会。** 120 秒档下工具轮次挤占预算，策略不完善时智能体模式 0/4；把单次请求上限收到 40%、让超时重试拿满剩余时间后升到 3/4，200 秒档 4/4。建议智能体模式每步时限 ≥200 秒（对弈台默认 600 秒）。
- 本对照样本很小（每格 1–2 次采样），只能说明「这一版策略在这几个局面上的表现」，不能推断长期成功率或棋力；棋力差异需要另外的成对换色对局与更多采样（本轮未跑）。
- 未做：代码工作区模式（模型自写搜索/评分程序）、评分与推荐着法。
- 批量评测会继承网页或命令行选择的 `direct` / `agent` 模式和每手请求轮数。
- 协议适配层会在同一手内保留 OpenAI Chat 的 `reasoning_content`、Responses 的 `reasoning` 项和 Anthropic 的 thinking 块，供下一轮工具请求续接。

## 第二轮：外部评审 9 项的复核与修复

评审相对 `8df6ccc` 提出 9 项（实现质量 6、方案符合度 3）。逐项核对后：5 项已在工作区修好并有测试，1 项（Claude 思考强度）按当前 Command Code 方案不适用，其余按下面处理。

| 评审项 | 复核结论 | 处理 |
|---|---|---|
| 1 多轮工具调用丢推理续接字段 | 已修：`_openai_chat_messages` / `_responses_input` / `_anthropic_agent_messages` 都会回传推理字段 | 本轮补了线上级验证（严格假提供方缺字段即 400），并**补上兼容网关的第二种写法**：Command Code 上 DeepSeek 返回 `reasoning` + `reasoning_details`、GLM 返回 `reasoning_content`，现在两者都按原键原样回传 |
| 2 Claude 思考强度未透传 | 不适用：当前接入走 OpenAI 兼容协议，`reasoning_effort` 已在智能体请求中下发并验证 | 仅验证 `openai_chat` 路径，未改 Anthropic |
| 3 批量评测仍跑直接模式 | 后端 `BenchmarkCreate` 已补 `mode` / `agent_max_rounds`，网页也已传参 | 本轮用 mock 跑通「评测 2 局全部为 agent 模式」 |
| 4 未知用量被当成零 | 已修：缺失用量时 token 与费用都是「未知」；顺带删掉永远为 0 的 `billed_input_tokens` | **本轮新发现并修复**：`_ratio()` 在 `cache_read_tokens` 为空但总量已知时抛 `TypeError`，会让评测报告接口 500 |
| 5 停止/截断被导成和棋 | 已修：`DRAW_REASONS` 与 `RULE_END_REASONS` 分开，停止/截断写「未计胜负」+「记录终止状态」 | 本轮定点复验四种终局，并**补上第二处**：行动回放抬头（`timeline.txt` / CLI）原来也把无胜方一律写成「和」，现在与棋谱共用 `result_text()` 同一份说法 |
| 6 排查日志不完整 | 部分：`finish_reason` 已落盘，但每轮实际发出的参数与提供方原始回复只有汇总 | **本轮补齐**：新增 `agent_actions.request_json` / `raw_json`（含迁移），整手汇总写进 `moves.actual_request_json` 的 `rounds[]`；正文原本就没有 2000 字截断。另外修掉「接口故障只留一个异常名」：`describe_error()` 会把异常链底层原因与「方法 + 主机 + 路径」（去掉查询串）写进 `api_error` |
| 7 落子前未再查整手截止时间 | 部分：工具调用前后已有检查，正文落子路径没有 | **本轮补齐**：工具层 `RuleTools(expired=...)` 直接拒绝超时落子，正文落子前同样检查 |
| 8 整手输出预算没有真正限制 | 已修：每轮 `max_tokens = 冻结上限 − 本手已用`，用尽即以 `agent_output_limit` 收手 | 本轮定点复验 `[100, 40]` 与提前收手 |
| 9 正式提交非法可反复纠正 | 已修：第二次正式提交失败即 `invalid_move` 判负；探索类调用不计数 | 本轮定点复验 |

定点验证脚本（本地 mock + 假提供方，不调用付费模型、不依赖 pytest）：

```powershell
.\.venv\Scripts\python.exe tests\verify_review_fixes.py
# 32/32 项通过
```

线上联调（同一套假提供方跑成 HTTP 服务，走真实 API 与数据库）：

```powershell
# 终端 A：假提供方（第 2 轮缺 reasoning_content 就返回 400）
.\.venv\Scripts\python.exe run\live_fake_provider.py 8123
# 终端 B：起服务后注册连接与预设，再开一局 agent 模式
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

结果（对局 `841676156d844f05b222e56da499d800`，2 手，被 `max_plies` 截断）：两手的每轮请求都完整落盘 `finish_reason=tool_calls`、实际发出的 `model/max_tokens/temperature/reasoning_effort`、提供方原始回复；第一手两轮的 `max_tokens` 是 `4096 → 4091`，整手输出额度确实在递减；`timeline.txt` 与 `backend.app.cli timeline` 都打印「max_tokens、时限、结束原因」，抬头为「未计胜负 · 步数上限截断」而不是「和」。

真实网关（Command Code，对局 `1fb4a1ad439748c99d61bed61e943cf9`，红 `cc-deepseek-v4-1-flash` 对黑 `cc-glm-5-3`，4 手，240 秒时限）：

- 黑方第二手是完整的多轮工具循环：请求（`max_tokens=131072`）→ `check_move` → **再请求（`max_tokens=130822`）** → 落子 `b9c7`，两次请求都没有报错——说明带推理字段的助手消息被真实网关接受。
- 同一个网关给出两种推理字段写法：DeepSeek 返回 `reasoning` + `reasoning_details`，GLM 返回 `reasoning_content`；两种都按原键回传。
- 缓存用量：GLM 第二轮返回 `prompt_tokens_details.cached_tokens=1920`，归一后 `cache_read_tokens=1920`（缓存命中 86.8%）。
- 四手全部合法，无接口错误、无 400。

## 复现命令

```powershell
# 验收 1 + 3
.\.venv\Scripts\python.exe -m pytest -q

# 第二轮评审项定点验证（离线，28 项）
.\.venv\Scripts\python.exe tests\verify_review_fixes.py

# 验收 2：真实模型走工具循环
.\.venv\Scripts\python.exe -m backend.app.cli game --red ds-flash --black heilovehei-claude-opus-5 --mode agent --max-plies 4 --timeout 200
.\.venv\Scripts\python.exe -m backend.app.cli timeline --game <对局ID>

# 验收 4：出错局面对照（会产生真实 API 费用）
.\.venv\Scripts\python.exe run\ab_experiment.py --cases 1,2 --runs 2 --out run\ab-report-final120.md
```
