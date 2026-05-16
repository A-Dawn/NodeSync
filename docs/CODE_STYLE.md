# NodeSync Python 代码规范

## 基本要求

- 使用 Python 3.11+。
- 业务代码优先写类型注解，公共函数必须标注参数与返回值。
- 协议、存储和核心业务对象使用 `dataclass` 或 pydantic 模型，不在核心流程里裸传 `dict`。
- `shared/` 和 `core/` 禁止 import MaiBot 版本相关模块。
- JSON/JSONL 读写必须集中走 `nodesync.shared.jsonio`，不要散落 `open(...).write(json.dumps(...))`。
- 日志使用 `nodesync.shared.logging_utils.get_logger`，不要使用 `print`。
- 代码注释和 docstring 使用中文；协议字段、枚举值和内部标识保持 ASCII。

## 异步代码

- 异步 I/O 必须有超时、错误处理和明确返回值。
- 后台任务要有可停止路径，避免插件卸载后残留任务。
- WebSocket 请求类交互必须带 `request_id`，响应处理必须校验 `request_id` 和目标 `bot_id` 后再清理 pending future。
- client 执行 `align_context` 时必须先落盘，再执行注入。

## 安全规范

- 对外 HTTP/WebSocket 默认使用 `Authorization: Bearer <auth_token>`；除受控调试外，不开启 `allow_query_token_auth`。
- 新增 HTTP 接口、WebSocket 消息或指令执行路径时，必须显式校验鉴权、目标 bot、stream 权限和过期时间。
- 禁止把 `auth_token`、模型 API key、完整请求头或敏感配置写入日志、测试输出或文档示例结果。
- 默认 token 只能用于本机开发；当 `server_host` 暴露到非本机地址时，必须拒绝弱 token 启动。
- client 不信任 server 以外的来源；收到不属于自身 `bot_id`、不在自身 `streams` 范围内或已过期的指令必须拒绝执行。

## 命名与结构

- `shared/`：只放协议、通用工具和集中中文文案。
- `core/`：只放与 MaiBot 无关的业务逻辑。
- `server/`：只放协调服务。
- `client/`：只放通用客户端运行时。
- `adapters/`：只放 MaiBot 版本适配代码。
- 常量使用大写，枚举值使用稳定的小写字符串。

## 文案规范

- 用户可见文案、prompt 注入块、推进消息优先集中在 `nodesync.shared.texts`；可调提示词模板放入 `nodesync.shared.prompt_templates` 并通过插件目录下的 `prompts/` 文件覆盖。
- 诊断文件必须走 `nodesync.shared.diagnostics` 和 `nodesync.shared.jsonio`，不得在 adapter 中直接写原始消息对象。
- 推进文案必须强调“继续推进当前话题/场景”，避免默认换话题。
- 如果实现采用模板、规则或 best-effort 转换，必须在 `DEVELOPMENT.md` 的“补完状态与宿主验证项”里标明。

## 推荐工具

```powershell
python -m ruff check .
python -m black .
python -m mypy nodesync
python -m compileall nodesync tests
python -m unittest discover -s tests
```

首版提交前至少保证：

```powershell
python -m ruff check .
python -m compileall nodesync tests
python -m unittest discover -s tests
```

## 禁止事项

- 禁止在 `shared/` 或 `core/` 中导入 `src.plugin_system`、`maibot_sdk` 或其他 MaiBot 版本私有模块。
- 禁止让 client 自行决定主动群内发言；群内推进必须来自 server 的 `advance_dialogue` 指令。
- 禁止在未持久化 JSONL 前执行 `align_context`。
- 禁止把损坏 JSONL 行视为全文件损坏；读取时应跳过损坏行。
- 禁止在 adapter 层反向修改 core 的状态对象来绕过协议。
