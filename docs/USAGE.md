# NodeSync 使用说明

本文面向安装和运行 NodeSync 的使用者。开发细节、协议和测试记录见 `docs/DEVELOPMENT.md`。

如果你没有开发经验，或只是想按步骤把插件先跑起来，建议先看 `docs/BEGINNER_DEPLOYMENT.md`。那份文档会用更少术语解释部署流程。

如果希望减少手动复制目录和生成配置的错误，可以使用 `scripts/install_nodesync.py`。脚本用法见 `docs/BEGINNER_DEPLOYMENT.md` 的“推荐方式：使用安装脚本”。

如果要在不接真实群平台的情况下，用真实 MaiBot 进程、真实 LLM 和真实 adapter 构造群聊信息流，请看 `docs/LOCAL_FLOW_TESTING.md`。

## 适用场景

NodeSync 用于让多个 MaiBot 实例在同一个群聊或 RP 场景里共享上下文、私有对齐和按需推进能力。

- `align_context`：服务端判断当前内容轻度停滞时，下发私有上下文对齐，client 在本机 LLM prompt 中注入，不直接发群消息。
- `advance_dialogue`：服务端判断当前内容明显停滞时，先下发内部推进意图；client 再用目标 bot 的回复器兼容层生成符合人设的群内发言。
- 推进不是换话题，而是沿着当前话题或 RP 场景继续往下走。

## 部署拓扑

至少需要一个实例运行 `server` 模式，其他参与 bot 可运行 `client` 模式。

`server`

启动内置 HTTP + WebSocket 协调服务，同时本机也会作为 client 连接自己。适合放在主要 bot 或常驻机器上。

`client`

不启动协调服务，只连接 `server_url`，负责上报消息、返回上下文、执行注入和发送推进消息。

`disabled`

完全禁用 NodeSync。

推荐首个部署方式：

```text
MaiBot A: NodeSync server
MaiBot B: NodeSync client
MaiBot C: NodeSync client
```

## 安装

MaiBot 父项目只扫描自己根目录下的第三方插件目录 `plugins/`：

- MaiBot 0.12.2 会扫描 `<MaiBot根目录>/plugins/*/plugin.py`。
- MaiBot 1.0 会扫描 `<MaiBot根目录>/plugins/*`，并要求目录内有 `_manifest.json` 和 `plugin.py`。

因此 NodeSync 下载后可以放在独立源码目录中，但最终 adapter 必须出现在：

```text
MaiBot 0.12.2: <MaiBot根目录>/plugins/nodesync/
MaiBot 1.0:    <MaiBot根目录>/plugins/nodesync_coordinator/
Docker:        <compose目录>/data/MaiMBot/plugins/<插件目录>/
```

不要把整个 `NodeSync/` 源码目录直接放到 MaiBot 的 `plugins/` 下；父项目不会把 `nodesync/adapters/...` 当作插件入口。

先让每个 MaiBot 运行环境都能 import `nodesync` 包。开发期推荐 editable 安装：

```powershell
cd D:\Dev\plugin_dev\NodeSync
python -m pip install -e .
```

如果 MaiBot 1.0 使用 `uv` 项目环境，也需要在该环境中安装：

```powershell
cd D:\Dev\plugin_dev\MaiBot-1.0-latest
uv pip install -e ..\NodeSync
```

然后把对应 adapter 放进 MaiBot 插件目录。

MaiBot 0.12.2：

```powershell
cd D:\Dev\plugin_dev\NodeSync
Copy-Item -Recurse -Force `
  .\nodesync\adapters\maibot_012 `
  ..\MaiBot-0.12.2\plugins\nodesync
```

MaiBot 1.0：

```powershell
cd D:\Dev\plugin_dev\NodeSync
Copy-Item -Recurse -Force `
  .\nodesync\adapters\maibot_10 `
  ..\MaiBot-1.0-latest\plugins\nodesync_coordinator
```

也可以使用软链接，但 Windows 软链接需要相应权限。

## 配置

两个版本的配置结构保持一致，核心是 `[nodesync]` 和 `[policy]`。

server 示例：

```toml
[plugin]
enabled = true

[nodesync]
mode = "server"
bot_id = "maibot-main"
bot_name = "主 MaiBot"
reply_persona = ""
auth_token = "change-this-token"
allow_query_token_auth = false
server_host = "0.0.0.0"
server_port = 8765
server_url = "http://127.0.0.1:8765"
http_client_max_bytes = 1048576
ws_max_msg_bytes = 1048576
data_dir = "data/nodesync"
streams = []

[prompts]
directory = "prompts"

[diagnostics]
enabled = false
output_dir = ""
include_hashes = true
max_depth = 4
max_items = 40

[policy]
enable_llm_decision = true
enable_llm_advance_generation = true
llm_worker_bot_id = ""
context_limit = 40
injection_history_limit = 5
intervention_cooldown_seconds = 300
directive_ttl_seconds = 900
directive_retry_interval_seconds = 20
directive_max_attempts = 3
scene_idle_close_seconds = 3600
analysis_min_messages = 4
analysis_window_messages = 8

[local_flow]
enabled = false
host = "127.0.0.1"
port = 8788
auth_token = ""
stream_id = "local-flow"
capture_outbound = true
context_limit = 80
max_messages = 500
```

client 示例：

```toml
[plugin]
enabled = true

[nodesync]
mode = "client"
bot_id = "maibot-side-a"
bot_name = "协作 MaiBot A"
reply_persona = ""
auth_token = "change-this-token"
allow_query_token_auth = false
server_host = "127.0.0.1"
server_port = 8765
server_url = "http://SERVER_IP:8765"
http_client_max_bytes = 1048576
ws_max_msg_bytes = 1048576
data_dir = "data/nodesync"
streams = []

[prompts]
directory = "prompts"

[diagnostics]
enabled = false
output_dir = ""
include_hashes = true
max_depth = 4
max_items = 40

[policy]
enable_llm_decision = true
enable_llm_advance_generation = true
llm_worker_bot_id = ""
context_limit = 40
injection_history_limit = 5
intervention_cooldown_seconds = 300
directive_ttl_seconds = 900
directive_retry_interval_seconds = 20
directive_max_attempts = 3
scene_idle_close_seconds = 3600
analysis_min_messages = 4
analysis_window_messages = 8

[local_flow]
enabled = false
host = "127.0.0.1"
port = 8788
auth_token = ""
stream_id = "local-flow"
capture_outbound = true
context_limit = 80
max_messages = 500
```

关键字段说明：

| 字段 | 说明 |
| --- | --- |
| `mode` | `server`、`client` 或 `disabled`。 |
| `bot_id` | NodeSync 内部 bot 标识，多个 bot 必须不同。 |
| `reply_persona` | 推进发言时注入给 client 回复器的人设/说话方式；留空时 adapter 会尽量读取 MaiBot 宿主人设，仍读不到则保持 bot 自身设定。 |
| `auth_token` | HTTP/WebSocket 鉴权 token，所有 server/client 必须一致；跨机器部署必须换成强随机值。 |
| `allow_query_token_auth` | 是否允许 `?token=` 鉴权；默认关闭，避免 token 进入代理和访问日志。 |
| `server_host` | server 监听地址；跨机器通常用 `0.0.0.0`。 |
| `server_port` | server 监听端口。 |
| `server_url` | client 连接地址；跨机器使用 server 的局域网或公网地址。 |
| `http_client_max_bytes` | HTTP 请求体最大字节数。 |
| `ws_max_msg_bytes` | WebSocket 单消息最大字节数。 |
| `data_dir` | SQLite、注入 JSONL 和指令结果 JSONL 的本地目录。 |
| `streams` | 限定处理的 stream_id 列表；空列表表示全部。 |
| `[prompts].directory` | 用户可编辑 prompt 模板目录；默认 `prompts`，相对路径按插件目录解析。 |
| `enable_llm_decision` | 是否让 LLM 主判定当前窗口是否停滞；关闭后只用规则兜底判断。 |
| `enable_llm_advance_generation` | 是否让 LLM 生成 server 侧推进意图；最终群聊文本仍由 client 侧回复器兼容层生成。 |
| `llm_worker_bot_id` | 指定负责 LLM 委托的 bot；留空时自动选第一个具备 LLM 能力的在线 client。 |
| `intervention_cooldown_seconds` | 同一场景两次介入之间的冷却时间。 |
| `analysis_min_messages` | 至少多少条上下文消息才允许分析。 |
| `analysis_window_messages` | 自动分析窗口消息数；每新增这么多条消息，server 才主动拉取上下文并让 LLM 判断一次。 |

`enable_llm_advance_generation` 当前生成的是 server 侧内部推进意图，不是最终群聊文本。最终群聊文本由 target client 的 `render_advance_message` 回复器兼容层生成，并会带入 `reply_persona` 或 MaiBot 宿主人设；这样可以避免“协调器/当前场景/角色们”等内部说明直接发进群，也能让推进发言更贴近该 bot 的角色。

## 提示词文件

NodeSync 启动时会在 `[prompts].directory` 里自动生成缺失的默认模板。默认 `directory = "prompts"`，相对路径会按插件目录解析。
安装脚本也会在复制插件时预置这一目录，方便用户安装后立刻修改。

```text
MaiBot 0.12.2: <MaiBot根目录>/plugins/nodesync/prompts/
MaiBot 1.0:    <MaiBot根目录>/plugins/nodesync_coordinator/prompts/
```

当前模板文件：

- `client_advance_reply.txt`：client 把内部推进意图改写成最终群聊发言。
- `prompt_injection.txt`：`align_context` 私有上下文注入块。
- `scene_decision.txt`：server 让 LLM 主判定当前窗口是否需要介入。
- `advance_generation.txt`：server 让 LLM 生成内部推进意图。

模板变量使用 `{{变量名}}`。你可以直接改这些 `.txt` 文件；已存在的模板不会被 NodeSync 覆盖。修改后建议重启 MaiBot。

## 受控诊断

默认不开启诊断。需要排查真实平台消息字段时，可以临时打开：

```toml
[diagnostics]
enabled = true
output_dir = ""
include_hashes = true
max_depth = 4
max_items = 40
```

`output_dir = ""` 时，诊断写入：

```text
data/nodesync/diagnostics/message_schema.jsonl
```

诊断文件只记录字段名、类型、长度和可选 hash，不写入群聊原文、prompt 原文、token、cookie 或 API key。排查完成后建议改回 `enabled = false`。

`local_flow` 是默认关闭的本地联调入口。启用后，可以用 `scripts/local_flow_driver.py` 在不接真实平台的情况下构造消息流；`capture_outbound=true` 会捕获推进消息而不发往真实平台。这个模式不验证真实平台外发链路，详见 `docs/LOCAL_FLOW_TESTING.md`。

## 启动与验证

1. 先启动 `server` 模式 bot。
2. 再启动所有 `client` 模式 bot。
3. 在 server 机器检查健康状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

4. 查看在线 bot：

```powershell
Invoke-RestMethod `
  -Headers @{ Authorization = "Bearer change-this-token" } `
  http://127.0.0.1:8765/bots
```

5. 手动触发某个群聊流分析：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Headers @{ Authorization = "Bearer change-this-token" } `
  http://127.0.0.1:8765/streams/YOUR_STREAM_ID/analyze
```

正常情况下，群聊消息进入 MaiBot 后会由插件自动上报并触发分析；手动接口主要用于调试。

## 安全建议

- 不要使用示例 token，例如 `change-me`、`change-this-token`、`test`。
- 跨机器部署时，`auth_token` 至少使用 32 字节随机值，并通过私密渠道分发给 client。
- `server_host = "0.0.0.0"` 会让服务监听所有网卡；插件会拒绝用弱 token 在非本机地址启动。
- 默认只允许 `Authorization: Bearer <auth_token>`；除非只在受控内网调试，否则不要开启 `allow_query_token_auth`。
- `/bots`、`/streams/{stream_id}/analyze`、`/sessions/{session_id}/close` 和 `/ws` 都需要鉴权。
- `/health` 未带鉴权时只返回最小健康信息；带鉴权时才返回模式和在线 client 数。
- 如果跨公网使用，建议放在 VPN、内网隧道或带 TLS 的反向代理后面。NodeSync 本身不负责 TLS 终止。
- 多个 bot 的 `bot_id` 必须唯一。重复 `bot_id` 会踢掉旧连接，避免同一身份被并行占用。
- client 会拒绝目标 `target_bot_id` 不是自己的指令，也会拒绝不在 `streams` 白名单中的指令和上下文请求。

## 运行期数据

client 本地注入历史：

```text
data/nodesync/context_injections.jsonl
```

client 本地指令执行结果：

```text
data/nodesync/directive_results.jsonl
```

server 权威状态：

```text
data/nodesync/nodesync.sqlite3
```

`align_context` 会先写入 `context_injections.jsonl`，再执行 prompt 注入。重启或断线后，client 可读取未过期注入作为恢复参考。

## 推荐调参

测试阶段可以降低门槛：

```toml
[policy]
intervention_cooldown_seconds = 0
analysis_min_messages = 4
analysis_window_messages = 4
enable_llm_decision = true
enable_llm_advance_generation = true
```

正式群聊建议保守一些：

```toml
[policy]
intervention_cooldown_seconds = 300
analysis_min_messages = 6
analysis_window_messages = 8
enable_llm_decision = true
enable_llm_advance_generation = true
```

如果担心模型费用，可以增大窗口，或临时关闭 LLM 判定和生成：

```toml
[policy]
analysis_window_messages = 16
enable_llm_decision = false
enable_llm_advance_generation = false
```

## 故障排查

`/bots` 看不到 client：

- 检查 server 是否先启动。
- 检查 client 的 `server_url` 是否能访问。
- 检查 `auth_token` 是否一致。
- 检查防火墙是否放行 `server_port`。

能看到 client，但没有推进：

- 群聊消息可能还不足 `analysis_min_messages`，或新增消息还没达到 `analysis_window_messages`。
- 当前场景可能被判断为有进展，因此输出 `no_op`。
- 仍在 `intervention_cooldown_seconds` 冷却期。
- client 的 `streams` 可能不包含当前 `stream_id`。

LLM 不工作：

- 检查 MaiBot 自身模型配置是否可用。
- 检查至少一个在线 client 具备 LLM 能力。
- 如需指定 LLM worker，设置 `llm_worker_bot_id` 为对应 `bot_id`。
- 可先关闭 `enable_llm_decision`，只验证基础通信，此时会回退规则判断。

重复发言：

- 检查 `directive_results.jsonl` 是否可写。
- 检查多个 bot 的 `bot_id` 是否重复。
- 检查 server 是否被多开且 client 连接到了不同 server。

真实平台字段不对：

- 临时开启 `[diagnostics].enabled = true`，复现一条消息。
- 查看 `data/nodesync/diagnostics/message_schema.jsonl`，确认真实消息对象里 stream、sender、text、message_id 对应字段。
- 诊断只记录脱敏结构；排查完及时关闭诊断。

## 当前限制

- 已验证到 MaiBot 1.0 `ctx.send.text` 适配层，但真实平台外发端到端仍需在实际机器人平台中验证。
- LLM 主判定已接入窗口触发；仍建议在真实群聊中继续积累样本，微调 `scene_decision.txt`。
- 已验证同机跨进程通信；跨机器部署需要额外验证网络、防火墙、反向代理和 token 配置。
- 已支持脱敏诊断输出，但真实平台字段 fixture 仍需要在实际平台采样后固化为单测。
- MaiBot 1.0 当前 SDK 未暴露官方 `HookHandler` 装饰器，NodeSync 使用本地兼容装饰器声明宿主已支持的 `hook_handler` 元数据。
