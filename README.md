# Astrbot-chat-connect-dsh

把 **QQ（AstrBot）** 和本机 **DeepSeek Harness（DSH）** 的会话连起来：在 QQ 里发 `/调用dsh <任务>`，机器人不进行理解、不参与对话，直接把这句话送进 DSH 当前会话执行；DSH 跑完后，把这一轮的完整回复文本发回 QQ（极短纯文本直接发消息，其余写成 Markdown 文件发送）。

适合：人不在电脑前，但想用 QQ 指挥 DSH 干活（总结群聊、跑脚本、查文件、继续某个对话任务等）。

## 组成

| 目录 | 类型 | 作用 |
| --- | --- | --- |
| [`dsh-qq-bridge/`](dsh-qq-bridge/) | DSH 插件 | 在 DSH 宿主进程内监听 `127.0.0.1:5795`，接收 prompt，用 `agent.followup()` 注入当前会话，收集 `assistant` 文本并返回 |
| [`astrbot-plugin-dsh-bridge/`](astrbot-plugin-dsh-bridge/) | AstrBot 插件 | 处理 `/调用dsh` 指令，`stop_event` 阻止模型介入，按 HTTP / ACP / 窗口三种后端转发，把结果发回 QQ |

```
QQ 群/私聊
  │  /调用dsh 总结今天的群聊
  ▼
AstrBot · astrbot_plugin_dsh_bridge
  │  POST http://127.0.0.1:5795/prompt  + x-bridge-secret
  ▼
DSH 宿主进程 · dsh-qq-bridge
  │  ctx.get('agents').get(sessionId).followup(用户消息)
  │  监听 session/event → assistant/message → turn/end
  ▼
返回 { ok, text } → AstrBot 发回 QQ（短文本直接发，长文本 Markdown 文件）
```

## 下载

Release 里提供可直接安装的包和描述文件：

**https://github.com/windmoon3032/Astrbot-chat-connect-dsh/releases/latest**

- `dsh-qq-bridge-0.2.2.zip` — DSH 侧插件
- `astrbot-plugin-dsh-bridge-0.2.2.zip` — AstrBot 侧插件
- `dsh-qq-bridge.info.json` — 用途、安装、配置、HTTP 接口、排查表

也可以直接使用仓库里的源码目录。

## 环境要求

- Windows 10 / 11
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) `>=4.13,<5`（`aiocqhttp` 平台，NapCat / LLOneBot 等）
- DeepSeek Harness `>=0.2.0-rc.2 <0.3.0`（在 `0.2.2-rc.3` 验证）
- Python 依赖（AstrBot 侧）：`pywin32`、`zstandard`（仅截图/窗口后端需要）

## 安装

### 可以直接找大肥鱼许愿

### 1. DSH 侧（dsh-qq-bridge）

以默认 `desktop` profile 为例，`<profile>` 换成实际 profile 名：

1. 把 `dsh-qq-bridge/` 复制到：

   ```
   %USERPROFILE%\.dsh\profiles\<profile>\node_modules\dsh-qq-bridge
   ```

   再复制一份到：

   ```
   %USERPROFILE%\.dsh\profiles\<profile>\plugins\dsh-qq-bridge
   ```

2. 编辑 `%USERPROFILE%\.dsh\profiles\<profile>\package.json`：

   ```json
   {
     "dependencies": {
       "dsh-qq-bridge": "file:./plugins/dsh-qq-bridge"
     },
     "dsh": {
       "profile": {
         "bundles": ["...原有...", "dsh-qq-bridge"]
       }
     }
   }
   ```

3. 编辑插件包里的 `cordis.patch.yml`，把 `secret` 换成你自己的随机字符串：

   ```yaml
   config:
     port: 5795
     secret: "换成一个随机字符串"
     timeoutMs: 1500000
   ```

4. 重启 DSH 桌面，然后验证：

   ```powershell
   curl http://127.0.0.1:5795/health
   ```

   应返回 `"version":"0.2.2"`、`"timeoutMs":1500000`。

> 端口被占用时插件只告警，不会让 DSH 崩溃；`secret` 为空时会提示配置。

### 2. AstrBot 侧（astrbot-plugin-dsh-bridge）

1. 把 `astrbot-plugin-dsh-bridge/` 复制到：

   ```
   <AstrBot>/data/plugins/astrbot_plugin_dsh_bridge
   ```

2. 安装可选依赖（仅截图/窗口后端需要）：

   ```powershell
   pip install -r requirements.txt
   ```

3. AstrBot WebUI → 插件 → 重载插件。

4. 在插件配置里填 `bridge_secret`，必须与 DSH 端的 `secret` 完全一致。

## 配置

### AstrBot 插件

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `backend` | `http` | `http`=DSH 进程内插件（能接当前会话）；`acp`=官方 ACP（只能接未被占用的会话）；`window`=窗口自动化（备用） |
| `bridge_url` | `http://127.0.0.1:5795/prompt` | DSH 桥地址 |
| `bridge_secret` | 空 | 两边必须一致，为空则拒绝发送 |
| `session_id` | 空 | 目标会话 id；留空自动取 `dsh_home` 下最近更新的会话 |
| `dsh_home` | 空 | 默认 `%USERPROFILE%\.dsh` 或环境变量 `DSH_HOME` |
| `admins` | `[]` | 允许使用的 QQ 号；留空回退 AstrBot 全局管理员 |
| `prefixes` | `/调用dsh`、`/dsh` | 指令前缀 |
| `timeout_seconds` | `1500` | 最长等待（25 分钟） |
| `short_text_max_chars` | `120` | 短文本直接发的上限，其余写 Markdown 文件 |
| `send_screenshot` | `true` | 是否附带 DSH 窗口截图 |
| `acp_cwd` | 空 | ACP 后端工作目录 |
| `window_keyword` 等 | — | 窗口后端参数 |

### DSH 插件

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `port` | `5795` | 只绑定 `127.0.0.1` |
| `secret` | 空 | 与 AstrBot 的 `bridge_secret` 一致 |
| `timeoutMs` | `1500000` | 单次请求最长等待 |
| `maxBodyBytes` | `262144` | 请求体上限 |

## 使用

```
/调用dsh 总结一下今天群聊里吵的那件事
/dsh 只回复：桥接正常
```

- 只有管理员可用；
- 命中后先回「收到，丢给 dsh 了」，然后 `stop_event`，不进入模型；
- 极短纯文本直接发消息，其余写成 `.md` 文件发送（不截断）：

  ```markdown
  # DSH 回复

  - 时间：2026-11-10 00:00:00
  - 会话：大肥鱼:GroupMessage:123456789
  - 字数：1234

  ---

  完整回复……
  ```

## HTTP 接口（DSH 侧）

```
GET  /health
POST /prompt     header: x-bridge-secret
     body: {"prompt":"...","sessionId":"session-xxxx","timeoutMs":1500000}
     200 ok / 400 参数错误 / 401 密钥错误 / 409 session-not-live|session-busy
     413 请求体过大 / 504 timeout（带 partial）
```

## 安全

- 桥只监听 `127.0.0.1`，不要暴露到公网；
- 密钥只存在两边配置里，仓库代码不含任何预置密钥或个人 QQ 号；
- 指令仅管理员可用；命中后不产生模型回复、不消耗 LLM token；
- 同一会话并发请求会被拒绝，超时返回已产生的部分文本。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `DSH 桥没在监听` | DSH 没启动，或 DSH 侧插件没装/没重启 |
| `共享密钥不对` | 两边 `secret` / `bridge_secret` 不一致 |
| `这个会话不在 DSH 进程里活着` | 目标会话没在 DSH 桌面里打开，或 `session_id` 填错 |
| `这个会话正在执行上一条` | 等 DSH 跑完再发 |
| ACP 报 `already owned by an active write handle` | 桌面正开着该会话，改用 `http` 后端 |
| 截图发不出来 | 没装 `pywin32`，或 DSH 窗口被最小化/标题不含 `window_keyword` |

更完整的排查见 [`astrbot-plugin-dsh-bridge/README.md`](astrbot-plugin-dsh-bridge/README.md) 与 Release 里的 `dsh-qq-bridge.info.json`。

## License

MIT
