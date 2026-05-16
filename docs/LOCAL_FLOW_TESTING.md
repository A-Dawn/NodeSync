# 本地信息流联调

这个模式用于你刚才描述的场景：不接真实群平台，但 MaiBot 进程、模型配置、LLM 调用和 NodeSync adapter 都用真实的。唯一替换掉的是“群聊消息从哪里来”：消息由本地脚本构造，再通过 adapter 的本机入口进入 NodeSync。

## 它能验证什么

- 真实 MaiBot 进程能加载 NodeSync 插件。
- 真实 NodeSync adapter 能启动 server/client、上报消息、拉取上下文、接收指令。
- 真实 LLM 能参与 `advance_dialogue` 两段式推进：server 生成推进意图，target client 再渲染成人设内发言。
- 构造出来的信息流能触发 `align_context` 或 `advance_dialogue`。
- 推进消息能回到本地信息流，方便继续交互测试。

## 明确的简化点

为了不接真实平台，`local_flow.capture_outbound=true` 时，`advance_dialogue` 不会调用真实平台外发接口，而是把推进消息捕获成本地 bot 发言，再重新喂回 NodeSync 信息流。

这意味着本模式不验证 QQ/消息平台的真实发送链路；真实平台外发仍需要单独接平台测试。

`advance_dialogue` 在本模式中也会走两段式：server 的指令内容是内部推进意图，目标 bot 的 adapter 会先带入 `reply_persona` 或宿主人设，调用回复器兼容层生成最终群内发言，再由 `capture_outbound` 捕获成本地 bot 发言。这样可以避免“协调器/当前场景/角色们”这类内部说明混进模拟群聊，也能观察推进发言是否还贴合角色。

## 配置插件

在每个要参与本地联调的 bot 插件配置中加入或打开：

```toml
[local_flow]
enabled = true
host = "127.0.0.1"
port = 8788
auth_token = ""
stream_id = "local-flow"
capture_outbound = true
context_limit = 80
max_messages = 500
```

`auth_token` 留空时会复用 `[nodesync].auth_token`。如果一台机器上同时启动多个 bot，每个 bot 的 `local_flow.port` 必须不同，例如 server 用 `8788`，client 用 `8789`。

## 推荐启动方式

1. 启动 server 模式 bot。
2. 启动 client 模式 bot。
3. 确认 NodeSync server 能看到 client：

```powershell
Invoke-RestMethod `
  -Headers @{ Authorization = "Bearer 你的auth_token" } `
  http://127.0.0.1:8765/bots
```

4. 进入 NodeSync 项目目录，启动交互脚本：

```powershell
cd D:\Dev\plugin_dev\NodeSync
$env:NODESYNC_LOCAL_FLOW_TOKEN = "你的auth_token"
python .\scripts\local_flow_driver.py --url http://127.0.0.1:8788 --stream local-flow
```

Linux 写法：

```bash
cd /path/to/NodeSync
export NODESYNC_LOCAL_FLOW_TOKEN="你的auth_token"
python scripts/local_flow_driver.py --url http://127.0.0.1:8788 --stream local-flow
```

## 交互命令

```text
/topic 文本       发送新话题
/rp 文本          发送新 RP 场景
/say 名字: 文本   让某个本地用户发言
/bot 名字: 文本   补一条 bot 发言
/history          查看本地构造消息历史
/help             查看帮助
/quit             退出
```

普通文本会作为“本地用户”发言。

示例：

```text
/rp 我们来到一扇封闭的石门前，门上有三枚暗淡的符文。
/say 小林: 我看看门上的符文。
/say 阿澈: 我也看看。
/say 小林: 还是看看。
/say 阿澈: 继续看看。
```

这类重复输入会更容易触发 NodeSync 的停滞判断。若开启真实 LLM 推进生成，服务端会委托在线 client 调模型生成推进消息；如果目标 bot 的 `capture_outbound=true`，推进消息会出现在 `/history` 里。

## 双 Bot 自动聊天流实测

如果要让脚本直接拉起两个 bot 进程、配置人设、发起话题、让两个 bot 用真实模型自然回复，并汇总 NodeSync 状态，可以运行：

```powershell
cd D:\Dev\plugin_dev\NodeSync
python .\scripts\run_local_chat_flow_test.py
```

Linux：

```bash
cd /path/to/NodeSync
python scripts/run_local_chat_flow_test.py
```

这个脚本会：

- 使用 MaiBot 1.0 的真实 `PluginLoader` 拉起两个 NodeSync adapter 实例。
- bot A 以 `server` 模式启动 NodeSync，bot B 以 `client` 模式连接。
- 为两个 bot 配置不同人设；普通聊天回复和 NodeSync 推进渲染都会带入对应人设，再调用真实 MaiBot 1.0 LLM 服务生成自然回复。
- 把所有消息喂入同一条本地聊天流。
- 在后半段注入重复短消息，观察 NodeSync 是否识别停滞并下发推进指令。
- 将完整报告写入 `data/local_chat_flow_runs/<时间>/report.json`。

这仍然不接真实 QQ/群平台；它验证的是真实 adapter、真实 bot 运行环境、真实 LLM 调用和 NodeSync 协调链路。

## 直接 HTTP 调用

不用交互脚本也可以直接发消息：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Headers @{ Authorization = "Bearer 你的auth_token" } `
  -ContentType "application/json" `
  -Body '{"stream_id":"local-flow","sender_id":"u1","sender_name":"本地用户","plain_text":"RP 场景：我们来到门前。"}' `
  http://127.0.0.1:8788/messages
```

查看历史：

```powershell
Invoke-RestMethod `
  -Headers @{ Authorization = "Bearer 你的auth_token" } `
  http://127.0.0.1:8788/messages/local-flow
```

## 安全提醒

- `local_flow` 默认关闭，只建议本机调试时开启。
- 不要把 `host` 设为 `0.0.0.0` 暴露给外网；如果确实要让其他机器喂本地消息，必须配置强 token。
- 交互脚本和 HTTP 接口只负责构造消息，不会读取或输出模型密钥。
