# NodeSync

NodeSync 是一个面向 MaiBot 多实例协作的节点桥梁插件。它为多个 bot 提供统一的通信节点，让 bot 可以跨进程、跨版本、跨机器交换消息、上下文、LLM 能力和执行指令。

当前版本内置了一个“话题/RP 场景按需推进”能力：当多个 bot 在同一个话题里反复铺陈、缺少反馈或迟迟没有进入下一步时，NodeSync 可以先做私有上下文对齐，也可以让某个 bot 在群内自然补一条推进消息。

## 当前内置能力

### 节点通信

- client 启动后向 server 注册 bot 信息、MaiBot 版本、能力和 stream 范围。
- server 通过 WebSocket 下发请求和指令。
- client 可上报消息、返回上下文、委托 LLM、执行上下文注入、发送群内消息。
- server 使用 SQLite 保存权威状态，包括 client、消息、场景和指令。

### 上下文与对齐

- server 会主动向 client 拉取上下文，不只依赖实时消息上报。
- `align_context` 会先写入 client 本地 JSONL，再执行私有 LLM 上下文注入。
- 本地注入文件可用于重启恢复、断线后的备份参考，以及后续相似场景查询。

### 话题/RP 推进

- LLM 主判定当前消息窗口是否有真实进展。
- 默认每新增 `8` 条消息触发一次自动判定。
- 决策固定为 `no_op`、`align_context`、`advance_dialogue`。
- `advance_dialogue` 使用两段式生成：server 生成内部推进意图，client 再按目标 bot 人设渲染成最终群聊发言。
- prompt 模板放在插件目录 `prompts/` 下，用户可以直接修改。

### 本地调试

- `local_flow` 可以在不接真实群平台的情况下构造消息流。
- 本地聊天流测试会拉起真实 MaiBot adapter、真实 NodeSync server/client，并使用真实 LLM。
- `capture_outbound=true` 时，推进消息会被捕获成本地 bot 发言，便于复现和写报告。

## 适配状态

| MaiBot 版本 | 入口目录                         | 状态                                 |
| ----------- | -------------------------------- | ------------------------------------ |
| `0.12.2`  | `nodesync/adapters/maibot_012` | 已提供插件入口与配置 schema          |
| `1.0`     | `nodesync/adapters/maibot_10`  | 已提供 SDK 插件入口与 WebUI 配置适配 |

运行模式：

- `server`：内置 HTTP + WebSocket 协调服务，负责状态存储、上下文拉取、LLM 判定和指令下发。
- `client`：连接 server，上报消息，返回上下文，执行私有上下文注入和群内推进。
- `disabled`：安装后暂不启用。

## 项目结构

```text
nodesync/
  shared/                 协议模型、JSON 工具、提示词和中文文案
  core/                   配置、SQLite 存储、场景分析、注入 JSONL
  server/                 内置 HTTP + WebSocket 协调服务
  client/                 adapter 共用的 WebSocket client 运行时
  adapters/
    maibot_012/           MaiBot 0.12.2 插件入口
    maibot_10/            MaiBot 1.0 插件入口
    local_flow.py         本地构造消息流联调入口

scripts/
  install_nodesync.py          跨平台安装脚本
  run_local_chat_flow_test.py  真实 adapter + 真实 LLM 本地聊天流测试
  local_flow_driver.py         手动喂入本地消息流

docs/
  BEGINNER_DEPLOYMENT.md  新手部署说明
  USAGE.md                完整使用说明
  LOCAL_FLOW_TESTING.md   本地消息流联调
  DEVELOPMENT.md          设计、协议和验证记录
  CODE_STYLE.md           代码规范
```

## 快速安装

进入 NodeSync 项目目录：

```powershell
cd D:\Dev\plugin_dev\NodeSync
```

安装到 MaiBot 1.0：

```powershell
python .\scripts\install_nodesync.py ..\MaiBot-1.0-latest --maibot-version 1.0 --mode server
```

安装到 MaiBot 0.12.2：

```powershell
python .\scripts\install_nodesync.py ..\MaiBot-0.12.2 --maibot-version 0.12.2 --mode client --server-url http://127.0.0.1:8765
```

安装脚本会复制对应 adapter、生成基础 `config.toml`，并在插件目录内创建默认 prompt 模板。Windows、Linux、Docker 的完整流程见 [BEGINNER_DEPLOYMENT.md](docs/BEGINNER_DEPLOYMENT.md)。

## 基础配置

server 和 client 必须使用同一个强随机 `auth_token`。不要使用示例值！也不要把 token 提交到 Git！

```toml
[nodesync]
mode = "server"
bot_id = "main-bot"
bot_name = "Main Bot"
auth_token = "replace-with-a-long-random-token"
server_host = "127.0.0.1"
server_port = 8765
server_url = "http://127.0.0.1:8765"
streams = []

[policy]
enable_llm_decision = true
enable_llm_advance_generation = true
analysis_min_messages = 4
analysis_window_messages = 8
intervention_cooldown_seconds = 300
```

关键字段：

- `mode`：当前实例作为 `server`、`client` 或 `disabled` 运行。
- `bot_id`：NodeSync 内部唯一 bot ID，多 bot 部署时不能重复。
- `auth_token`：server/client 共享鉴权 token。
- `streams`：限制处理哪些群聊流；空列表表示全部。
- `analysis_min_messages`：至少累计多少条上下文消息后才允许分析。
- `analysis_window_messages`：自动流程每新增多少条消息才拉取上下文并让 LLM 判断一次。
- `intervention_cooldown_seconds`：同一场景两次介入的冷却时间。

完整配置说明见 [USAGE.md](docs/USAGE.md)。

## Prompt 文件

安装后，插件目录会包含：

```text
prompts/
  client_advance_reply.txt
  prompt_injection.txt
  scene_decision.txt
  advance_generation.txt
```

这些文件用于调整话题推进、上下文注入和群聊回复生成方式。修改后建议重启 MaiBot。默认模板也可以从 [prompt_templates.py](nodesync/shared/prompt_templates.py) 查看。

## 本地测试

安装依赖：

```powershell
python -m pip install -r requirements.txt
python -m pip install -e .
```

运行基础测试：

```powershell
python -m ruff check .
python -m unittest discover -s tests
python -m compileall nodesync tests scripts
```

真实 adapter + 真实 LLM 本地聊天流测试需要同级目录存在 MaiBot 1.0 源码、根配置 `bot_config.toml` / `model_config.toml`，并安装 `uv`：

```powershell
python .\scripts\run_local_chat_flow_test.py
```

动作堆叠停滞测试：

```powershell
python .\scripts\run_local_chat_flow_test.py --stagnation-mode action-overload
```

测试报告会写入：

```text
data/local_chat_flow_runs/<时间>/report.json
```

`data/` 和 `logs/` 是本地运行产物，默认已在 `.gitignore` 中忽略。

## 安全说明

- HTTP/WebSocket 默认使用 `Authorization: Bearer <auth_token>` 鉴权。
- `allow_query_token_auth` 默认关闭，只建议临时内网调试使用。
- server 绑定到非本机地址时，NodeSync 会拒绝弱 token。
- 不要提交 `config.toml`、`.env`、`bot_config.toml`、`model_config.toml` 或任何包含 API key/token 的文件。

## 当前限制

- LLM 主判定已接入窗口触发，但是真实群聊长期稳定性仍需要更多样本微调 `prompts/scene_decision.txt`。
- NodeSync 的协议层已经具备扩展空间，当前仓库优先实现话题/RP 场景协作能力，更多跨 bot 协作功能还需要继续设计。

## 文档入口

- [新手部署说明](docs/BEGINNER_DEPLOYMENT.md)
- [使用说明](docs/USAGE.md)
- [本地消息流联调](docs/LOCAL_FLOW_TESTING.md)
- [开发文档](docs/DEVELOPMENT.md)
- [代码规范](docs/CODE_STYLE.md)

## License

NodeSync 使用 GNU General Public License v3.0 授权。

SPDX 标识：`GPL-3.0-only`

完整许可证正文见 [LICENSE](LICENSE)。
