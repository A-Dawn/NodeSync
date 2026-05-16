# NodeSync 开发文档

NodeSync 是一个面向 MaiBot 多实例协作的插件雏形，目标是让多个 bot 在同一个群聊话题或 RP 场景里获得共享反馈、私有对齐和按需推进能力，避免所有 bot 都停在同一层重复回应。

## 项目目标

- 支持 MaiBot `0.12.2` 与 MaiBot `1.0` 两套插件系统。
- 每个实例可配置为 `server`、`client` 或 `disabled`。
- `server` 负责收集消息、主动拉取上下文、识别当前群聊话题/RP 场景、判断是否停滞，并下发指令。
- `client` 负责上报消息、返回上下文、委托 LLM、执行私有上下文注入和群内推进消息；推进消息生成时会注入 `reply_persona` 或 MaiBot 宿主人设。
- 推进是“沿着当前话题/场景继续向下一步走”，不是强行换话题。
- `align_context` 必须先写入本地 JSONL 文件，再执行 LLM prompt 注入。
- server 使用“可配置消息窗口 + LLM 主判定”的决策链路；LLM 不可用时自动回退到规则基线。
- prompt 模板支持文件化覆盖；adapter 默认写入插件目录下的 `prompts/*.txt`，用户可在不改代码的情况下调整提示词。
- 受控诊断默认关闭；开启后只写入脱敏消息字段结构，用于对照真实平台事件字段。

## 目录结构

```text
NodeSync/
  nodesync/
    shared/              # 协议模型、JSON/时间/日志、集中中文文案
    core/                # 配置、SQLite 存储、JSONL 注入存储、场景识别
    server/              # aiohttp HTTP + WebSocket 协调服务
    client/              # WebSocket client 运行时与 MaiBot bridge 协议
    adapters/
      maibot_012/        # MaiBot 0.12.2 插件入口与 manifest
      maibot_10/         # MaiBot 1.0 SDK 插件入口与 manifest
  docs/
    USAGE.md
    DEVELOPMENT.md
    CODE_STYLE.md
  tests/
```

`shared/` 和 `core/` 不 import MaiBot 内部模块。所有版本差异都在 `adapters/` 内部处理。

面向安装和运行的步骤见 `docs/USAGE.md`；本文只保留设计、开发和验证记录。

本地构造消息流联调见 `docs/LOCAL_FLOW_TESTING.md`。该模式使用真实 MaiBot 进程、真实 LLM 和真实 NodeSync adapter，只把真实平台事件替换为本机 HTTP 构造消息入口。

## 运行模式

`server`

启动内置 aiohttp 服务，同时启动本机 client 连接本地 server。这样安装 server 模式插件的 bot 本身也能参与协作，并可服务远端 client。

`client`

不启动内置服务，只连接配置中的 `server_url`，注册能力并执行服务端下发的指令。

可通过 `streams` 配置限定 client 只处理指定 `stream_id`；空列表表示通配全部 stream。

`disabled`

不启动任何 NodeSync 运行时。

`local_flow`

这是 adapter 旁路的本地联调入口，不属于正式群平台链路。开启后，插件会启动一个仅建议绑定 `127.0.0.1` 的 HTTP 服务，测试脚本可通过 `POST /messages` 把构造消息交给真实 adapter。`capture_outbound=true` 时，`advance_dialogue` 不调用真实平台外发接口，而是把推进消息捕获成本地 bot 发言并重新进入 NodeSync 流程。这个简化点必须在测试结论中单独标明：它验证 NodeSync 决策、LLM 生成和 adapter 执行，但不验证真实消息平台发送。

## 双版本适配边界

| 能力 | MaiBot 0.12.2 | MaiBot 1.0 |
| --- | --- | --- |
| 消息采集 | `ON_MESSAGE` EventHandler | `EventHandler(EventType.ON_MESSAGE)` |
| 私有注入 | `POST_LLM` 修改 `MaiMessages.llm_prompt` | 主路径：`hook_handler` 订阅 `maisaka.planner.before_request` 并改写 `messages`；兼容后备：`POST_LLM EventHandler` 修改 prompt |
| 群内发送 | `send_api.text_to_stream` | `self.ctx.send.text` |
| 上下文读取 | `message_api.get_recent_messages` | `self.ctx.message.get_recent` |
| LLM 委托 | `llm_api.generate_with_model` | `self.ctx.llm.generate` |

MaiBot 1.0 当前宿主注册表暴露的组件类型是 `action`、`command`、`tool`、`event_handler`、`hook_handler` 和 `message_gateway`，没有 `workflow_step`。因此 NodeSync 不再声明 `WorkflowStep(PLAN)`；1.0 私有注入通过一个轻量本地 `HookHandler` 兼容装饰器写入 SDK 的 `__maibot_component_info__`，让 `maibot_sdk.collect_components()` 输出宿主可注册的 `hook_handler` 组件。该装饰器只声明宿主已经支持的元数据，不绕过宿主的 Hook 规格校验。

`maisaka.planner.before_request` 的输入包括 `messages`、`tool_definitions`、`selected_history_count`、`built_message_count`、`selection_reason` 和 `session_id`。NodeSync 在存在有效 `align_context` 注入记录时，会在 `messages` 前插入一条私有 system 消息；返回的 `modified_kwargs` 会保留原业务参数，并更新 `built_message_count`。

## WebSocket 协议

所有 WebSocket 消息使用统一 Envelope：

```json
{
  "type": "context.request",
  "request_id": "req-id",
  "payload": {}
}
```

当前消息类型：

- `client.register`：client 注册 bot 身份、MaiBot 版本、能力列表和可处理 stream。
- `client.heartbeat`：连接保活。
- `chat.event`：client 上报群聊消息。
- `context.request` / `context.response`：server 主动向 client 拉取上下文与注入历史。
- `llm.request` / `llm.response`：server 可委托具备 LLM 能力的 client 生成内容。
- `directive.push`：server 下发 `align_context` 或 `advance_dialogue`。
- `directive.ack`：client 收到指令后的确认。
- `directive.result`：client 完成或失败后的执行结果。

## HTTP 接口

- `GET /health`：服务健康检查。
- `GET /bots`：查看已注册和在线 bot。
- `POST /streams/{stream_id}/analyze`：手动触发某个 stream 的分析。
- `POST /sessions/{session_id}/close`：手动关闭场景。

`/health` 未带鉴权时只返回最小健康信息；带 `Authorization: Bearer <auth_token>` 时才返回运行模式和在线 client 数。

`/bots`、`/streams/{stream_id}/analyze`、`/sessions/{session_id}/close` 和 `/ws` 都要求鉴权。默认只接受 `Authorization: Bearer <auth_token>`；`?token=<auth_token>` 默认关闭，只能通过 `allow_query_token_auth=true` 在受控调试环境中显式开启。

当 `server_host` 暴露到非本机地址时，server 会拒绝使用空值、`change-me`、`change-this-token` 等弱 token 启动。公网或跨网段部署仍建议放在 VPN 或 TLS 反向代理后；NodeSync 内置服务暂不终止 TLS。

## 场景识别与推进流程

1. client 在 `ON_MESSAGE` 收到消息后发送 `chat.event`。
2. server 写入 SQLite，并异步触发 `analyze_stream(force=false)`。
3. 自动分析先检查 `analysis_min_messages` 与 `analysis_window_messages`；只有新增消息达到窗口长度时才拉取上下文。
4. server 读取本地 SQLite 最近消息，再向所有具备 `context_fetch` 能力的 client 发送 `context.request`。
5. server 合并上下文，创建或更新 `SceneState`。
6. `SceneAnalyzer` 只提供讨论/RP、参与者、未解决问题、事实片段、重复循环和规则兜底参考。
7. server 构造 LLM 决策 prompt，要求 LLM 作为主判定者只返回 JSON；若解析失败或超时，回退规则基线。
8. 决策输出固定为 `no_op`、`align_context`、`advance_dialogue`。
9. LLM 可标记 `completed=true`，server 自动关闭当前场景。
10. 有进展时 `no_op`；轻度停滞优先 `align_context`；明显原地打转或对齐后仍无进展时 `advance_dialogue`。
11. `advance_dialogue` 的 server 侧输出是“内部推进意图”，不是群内最终发言。
12. target client 收到 `advance_dialogue` 后，先调用本机回复器兼容层，把推进意图改写成符合该 bot 人设和上下文的最终群聊发言，再执行发送。人设来源优先级为：插件配置 `reply_persona`、MaiBot 宿主配置中的 personality、保持 bot 自身设定。
13. 如果 client 侧渲染失败，会使用安全的第一人称保底发言；禁止把“协调器、当前场景、角色们、推进意图”等内部元叙事文本直接发进群。
14. server 按能力选择 target bot，写入 SQLite 权威状态，再发送 `directive.push`。

## 自动场景关闭

server 会在分析前检查活跃场景是否应关闭：

- 最近消息包含“结束、完结、收尾、就到这里、end scene”等收场信号：关闭场景并不再介入。
- 最近消息包含“新话题、换个话题、开新场景、new topic、new scene”等信号：关闭旧场景，并把后续消息作为新场景分析。
- 活跃场景超过 `scene_idle_close_seconds` 未更新：关闭旧场景。
- LLM 决策 JSON 返回 `completed=true`：关闭场景。

## 上下文注入持久化

client 本地注入文件：

```text
data/nodesync/context_injections.jsonl
```

每行是一个独立 JSON 对象，字段包括：

- `injection_id`
- `session_id`
- `stream_id`
- `target_bot_id`
- `directive_type`
- `content`
- `source_scene_snapshot`
- `created_at`
- `expires_at`
- `applied_at`
- `status`: `pending | applied | expired | superseded`

`align_context` 的执行顺序固定为：先追加 `pending` 记录，再调用 adapter 执行 prompt 注入，成功后追加 `applied` 记录。读取时以后写入的同 `injection_id` 记录为准；损坏单行会被跳过，不影响其他行。

server 端 SQLite 是权威状态源，client JSONL 用于本机恢复、断线备份和后续相似场景参考。

## Prompt 文件化

安装脚本会在插件目录内预置 prompt 模板；运行时也会调用 `configure_prompt_templates(config.prompts_dir)`，在目录缺失时写入默认模板。MaiBot adapter 会把 `[prompts].directory` 解析到当前插件入口目录；默认值 `prompts` 对应：

```text
MaiBot 0.12.2: <MaiBot根目录>/plugins/nodesync/prompts/
MaiBot 1.0:    <MaiBot根目录>/plugins/nodesync_coordinator/prompts/
```

当前文件：

- `client_advance_reply.txt`：client 侧回复器兼容层，把内部推进意图改写为最终群聊发言。
- `prompt_injection.txt`：私有上下文对齐注入块。
- `scene_decision.txt`：server 侧 LLM 主判定 prompt。
- `advance_generation.txt`：server 侧内部推进意图生成 prompt。

模板变量采用 `{{name}}` 占位。已存在模板不会被覆盖；如果模板读取失败，会回退内置默认模板并记录 warning。

MaiBot 1.0 WebUI 配置中，提示词相关配置独立为“提示词”分组，诊断相关配置独立为“诊断”分组；字段使用 `x-widget`、`x-icon` 和 `advanced` 元数据适配前端展示。0.12.2 通过 `config_schema` 暴露同名 `[prompts]` 与 `[diagnostics]` 配置节。

## 受控诊断

配置项位于 `[diagnostics]`，默认 `enabled=false`。开启后 adapter 会在真实消息入口和上下文读取路径记录脱敏字段结构：

```text
data/nodesync/diagnostics/message_schema.jsonl
```

诊断记录包含来源、原始对象类型、`flatten/to_dict/model_dump/to_rpc_dict` 等可用结构，以及转换后的 `ChatMessageEvent` 摘要。文本、prompt、content、token、cookie、API key 等值不会落盘；字符串只记录长度和可选 `sha256_12`，用于判断同一字段是否稳定映射。

## 指令幂等与重试

server 为每条指令生成 `idempotency_key`，同一场景、决策类型、目标 bot 和重复模式下不会反复创建新指令。未完成指令由后台重试队列处理：

- 重试状态：`queued | offline | send_failed | pushed | acked | failed`
- 终态：`applied | expired | exhausted | cancelled`
- 重试间隔：`directive_retry_interval_seconds`
- 最大尝试次数：`directive_max_attempts`

每次 `_push_directive` 会增加 `attempts`，并把最新尝试次数发给 client。client 本地维护：

```text
data/nodesync/directive_results.jsonl
```

收到重复 `directive_id` 时，client 会直接返回最近一次执行结果，不会重复发送群内推进消息。

## 本地开发与测试

安装依赖：

```powershell
cd D:\Dev\plugin_dev\NodeSync
python -m pip install -e .
python -m pip install aiohttp ruff black mypy
```

检查：

```powershell
python -m ruff check .
python -m compileall nodesync tests
python -m unittest discover -s tests
```

真实 MaiBot 宿主烟测默认不随普通单测执行，需要显式开启：

```powershell
$env:NODESYNC_REAL_MAIBOT_TESTS = "1"
python -m unittest tests.test_real_maibot_hosts
```

真实双 Bot 通信烟测也默认不随普通单测执行，需要显式开启：

```powershell
$env:NODESYNC_REAL_TWO_BOT_TESTS = "1"
python -m unittest tests.test_real_two_bot_communication
```

真实模型集成测试会消耗配置中的模型额度，也默认不随普通单测执行，需要显式开启：

```powershell
$env:NODESYNC_REAL_MODEL_TESTS = "1"
python -m unittest tests.test_real_model_integration
```

该测试依赖工作区存在：

- `D:\Dev\plugin_dev\MaiBot-0.12.2`
- `D:\Dev\plugin_dev\MaiBot-1.0-latest`
- `uv`，用于按 MaiBot 1.0 项目依赖环境运行 `PluginLoader`
- `D:\Dev\plugin_dev\bot_config.toml`
- `D:\Dev\plugin_dev\model_config.toml`

双 Bot 通信烟测会校验根目录配置文件存在，但不会读取、打印或改写其中的模型密钥。该测试为了隔离通信链路，会关闭真实 LLM 判定/生成，并用 1.0 SDK 能力测试桩提供上下文与发送能力；它验证的是两个真实宿主插件实例之间的 NodeSync 协议通信，不等同于完整 MaiBot 主进程接入真实平台。

真实模型集成测试会把根目录 `bot_config.toml` 和 `model_config.toml` 复制到临时目录，再让 MaiBot 1.0 `llm_service` 从临时复制文件加载配置；配置版本自动升级也只会写入临时目录。测试不会输出或改写工作区根配置中的密钥。当前本地聊天流脚本默认开启 LLM 主判定和 LLM 推进生成；双 Bot 通信烟测仍可关闭 LLM 来隔离协议链路。

部署到 MaiBot 时，需要让 MaiBot 进程能 import `nodesync` 包。推荐开发期使用 `pip install -e D:\Dev\plugin_dev\NodeSync`，再把对应 adapter 目录复制或软链接到 MaiBot 插件目录：

- 0.12.2：`nodesync/adapters/maibot_012`
- 1.0：`nodesync/adapters/maibot_10`

完整使用说明、配置样例、启动验证和故障排查见 `docs/USAGE.md`。

## 补完状态与宿主验证项

以下是已经从首版简化状态补完的内容：

- LLM 主判定链路已接入：server 按 `analysis_window_messages` 聚合新消息窗口，再通过 `llm.request` 请求 client 判断是否停滞；规则结果只作为兜底参考。
- `advance_dialogue` 已改为两段式：server 生成内部推进意图，client 侧再用目标 bot 的回复器兼容层生成最终群内发言；失败时回退安全的第一人称发言。
- `advance_dialogue` 的 client 侧渲染已支持人设注入：配置 `reply_persona` 时显式使用该人设；留空时 adapter 会尽量读取 MaiBot 宿主 personality。
- prompt 模板已文件化：`client_advance_reply`、`prompt_injection`、`scene_decision` 和 `advance_generation` 均可由插件目录 `prompts/` 中的 `.txt` 文件覆盖。
- 受控诊断已实现：开启 `[diagnostics].enabled` 后会写入脱敏消息字段结构，默认关闭，不记录原文和密钥。
- 自动场景完成识别已实现：支持收场关键词、新场景关键词、空闲超时和 LLM `completed=true`。
- 指令幂等与重试队列已实现：server 使用 `idempotency_key` 去重，后台重试未完成指令；client 使用本地结果 JSONL 防重复执行。
- adapter 已支持配置化 `streams`；空列表仍表示通配全部 stream。
- adapter 的消息字段转换已覆盖 MaiBot 0.12.2 `DatabaseMessages.flatten()` 关键字段、1.0 dict/pydantic 消息和常见对象属性。
- adapter 已支持 `local_flow` 本地构造消息流入口，可在不接真实平台时用真实 bot 进程和真实 LLM 做交互式联调。
- `scripts/run_local_chat_flow_test.py` 已支持自动拉起两个 MaiBot 1.0 真实 adapter 实例，配置双 bot 人设，维护本地聊天流，调用真实 LLM 生成自然回复，并汇总 NodeSync 状态报告。

以下内容已通过真实 MaiBot 宿主单实例烟测：

- MaiBot 0.12.2：使用真实 `BasePlugin`、`BaseEventHandler`、`EventType`、`MaiMessages` 加载 adapter；验证 ON_START 启动 server/client、本机 client 注册、ON_MESSAGE 上报写入 server SQLite、POST_LLM 私有 prompt 注入、ON_STOP 停止运行时。
- MaiBot 1.0：使用真实 `PluginLoader` 加载 adapter；使用真实 `ComponentRegistry` 与 Maisaka `maisaka.planner.before_request` hook spec 注册组件；验证 `hook_handler` 注册、server/client 启动、本机 client 注册、ON_MESSAGE 上报写入 server SQLite、before_request 私有 system message 注入、`self.ctx.send.text` 发送适配。
- MaiBot 1.0 SDK 当前未暴露官方 `HookHandler` 装饰器；NodeSync 仍使用本地兼容装饰器写入 SDK 组件元数据。真实宿主注册表已经验证可接受该 `hook_handler` 声明。

以下内容已通过真实 MaiBot 双实例跨进程通信烟测：

- MaiBot 0.12.2 adapter 以 `server` 模式启动 NodeSync 协调服务，MaiBot 1.0 adapter 以 `client` 模式连接同一服务。
- `client.register` 注册后，server 的 `/bots` 可看到 1.0 client 在线。
- server 主动向 1.0 client 下发 `context.request`，client 通过 SDK message capability 返回指定 stream 的上下文。
- 普通停滞流触发 `align_context`，1.0 client 返回 `directive.ack` 与 `directive.result`，并在本地 `context_injections.jsonl` 写入 `applied` 记录。
- 1.0 `maisaka.planner.before_request` hook 可读取跨进程下发的注入，并插入 `NodeSync 私有上下文对齐` system message。
- 高重复停滞流触发 `advance_dialogue`，1.0 client 先把内部推进意图渲染成人设内发言，再进入 `ctx.send.text` 适配层；server SQLite 记录指令状态为 `applied`。
- 1.0 client 通过 `ON_MESSAGE` 上报的 `chat.event` 可写入 0.12.2 server 侧 SQLite。

以下内容已通过真实模型集成测试：

- MaiBot 1.0 `llm_service.generate()` 可使用工作区根目录模型配置的临时复制文件完成真实模型调用。
- NodeSync 0.12.2 server 在 `advance_dialogue` 阶段可通过 WebSocket `llm.request` 委托 1.0 client 调用真实模型生成内部推进意图。
- 真实模型生成的推进意图可回到 server，随后通过 `directive.push` 下发给 1.0 client；client 再调用本机模型渲染成人设内最终发言，并进入 `ctx.send.text` 适配层。
- 已验证本次真实模型响应成功返回模型名、响应摘要和 token 统计；日志中不记录 API key。

以下内容仍未覆盖：

- 多个 MaiBot 实例之间的跨机器通信。
- 暴露到公网后的 TLS、反向代理、访问日志脱敏和防火墙策略验证；NodeSync 内置服务当前只负责应用层 token 鉴权。
- 完整 MaiBot 主进程同时接入真实平台后由平台事件触发 NodeSync 的端到端通信。
- 真实消息平台事件中的完整字段 fixture 对照；当前已提供受控诊断输出脱敏字段结构，但仍需要在真实平台采样后固化为测试 fixture。
- 真实群消息外发到平台链路；当前测试仍使用 1.0 `ctx.send.text` 测试桩截获发送内容，未连接真实平台。
- `local_flow.capture_outbound=true` 的本地联调不会验证真实平台外发，只会捕获推进消息并写回本地构造信息流。
- 目前 MaiBot SDK 尚未提供统一的“直接调用宿主回复器生成最终发言”接口；NodeSync 首版在 adapter 层用本机 LLM 能力实现回复器兼容层。后续若宿主暴露正式 replyer，可替换 `render_advance_message`。
- `enable_llm_decision=True` 的真实群聊长期稳定性仍需继续采样；可通过编辑 `prompts/scene_decision.txt` 微调判定口径。

## 当前验证状态

已通过：

- `python -m ruff check .`
- `python -m compileall nodesync tests scripts`
- `python -m unittest discover -s tests`

测试覆盖协议模型、注入 JSONL 损坏行容错、过期注入、场景 no-op/alignment/advance/完成识别、LLM 决策解析、指令重试状态、指令结果幂等缓存和 adapter 字段转换。

新增本地信息流测试覆盖：

- `local_flow` HTTP 输入口鉴权、构造消息上报、历史查询。
- `capture_outbound` 把 `advance_dialogue` 捕获为本地 bot 发言并重新进入消息流。
- 真实宿主上下文和本地构造上下文按 `message_id` 去重、按时间排序。
- 双 bot 本地聊天流实测脚本可验证：两个真实 adapter 在线注册、真实模型自然回复、场景识别、重复短消息触发 `advance_dialogue`、指令 `applied`，并生成 `data/local_chat_flow_runs/<时间>/report.json`。
- `advance_dialogue` 渲染测试覆盖：client 会先把内部推进意图交给 `render_advance_message`，再发送渲染后的最终群聊文本；内部协调文案会被清理为安全保底发言。
- 本地聊天流真实模型回归已覆盖人设渲染：`reply_persona` 注入后，`local-flow-out-*` 捕获到的是澪人设下的第一人称推进发言，不是内部推进意图或协调器文本。

新增运行时集成测试覆盖：

- 内置 HTTP `/health` 最小公开响应、`/bots` 强制鉴权、`/streams/{stream_id}/analyze` 强制鉴权和 `?token=` 默认禁用行为。
- WebSocket client 注册后写入在线 bot 与 SQLite client 记录。
- WebSocket 未注册消息拒绝、重复 `bot_id` 连接替换、未知 capability 过滤和 stream 权限校验。
- server 主动发送 `context.request`，client 返回 `context.response`。
- `context.response` 与 `llm.response` 必须来自 server 实际请求的 bot，异常来源不会消费 pending request。
- HTTP 手动分析触发 `align_context` 指令，client 返回 `directive.ack` 与 `directive.result`，server 记录为 `applied`。
- `directive.ack/result` 只接受目标 bot 回报；client 会拒绝不属于自己的目标、stream 或已过期指令。
- `align_context` 在调用适配层注入前已经写入本地 JSONL 的 `pending` 记录，成功后可读取 `applied` 活跃注入。
- `context.response` 会携带同 `stream_id/session_id` 的注入历史。
- 重复 `directive.push` 会复用本地 `directive_results.jsonl`，不会重复发送群内推进消息。

真实 MaiBot 宿主烟测已通过：

- `NODESYNC_REAL_MAIBOT_TESTS=1 python -m unittest tests.test_real_maibot_hosts`
- MaiBot 0.12.2：插件可用真实插件基类启动，基础消息上报与 prompt 注入正常。
- MaiBot 1.0：插件可用真实 `PluginLoader` 加载，`hook_handler` 可注册到真实 `ComponentRegistry`，基础消息上报、before_request 注入和发送适配正常。

真实 MaiBot 双实例通信烟测已通过：

- `NODESYNC_REAL_TWO_BOT_TESTS=1 python -m unittest tests.test_real_two_bot_communication`
- MaiBot 0.12.2 进程以 server 模式启动，MaiBot 1.0 进程以 client 模式连接。
- 已验证跨进程 WebSocket 注册、主动上下文拉取、`align_context` 指令 ACK/RESULT、注入 JSONL 落盘、1.0 before_request hook 注入、`advance_dialogue` 推进发送适配和 client 消息上报写库。

真实模型集成测试已通过：

- `NODESYNC_REAL_MODEL_TESTS=1 python -m unittest tests.test_real_model_integration`
- 直接探针验证：MaiBot 1.0 `llm_service.generate(task_name="utils")` 使用根目录模型配置临时复制文件返回 `NODESYNC_REAL_MODEL_OK`。
- 双 Bot 链路验证：0.12.2 server 通过 `llm.request` 委托 1.0 client 使用真实模型生成 `advance_dialogue` 内部推进意图，随后下发给 1.0 client 渲染为最终发言并进入 `ctx.send.text` 适配层，最终记录指令 `applied`。
