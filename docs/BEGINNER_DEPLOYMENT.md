# NodeSync 友好部署说明

这份文档写给不熟悉代码和命令行的使用者。你可以把它当成一张检查清单：按顺序做，做完一项再做下一项。

如果你已经很熟悉 MaiBot 插件、Python 环境和网络配置，可以直接看 `docs/USAGE.md`；那份文档更像技术手册。

## 先理解三个词

`server`

负责协调多个 bot。它会观察群聊是否卡住，并决定要不要让某个 bot 进行“私下对齐”或“在群里推进一句”。

`client`

连接到 server 的 bot。它负责把自己的群聊消息、上下文和发送能力提供给 server。

`auth_token`

server 和 client 之间的通行口令。所有 bot 必须填同一个 `auth_token`，把它当做你的银行卡密码！不要把它发到群里，也不要使用示例值！

最小部署方式：

```text
主 bot：NodeSync server
副 bot：NodeSync client
```

如果有更多 bot：

```text
主 bot：NodeSync server
副 bot A：NodeSync client
副 bot B：NodeSync client
副 bot C：NodeSync client
```

## 开始前检查

你需要准备：

- 已经能正常启动的 MaiBot。
- 至少两个 bot 实例，或者同一台机器上的两个 MaiBot 运行目录。
- NodeSync 源码目录，例如 Windows 的 `D:\Dev\plugin_dev\NodeSync`，或 Linux 的 `/opt/NodeSync`。
- Windows 能打开 PowerShell；Linux 能打开终端或 SSH。

下面会同时给 Windows 和 Linux 示例。你的路径如果不同，把示例路径换成自己的路径即可。

## 下载后应该放在哪里

先记住一句话：**NodeSync 源码目录不是 MaiBot 直接加载的插件目录。**

你可以把 NodeSync 下载到任意方便的位置，例如和 MaiBot 放在同一个父目录下：

```text
D:\Dev\plugin_dev\
  NodeSync\
  MaiBot-0.12.2\
  MaiBot-1.0-latest\
```

Linux 也类似：

```text
/opt/
  NodeSync/
  MaiBot-0.12.2/
  MaiBot-1.0-latest/
```

MaiBot 父项目真正会扫描的是它自己根目录下的 `plugins/`。安装完成后，最终路径应该长这样：

```text
MaiBot 0.12.2:
<MaiBot-0.12.2>/plugins/nodesync/
  plugin.py
  _manifest.json
  config.toml

MaiBot 1.0:
<MaiBot-1.0-latest>/plugins/nodesync_coordinator/
  plugin.py
  _manifest.json
  config.toml
```

也就是说：

- NodeSync 源码目录负责提供代码和安装脚本。
- `pip install -e .` 让 MaiBot 的 Python 环境能 import `nodesync` 包。
- `scripts/install_nodesync.py` 或手动复制，会把对应版本的 adapter 放进 MaiBot 的 `plugins/` 目录。

不要把整个 `NodeSync/` 文件夹直接丢进 MaiBot 的 `plugins/`。父项目需要看到的是 `plugins/nodesync/plugin.py` 或 `plugins/nodesync_coordinator/plugin.py`，而不是 `plugins/NodeSync/nodesync/adapters/...`。

Docker 用户也一样，只是 MaiBot 的 `plugins/` 通常被映射到了宿主机目录：

```text
Docker compose 目录/
  data/MaiMBot/plugins/nodesync/                 # MaiBot 0.12.2
  data/MaiMBot/plugins/nodesync_coordinator/     # MaiBot 1.0
```

## 第一步：选择谁当 server

选一个最稳定、最常在线的 bot 当 server。通常选择主 bot。

记下这个 bot 所在电脑的地址：

- 如果所有 bot 都在同一台电脑，地址写 `127.0.0.1`。
- 如果 client 在另一台电脑，地址写 server 电脑的局域网 IP，例如 `192.168.1.20`。

不知道局域网 IP 的话，在 server 电脑打开 PowerShell，运行：

```powershell
ipconfig
```

Linux 服务器可以运行：

```bash
hostname -I
```

找到类似 `IPv4 地址` 的一行。

## 第二步：生成一个通行口令

在 PowerShell 里运行：

```powershell
([guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N"))
```

Linux 终端里运行：

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

它会输出一长串字符。把这串字符保存好，后面所有 bot 的 `auth_token` 都填它。

不要使用这些示例口令：

- `change-me`
- `change-this-token`
- `test`
- `token`
- `password`

## 第三步：安装 NodeSync 包

每个运行 MaiBot 的环境都需要能找到 NodeSync。

Windows PowerShell：

```powershell
cd D:\Dev\plugin_dev\NodeSync
python -m pip install -e .
```

Linux 终端：

```bash
cd /opt/NodeSync
python3 -m pip install -e .
```

如果你的 MaiBot 1.0 是用 `uv` 运行的，再对 MaiBot 1.0 目录执行一次：

Windows PowerShell：

```powershell
cd D:\Dev\plugin_dev\MaiBot-1.0-latest
uv pip install -e ..\NodeSync
```

Linux 终端：

```bash
cd /opt/MaiBot-1.0-latest
uv pip install -e ../NodeSync
```

如果提示找不到 `python`、`pip` 或 `uv`，先不要继续。说明当前 MaiBot 环境还没有准备好，需要先处理 MaiBot 自身的运行环境。

## 推荐方式：使用安装脚本

NodeSync 提供了独立安装脚本：

```text
scripts/install_nodesync.py
```

它会帮你完成三件事：

- 选择正确的 MaiBot adapter。
- 复制到 MaiBot 的 `plugins/` 目录。
- 生成基础 `config.toml`。

如果目标插件目录已经存在，脚本默认会停止并提醒你，不会直接覆盖。需要替换旧插件时，加 `--force`，脚本会先备份旧目录再复制新目录。

最简单的方式是直接运行脚本，然后按提示回答问题：

```powershell
cd D:\Dev\plugin_dev\NodeSync
python scripts\install_nodesync.py
```

Linux：

```bash
cd /opt/NodeSync
python3 scripts/install_nodesync.py
```

如果你想少回答一个问题，可以直接把 MaiBot 根目录放在命令后面。脚本会尽量自动识别 MaiBot 版本：

```powershell
cd D:\Dev\plugin_dev\NodeSync
python scripts\install_nodesync.py ..\MaiBot-1.0-latest
```

Linux：

```bash
cd /opt/NodeSync
python3 scripts/install_nodesync.py ../MaiBot-0.12.2
```

Docker 用户通常传 compose 挂载出来的插件目录。因为脚本无法只靠 `plugins` 目录判断 MaiBot 版本，所以这里需要多写一个版本：

```bash
cd /opt/NodeSync
python3 scripts/install_nodesync.py ../maibot-docker/data/MaiMBot/plugins --version 1.0
```

如果你想先看看脚本会做什么，不实际写文件：

```bash
cd /opt/NodeSync
python3 scripts/install_nodesync.py \
  ../maibot-docker/data/MaiMBot/plugins \
  --version 1.0 \
  --dry-run
```

常用可选参数只有这几个：

| 参数                                      | 什么时候用                                                        |
| ----------------------------------------- | ----------------------------------------------------------------- |
| `--version 1.0` 或 `--version 0.12.2` | 脚本无法自动识别 MaiBot 版本时使用，Docker 插件目录模式通常需要。 |
| `--mode server` / `--mode client`     | 不想在向导里选择模式时使用。                                      |
| `--auth-token "..."`                    | 已经有 server token，特别是安装 client 时使用。                   |
| `--server-url "http://IP:8765"`         | 安装 client，且 server 不在本机时使用。                           |
| `--force`                               | 目标插件目录已存在，并且你确认要替换旧插件时使用。                |
| `--dry-run`                             | 只预览，不写入文件。                                              |
| `--install-package`                     | 顺便执行 `pip install -e NodeSync`。                            |

如果你希望脚本顺便执行 `pip install -e NodeSync`，加 `--install-package` 即可：

```bash
cd /opt/NodeSync
python3 scripts/install_nodesync.py ../MaiBot-1.0-latest --install-package
```

但要注意，这会安装到“运行脚本时使用的 Python 环境”。如果你的 MaiBot 使用 `uv` 或 Docker，务必确认脚本运行在正确环境里。

## 手动方式：复制插件到 MaiBot

如果你已经使用上面的安装脚本完成复制，可以跳过这一段。

如果你的 bot 是 MaiBot 0.12.2：

Windows PowerShell：

```powershell
cd D:\Dev\plugin_dev\NodeSync
Copy-Item -Recurse -Force `
  .\nodesync\adapters\maibot_012 `
  ..\MaiBot-0.12.2\plugins\nodesync
```

Linux 终端：

```bash
cd /opt/NodeSync
mkdir -p ../MaiBot-0.12.2/plugins
cp -r ./nodesync/adapters/maibot_012 ../MaiBot-0.12.2/plugins/nodesync
```

如果你的 bot 是 MaiBot 1.0：

Windows PowerShell：

```powershell
cd D:\Dev\plugin_dev\NodeSync
Copy-Item -Recurse -Force `
  .\nodesync\adapters\maibot_10 `
  ..\MaiBot-1.0-latest\plugins\nodesync_coordinator
```

Linux 终端：

```bash
cd /opt/NodeSync
mkdir -p ../MaiBot-1.0-latest/plugins
cp -r ./nodesync/adapters/maibot_10 ../MaiBot-1.0-latest/plugins/nodesync_coordinator
```

如果你的 MaiBot 路径不一样，把命令里的 MaiBot 目录改成你自己的路径。如果目标插件目录已经存在，先停止 MaiBot，再把旧目录备份或删除后重新复制。

### 0.12.2 和 1.0 的安装差异

两个版本都把第三方插件放在 MaiBot 根目录的 `plugins/` 下面，但加载方式不同。

| 项目         | MaiBot 0.12.2                                                           | MaiBot 1.0                                                               |
| ------------ | ----------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| 建议目录名   | `plugins/nodesync`                                                    | `plugins/nodesync_coordinator`                                         |
| 必需文件     | `plugin.py`、`_manifest.json`                                       | `plugin.py`、`_manifest.json`                                        |
| 入口要求     | `plugin.py` 中使用 `@register_plugin` 注册插件类                    | `plugin.py` 必须提供 `create_plugin()`                               |
| 插件 ID 来源 | 插件类里的 `plugin_name`，NodeSync 当前为 `nodesync`                | `_manifest.json` 里的 `id`，NodeSync 当前为 `nodesync.coordinator` |
| 配置生成     | 第一次启动时由 0.12.2 插件基类按 `config_schema` 生成 `config.toml` | 第一次启动时由 1.0 Runner 按 `config_model` 生成 `config.toml`       |
| 修改配置后   | 通常重启 MaiBot 最稳妥                                                  | 可由 1.0 运行时重载配置，但新手建议重启 MaiBot                           |

实际操作时，不要只复制单个 `plugin.py`。必须复制整个 adapter 文件夹，让 `plugin.py` 和 `_manifest.json` 在同一个插件目录里。

第一次复制后，推荐这样做：

1. 启动 MaiBot 一次，让它自动生成 `config.toml`。
2. 停止 MaiBot。
3. 打开生成出来的 `config.toml`，按下面的 server/client 示例修改。
4. 再启动 MaiBot。

这样比手动新建配置文件更不容易写错字段名。

## 第五步：配置 server

找到 server bot 的 NodeSync 插件配置文件。它通常叫 `config.toml`，位置在 MaiBot 的插件配置目录里。

常见位置：

```text
MaiBot 0.12.2: <MaiBot根目录>/plugins/nodesync/config.toml
MaiBot 1.0:    <MaiBot根目录>/plugins/nodesync_coordinator/config.toml
Docker:        宿主机 ./data/MaiMBot/plugins/<插件目录>/config.toml
```

如果找不到，先启动 MaiBot 一次，让插件系统自动生成配置。还找不到的话，可以在 MaiBot 目录里搜索 `nodesync` 或 `config.toml`。

server bot 的关键配置如下，把 `auth_token` 换成你第二步生成的口令：

```toml
[plugin]
enabled = true

[nodesync]
mode = "server"
bot_id = "main-bot"
bot_name = "主 Bot"
reply_persona = ""
auth_token = "把第二步生成的长口令放这里"
allow_query_token_auth = false
server_host = "127.0.0.1"
server_port = 8765
server_url = "http://127.0.0.1:8765"
data_dir = "data/nodesync"
streams = []

[prompts]
directory = "prompts"

[diagnostics]
enabled = false

[policy]
enable_llm_decision = true
enable_llm_advance_generation = true
intervention_cooldown_seconds = 300
analysis_min_messages = 4
analysis_window_messages = 8
```

如果 client 在另一台电脑，把 server 的 `server_host` 改成：

```toml
server_host = "0.0.0.0"
```

这表示允许其他电脑连接。此时必须使用强口令，不能用示例口令。

## 第六步：配置 client

每个 client bot 也要找到自己的 NodeSync 插件配置文件。

client 的关键配置如下：

```toml
[plugin]
enabled = true

[nodesync]
mode = "client"
bot_id = "side-bot-a"
bot_name = "副 Bot A"
reply_persona = ""
auth_token = "和 server 完全一样的长口令"
allow_query_token_auth = false
server_host = "127.0.0.1"
server_port = 8765
server_url = "http://127.0.0.1:8765"
data_dir = "data/nodesync"
streams = []

[prompts]
directory = "prompts"

[diagnostics]
enabled = false

[policy]
enable_llm_decision = true
enable_llm_advance_generation = true
intervention_cooldown_seconds = 300
analysis_min_messages = 4
analysis_window_messages = 8
```

注意三件事：

- 每个 bot 的 `bot_id` 必须不同。
- client 的 `auth_token` 必须和 server 一模一样。
- client 的 `server_url` 必须指向 server。

`reply_persona` 是“这个 bot 在被要求推进时应该怎么说话”的人设提示。你可以先留空，NodeSync 会尽量读取 MaiBot 自己的人设；如果你的 bot 角色很明确，也可以写成一句短说明，例如：

```toml
reply_persona = "冷静、观察细致，像队伍里的调查员，说话简短但会推动下一步。"
```

这个字段不会发到群里，它只会在 NodeSync 让 bot 生成推进发言时，作为私下提示交给模型。

如果所有 bot 在同一台电脑：

```toml
server_url = "http://127.0.0.1:8765"
```

如果 client 在另一台电脑，假设 server 电脑 IP 是 `192.168.1.20`：

```toml
server_url = "http://192.168.1.20:8765"
```

## 想改提示词怎么办

安装脚本会把常用提示词放到插件目录里；如果目录缺失，NodeSync 启动时也会自动补齐：

```text
MaiBot 0.12.2: <MaiBot根目录>/plugins/nodesync/prompts/
MaiBot 1.0:    <MaiBot根目录>/plugins/nodesync_coordinator/prompts/
```

如果你不想改代码，只想调整 bot 推进时的说法，优先改这个文件：

```text
plugins/<NodeSync插件目录>/prompts/client_advance_reply.txt
```

里面的 `{{bot_name}}`、`{{reply_persona}}`、`{{recent_messages}}` 这类内容是变量，建议保留。改完后重启 MaiBot 最稳妥。

## 需要排查消息字段怎么办

平时不要开诊断。只有真实平台消息收不到、字段不对、stream_id 不对时，再临时打开：

```toml
[diagnostics]
enabled = true
output_dir = ""
include_hashes = true
max_depth = 4
max_items = 40
```

诊断文件会写到：

```text
data/nodesync/diagnostics/message_schema.jsonl
```

它只记录字段结构、类型、长度和 hash，不记录群聊原文、token、cookie 或 API key。问题排查完，记得改回 `enabled = false`。

## 第七步：启动顺序

按这个顺序启动：

1. 先启动 server bot。
2. 等 server bot 启动完成。
3. 再启动 client bot。

如果反过来启动，client 可能会先连不上，但通常会自动重试。为了少看报错，建议仍然先启动 server。

## 第八步：确认是否连接成功

在 server 电脑打开 PowerShell 或 Linux 终端。

先检查 server 是否活着：

Windows PowerShell：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

Linux 终端：

```bash
curl http://127.0.0.1:8765/health
```

如果看到类似下面的内容，说明 server 已经启动：

```text
ok   time
--   ----
True ...
```

再查看在线 bot。把下面命令里的口令换成你的 `auth_token`：

Windows PowerShell：

```powershell
Invoke-RestMethod `
  -Headers @{ Authorization = "Bearer 把你的长口令放这里" } `
  http://127.0.0.1:8765/bots
```

Linux 终端：

```bash
curl -H "Authorization: Bearer 把你的长口令放这里" \
  http://127.0.0.1:8765/bots
```

如果看到 `live` 里出现 client 的 `bot_id`，说明连接成功。

例如：

```text
live
----
{main-bot, side-bot-a}
```

## 第九步：在群里测试

让 bot 所在群聊里出现几条连续消息。NodeSync 默认至少看到 `analysis_min_messages` 条消息后才允许分析，并且每新增 `analysis_window_messages` 条消息才会主动拉取上下文、请模型判断一次。

测试时可以让群聊进入一个轻微卡住的状态，例如：

```text
A：我想吃饼干了。
B：我看看冰箱。
C：我也看看。
A：继续看看。
B：还是先看看。
```

正常情况下：

- 轻度停滞时，模型通常会让 NodeSync 先做私有上下文对齐，不一定马上在群里发消息。
- 明显停滞时，模型才会让某个 bot 在群里发一条推进消息。
- 推进不是换话题，而是让当前话题或 RP 场景进入下一步。

## 不接真实群平台的本地联调

如果你暂时不想接真实 QQ/群平台，也可以用“本地信息流”测试。这个模式里，MaiBot 进程、LLM 和 NodeSync 插件都是真的，只有群聊消息由本地脚本构造。

在要接收构造消息的 bot 配置里打开：

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

`auth_token` 留空时，会复用 `[nodesync]` 里的 `auth_token`。如果同一台机器上多个 bot 都开启 `local_flow`，每个 bot 的 `port` 要不同。

然后进入 NodeSync 项目目录，启动交互脚本：

Windows PowerShell：

```powershell
cd D:\Dev\plugin_dev\NodeSync
$env:NODESYNC_LOCAL_FLOW_TOKEN = "把你的长口令放这里"
python .\scripts\local_flow_driver.py --url http://127.0.0.1:8788 --stream local-flow
```

Linux 终端：

```bash
cd /opt/NodeSync
export NODESYNC_LOCAL_FLOW_TOKEN="把你的长口令放这里"
python3 scripts/local_flow_driver.py --url http://127.0.0.1:8788 --stream local-flow
```

进入脚本后可以输入：

```text
/rp 我们来到一扇封闭的石门前，门上有三枚暗淡的符文。
/say 小林: 我看看门上的符文。
/say 阿澈: 我也看看。
/say 小林: 还是看看。
/say 阿澈: 继续看看。
/history
```

需要注意：`capture_outbound = true` 时，NodeSync 的推进消息会被捕获成本地 bot 发言，不会真的发到 QQ/真实平台。也就是说，这个模式能测试真实 bot 进程、真实 LLM、真实 adapter 和 NodeSync 协作逻辑，但不能证明真实平台外发链路一定可用。更完整说明见 `docs/LOCAL_FLOW_TESTING.md`。

## Docker 用户怎么部署

当前 NodeSync 仓库没有内置官方 `Dockerfile` 或 `docker-compose.yml`，也没有发布官方镜像。所以 Docker 部署不是“一键启动 NodeSync 容器”，而是把 NodeSync 安装进你现有的 MaiBot 容器里。

这部分是通用接入方法，需要按你的 MaiBot 镜像、目录和 compose 文件调整。

MaiBot 0.12.2 和 1.0 的官方 compose 示例里，插件目录通常这样挂载：

```yaml
volumes:
  - ./data/MaiMBot/plugins:/MaiMBot/plugins
```

这意味着你在宿主机操作的是：

```text
./data/MaiMBot/plugins
```

容器里看到的是：

```text
/MaiMBot/plugins
```

所以 Docker 用户复制插件时，通常应复制到宿主机的 `./data/MaiMBot/plugins/`，而不是进入容器后手改 `/MaiMBot/plugins/`。这样容器重启后插件还在。

### 情况一：MaiBot 已经在 Docker 里运行

思路很简单：

1. 把 NodeSync 源码挂载进 MaiBot 容器。
2. 让容器里的 Python 环境安装 NodeSync。
3. 把对应 MaiBot 版本的 adapter 放进容器里的插件目录。
4. 如果这个容器是 server，把 `8765` 端口暴露出来。

一个简化的 compose 片段如下：

```yaml
services:
  maibot-server:
    image: your-maibot-image
    volumes:
      - ../NodeSync:/opt/NodeSync
      - ./data/MaiMBot/plugins:/app/plugins
    ports:
      - "8765:8765"
    command: >
      sh -c "cd /opt/NodeSync &&
             python -m pip install -e . &&
             cp -r ./nodesync/adapters/maibot_10 /app/plugins/nodesync_coordinator &&
             python main.py"
```

上面只是示例：

- `your-maibot-image` 换成你的 MaiBot 镜像。
- `/app/plugins` 换成容器里真实的 MaiBot 插件目录。
- `python main.py` 换成你原本启动 MaiBot 的命令。
- MaiBot 0.12.2 使用 `maibot_012`，MaiBot 1.0 使用 `maibot_10`。

如果容器里已经有 NodeSync 插件目录，重复 `cp -r` 可能会复制出嵌套目录。正式使用时，建议在构建镜像阶段固定复制，或在停止容器后手动整理插件目录。

更稳妥的做法是先在宿主机复制插件目录：

```bash
cd /opt/NodeSync

# MaiBot 0.12.2
mkdir -p ../maibot-docker/data/MaiMBot/plugins
cp -r ./nodesync/adapters/maibot_012 ../maibot-docker/data/MaiMBot/plugins/nodesync

# MaiBot 1.0
mkdir -p ../maibot-docker/data/MaiMBot/plugins
cp -r ./nodesync/adapters/maibot_10 ../maibot-docker/data/MaiMBot/plugins/nodesync_coordinator
```

然后再解决 Python 包安装问题。NodeSync 不是纯单文件插件，`plugin.py` 还需要导入 `nodesync` 包。Docker 里常见有三种做法：

| 做法               | 适合谁             | 说明                                                                          |
| ------------------ | ------------------ | ----------------------------------------------------------------------------- |
| 自定义镜像         | 正式部署           | 在 Dockerfile 里 `pip install` NodeSync，最稳定。                           |
| 启动时安装         | 临时测试           | compose 的 `command` 里先 `pip install -e /opt/NodeSync`，再启动 MaiBot。 |
| 挂载 site-packages | 熟悉 Docker 的用户 | MaiBot compose 里有 `site-packages` 持久化示例，但需要你自己启用和维护。    |

正式使用更推荐自定义镜像或把安装步骤固定在 compose 中，不建议每次手动进容器改文件。

### 情况二：server 和 client 都在同一个 compose 里

如果两个 MaiBot 容器在同一个 `docker-compose.yml` 里，client 的 `server_url` 可以直接写 server 服务名。

例如 server 服务叫 `maibot-server`：

```toml
[nodesync]
mode = "client"
server_url = "http://maibot-server:8765"
reply_persona = ""
auth_token = "和 server 完全一样的长口令"
```

server 容器里建议这样配置：

```toml
[nodesync]
mode = "server"
server_host = "0.0.0.0"
server_port = 8765
server_url = "http://127.0.0.1:8765"
reply_persona = ""
auth_token = "把第二步生成的长口令放这里"
```

`server_host = "0.0.0.0"` 在容器里很常见，因为它表示容器内服务监听所有容器网卡。只要你使用强 `auth_token`，NodeSync 会允许启动。

### 情况三：client 在 Docker 外面

如果 server bot 在 Docker 里，client bot 在宿主机或另一台机器上：

- compose 里要映射端口：`"8765:8765"`。
- server 配置使用 `server_host = "0.0.0.0"`。
- client 的 `server_url` 写宿主机 IP，例如 `http://192.168.1.20:8765`。
- 防火墙要允许访问 `8765` 端口。

### Docker 部署限制

- 当前项目没有官方 Docker 镜像和一键 compose 文件。
- 上面的 compose 片段是接入模板，不是已经针对所有 MaiBot 镜像验证过的最终配置。
- 不同 MaiBot 镜像的插件目录和启动命令可能不同，需要按实际镜像调整。
- 如果跨公网访问 Docker 里的 server，仍建议放在 VPN、内网隧道或 TLS 反向代理后面。

## 常见问题

`/bots` 看不到 client：

- server 是否先启动了。
- client 的 `server_url` 是否写对了。
- server 和 client 的 `auth_token` 是否完全一致。
- 每个 bot 的 `bot_id` 是否不同。
- 跨机器时，server 电脑防火墙是否放行 `8765` 端口。

server 启动失败，并提示 token 太弱：

- 你可能使用了 `change-me`、`test` 这类示例口令。
- 按第二步重新生成一个长口令，并同步填到所有 bot。

能连接，但群里没有推进：

- 消息数量可能还不够。
- 当前对话可能被判断为“仍有进展”，所以不会介入。
- 可能还在冷却时间内。
- 如果你设置了 `streams`，确认当前群聊的 stream 在列表里。

模型费用太高或不想先用模型：

可以先这样配置，验证基础通信：

```toml
[policy]
enable_llm_decision = false
enable_llm_advance_generation = false
```

确认通信没问题后，再打开模型生成：

```toml
[policy]
enable_llm_decision = true
enable_llm_advance_generation = true
analysis_window_messages = 8
```

## 安全提醒

- 不要把 `auth_token` 发到群里、截图里或公开仓库里。
- 不建议把 NodeSync 直接暴露到公网。
- 跨公网使用时，请放在 VPN、内网隧道或带 TLS 的反向代理后面。
- `allow_query_token_auth` 保持 `false`，除非你明确知道为什么要打开它。

## 当前还需要知道的限制

- NodeSync 已验证同机跨进程通信和真实模型推进生成，但跨机器部署仍需要你检查网络、防火墙和路由。
- 安装脚本可以减少复制错误，但 Docker 镜像的启动命令、插件目录和 Python 环境仍需要按你的实际 compose 调整。

到这里，你已经完成了基本部署。后续如果要细调冷却时间、模型策略、stream 白名单，再看 `docs/USAGE.md` 会更合适。
