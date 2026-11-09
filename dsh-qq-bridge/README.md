# dsh-qq-bridge

让外部程序（例如 QQ 机器人）把一段 prompt 送进 **正在运行的 DSH 会话**，并拿回这一轮的 assistant 回复文本。

- 只在 `127.0.0.1` 上监听，带共享密钥；
- 不新开会话、不抢写锁：通过 DSH 宿主进程内的 `ctx.get('agents')` 直接 `agent.followup()`；
- 用消息 id 精确匹配自己的 `user/message`，监听 `session/event` 收集文本，等对应 `turn/end`；
- 同一会话并发返回 409，超时返回 504（带已产生的部分文本）。

## 兼容性

- DSH `>= 0.2.0-rc.2 < 0.3.0`（在 `0.2.1-rc.3` 上验证）
- 仅 Windows（配合 AstrBot 的 QQ 桥插件使用；插件本身是纯 Node，跨平台也可跑，但 AstrBot 侧目前只支持 Windows）

## 接口

```
GET  /health
  → {"ok":true,"name":"dsh-qq-bridge","version":"0.2.0","port":5795,
     "timeoutMs":1500000,"busy":[]}

POST /prompt
  header: x-bridge-secret: <secret>
  body:   {"prompt":"...","sessionId":"session-xxxx","timeoutMs":1500000}
  200 → {"ok":true,"sessionId":"...","text":"...","elapsedMs":1234}
  400 → prompt / sessionId 缺失，或 JSON 非法
  401 → 密钥错误
  409 → session-not-live（会话没在这个 DSH 进程里开着）/ session-busy（上一条还在跑）
  504 → timeout（附 partial：已经产生的文本）
```

## 安装（手动，适用于 Electron 管理的 profile）

以默认 `desktop` profile 为例（把 `<profile>` 换成你的 profile 名）：

1. 把本目录复制到：

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
         "bundles": [
           "...原有 bundles...",
           "dsh-qq-bridge"
         ]
       }
     }
   }
   ```

3. 编辑插件包里的 `cordis.patch.yml`，把 `secret` 改成你自己的随机字符串：

   ```yaml
   config:
     port: 5795
     secret: "换成你的密钥"
     timeoutMs: 1500000
   ```

4. **重启 DSH 桌面**（插件是进程内加载的，不能热插拔）。

5. 验证：

   ```powershell
   curl http://127.0.0.1:5795/health
   ```

   应返回 `version: 0.2.1`、`timeoutMs: 1500000`。

## 卸载

- 从 profile 的 `package.json` 里删掉 `dsh-qq-bridge` 的依赖和 bundles 条目；
- 删除 `node_modules/dsh-qq-bridge` 与 `plugins/dsh-qq-bridge`；
- 重启 DSH。

## 安全

- 只绑定 `127.0.0.1`，不要把它暴露到公网；
- `secret` 为空时插件会告警，且 AstrBot 端会拒绝发送；两边必须一致；
- 请求体上限 256KB，超时默认 25 分钟；
- 知道密钥的本机程序可以向 DSH 会话注入消息，请仅在内网/本机使用。

## License

MIT
