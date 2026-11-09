# AstrBot × DSH 桥（astrbot-plugin-dsh-bridge）

QQ → 本机 [DeepSeek Harness](https://github.com/deepseek-ai) 会话的桥。管理员在 QQ 里发：

```
/调用dsh 帮我把今天群聊总结一下
```

机器人**不进行理解、不参与对话**，直接把这句话送进本机 DSH 的当前会话；DSH 跑完后，把这一轮的完整回复文本（可选 DSH 窗口截图）发回 QQ。

三个后端：

| backend | 原理 | 适用 |
| --- | --- | --- |
| `http`（默认，推荐） | 调 DSH 进程内插件 `dsh-qq-bridge` 的 `127.0.0.1:5795/prompt` | 能接**当前正在聊的会话** |
| `acp` | 起 `dsh --profile acp` 子进程，走官方 ACP stdio JSON-RPC | 只能接未被桌面占用的会话（桌面打开的会话有写锁） |
| `window` | 找 DSH 窗口、粘贴、回车、读会话文件 | 兜底，最不稳定 |

## 安装

1. 把本目录放进 AstrBot 的插件目录：

   ```
   <AstrBot>/data/plugins/astrbot_plugin_dsh_bridge
   ```

2. 安装可选依赖（HTTP 后端不是必须，但截图/窗口后端需要）：

   ```powershell
   pip install -r requirements.txt
   ```

3. 在 AstrBot WebUI → 插件 → 重载插件，或重启 AstrBot。

4. 装 DSH 侧插件 `dsh-qq-bridge`（见同包 `dsh-qq-bridge` 目录的 README / `dsh-qq-bridge.info.json`），并把两边的共享密钥配成同一个值。

## 配置（AstrBot 插件配置页）

| 键 | 说明 |
| --- | --- |
| `enabled` | 总开关 |
| `admins` | 允许使用的 QQ 号；留空回退到 AstrBot 全局管理员 |
| `prefixes` | 指令前缀，默认 `/调用dsh`、`/dsh` |
| `backend` | `http`（默认）/ `acp` / `window` |
| `bridge_url` | DSH 桥地址，默认 `http://127.0.0.1:5795/prompt` |
| `bridge_secret` | 共享密钥，**必须与 DSH 端一致**；为空时拒绝发送 |
| `session_id` | 目标会话 id；留空自动取 `dsh_home` 下最近更新的会话 |
| `dsh_home` | DSH 数据目录；留空用 `%USERPROFILE%\.dsh` 或环境变量 `DSH_HOME` |
| `timeout_seconds` | 等待上限，默认 1500 秒（25 分钟） |
| `short_text_max_chars` | 短到可以直接发文字的纯文本上限，默认 120；其余写 JSON 文件 |
| `max_chars` | 已弃用：文件模式不再截断，仅兼容旧配置保留 |
| `send_screenshot` | 是否附带 DSH 窗口截图（需 pywin32 + 窗口存在） |
| `acp_cwd` | `acp` 后端工作目录；留空用用户目录 |
| `window_keyword` / `input_x_ratio` / `input_y_ratio` | `window` 后端参数，窗口布局不同时微调 |

## 指令

```
/调用dsh <任意内容>     把内容送进 DSH 当前会话
/dsh <任意内容>         同上
```

只有 `admins` 里的人（或 AstrBot 全局管理员）能用；命中指令后会 `stop_event`，不会触发模型回复。

## 输出格式

- **极短纯文本**（单行、无 Markdown/JSON/代码样式、≤ `short_text_max_chars` 字，默认 120）→ 直接发一条文字消息；
- **其余全部**（长文本、多行、Markdown、代码块、表格等）→ 写成 `.md` 文件发送：

  ```markdown
  # DSH 回复

  - 时间：2026-11-10 00:20:00
  - 会话：大肥鱼:GroupMessage:123456789
  - 字数：1234

  ---

  完整回复文本……
  ```

  文件保存在 `<插件目录>/files/dsh-reply-*.md`，只保留最近 100 个；文件模式**不截断**，Markdown 换行正常，阅读器直接可读。

## 行为

1. 收到指令 → 回一句「收到，丢给 dsh 了」；
2. 根据 `backend` 转发；
3. `http` 后端：POST 给 DSH 桥，等它返回本轮的 assistant 文本（最长 `timeout_seconds`）；
4. 短文本直接发消息，其余写 `.md` 文件发送（不截断）；截图开启时再发一张 DSH 窗口图。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `DSH 桥没在监听 5795` | DSH 没启动，或 DSH 侧插件没装/没重启 |
| `共享密钥不对` | 两边 `secret` / `bridge_secret` 不一致 |
| `这个会话不在 DSH 进程里活着` | 目标会话没在 DSH 桌面里打开；或 `session_id` 填错 |
| `这个会话正在执行上一条` | 等 DSH 跑完再发 |
| `acp` 报 `already owned by an active write handle` | 桌面正开着该会话，改用 `http` 后端 |
| 截图发不出来 | 没装 pywin32，或 DSH 窗口被最小化/改标题 |

## 安全

- 只调用 `127.0.0.1`，不要暴露到公网；
- 密钥只存在两边配置里，不写进代码；
- 指令仅管理员可用；命中后不进入模型，不产生 LLM 费用。

## 兼容性

- Windows 10/11；
- AstrBot `>=4.13,<5`；
- DSH `0.2.2-rc.3` 验证通过（DSH 侧插件要求 `>=0.2.2-rc.2 <0.3.0`）。

## License

MIT
