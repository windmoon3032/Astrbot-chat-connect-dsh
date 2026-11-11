# -*- coding: utf-8 -*-
"""DSH 桥：/调用dsh <prompt> —— 不过模型，直接把话贴进本机 DSH 当前会话。

流程：
  1. 命中指令后抢在 LLM 之前 stop_event，机器人不会理解/回复这句话；
  2. 找到 DeepSeek Harness 窗口，聚焦、点输入框、粘贴、回车；
  3. 盯当前会话的 session.v4.jsonl.zstd，等这次 turn/end；
  4. 取出该 turn 的 assistant 文本，连同窗口截图一起发回原会话。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import zstandard as zstd
except Exception:  # 窗口后端才需要，缺了不影响 HTTP/ACP 后端
    zstd = None

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, File, Image, Plain
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_dsh_bridge"
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import acp_client  # noqa: E402

try:
    import win_ui  # noqa: E402
except Exception:  # pywin32 缺失时窗口后端/截图不可用
    win_ui = None

SESSION_FILE = "session.v4.jsonl.zstd"


class DshBridge(Star):
    _notify_generation = 0

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._tasks: set[asyncio.Task] = set()
        self._busy = False
        self._busy_since = 0.0
        self._cache: dict[str, tuple[int, int, list[dict]]] = {}
        self._sent_turns: dict[str, int] = {}
        self._at_targets: list[str] = []
        self._pending_question: dict | None = None
        self._session_override: str = self._load_session_override()
        self._notify_state: dict[str, int] = {}
        self._notify_task: asyncio.Task | None = None
        try:
            self._notify_state = self._load_notify_state()
        except Exception:
            self._notify_state = {}
        if bool(self._cfg("notify_enabled", False)):
            try:
                old = self._notify_task
                if old and not old.done():
                    old.cancel()
                type(self)._notify_generation += 1
                self._notify_gen = type(self)._notify_generation
                self._notify_task = asyncio.create_task(self._notify_loop())
                logger.info(f"[{PLUGIN_NAME}] DSH 完成推送已启用")
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 启动完成推送失败: {exc}")

    # ------------------------------------------------------------- helpers
    def _basic(self) -> dict:
        value = self.config.get("basic") if hasattr(self.config, "get") else None
        return value if isinstance(value, dict) else {}

    def _cfg(self, key: str, default=None):
        return self._basic().get(key, default)

    def _admins(self) -> set[str]:
        raw = self._cfg("admins", [])
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,，\s]+", raw) if x]
        return {str(x).strip() for x in (raw or []) if str(x).strip()}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "")
        if sender and sender in self._admins():
            return True
        try:
            if bool(event.is_admin()):
                return True
        except Exception:
            pass
        try:
            admins = self.context.get_config(umo=event.unified_msg_origin).get("admins_id", []) or []
            return sender in {str(x).strip() for x in admins}
        except Exception:
            return False

    def _dsh_home(self) -> Path:
        configured = str(self._cfg("dsh_home", "") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))

    def _session_path(self) -> Path | None:
        root = self._dsh_home() / "sessions"
        if not root.is_dir():
            return None
        wanted = str(self._cfg("session_id", "") or "").strip()
        best: Path | None = None
        best_mtime = -1.0
        for path in root.glob(f"**/{SESSION_FILE}"):
            if wanted and wanted not in path.parent.name:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
        return best

    def _read_events(self, path: Path, force: bool = False) -> list[dict]:
        try:
            st = path.stat()
        except OSError:
            return []
        key = str(path)
        cached = self._cache.get(key)
        if (
            not force
            and cached is not None
            and cached[0] == st.st_size
            and cached[1] == st.st_mtime_ns
        ):
            return cached[2]
        if zstd is None:
            return cached[2] if cached else []
        try:
            raw = zstd.ZstdDecompressor().stream_reader(path.read_bytes()).read()
        except Exception:
            return cached[2] if cached else []
        events: list[dict] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                events.append(obj)
        self._cache[key] = (st.st_size, st.st_mtime_ns, events)
        return events

    @staticmethod
    def _last_turn(events: list[dict]) -> int:
        for event in reversed(events):
            if event.get("type") == "turn/end":
                try:
                    return int((event.get("data") or {}).get("turn") or 0)
                except Exception:
                    continue
        return 0

    @staticmethod
    def _assistant_text(events: list[dict], turn: int, min_seq: int) -> str:
        parts: list[str] = []
        for event in events:
            if event.get("type") != "assistant/message":
                continue
            if int(event.get("seq") or 0) <= min_seq:
                continue
            data = event.get("data") or {}
            if turn and int(data.get("turn") or 0) != turn:
                continue
            message = data.get("message") or {}
            for item in message.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text") or "").strip()
                    if text:
                        parts.append(text)
        return "\n".join(parts).strip()

    AT_PATTERN = re.compile(r"\[at[:：]\s*(all|\d{5,12})\s*\]", re.I)

    def _at_chain(self, text: str) -> MessageChain:
        """把 [at:123456] / [at：123456] 转成真正的 QQ At 组件。"""
        chain: list = []
        pos = 0
        for m in self.AT_PATTERN.finditer(text):
            if m.start() > pos:
                chain.append(Plain(text[pos : m.start()]))
            chain.append(At(qq=m.group(1)))
            pos = m.end()
        if pos < len(text):
            chain.append(Plain(text[pos:]))
        return MessageChain(chain or [Plain(text)])

    async def _at_hints(self, event: AstrMessageEvent) -> str:
        """从原消息里提取被 @ 的人，给 DSH 提供 QQ 号映射。"""
        found: list[tuple[str, str]] = []
        try:
            for seg in getattr(event.message_obj, "message", []) or []:
                if isinstance(seg, At):
                    qq = str(getattr(seg, "qq", "") or "").strip()
                    if qq and qq != "all":
                        found.append((qq, str(getattr(seg, "name", "") or "").strip()))
        except Exception:
            pass
        text = str(event.message_str or "")
        names = re.findall(r"@([^\s@]{1,20})", text)
        if names and event.get_group_id():
            try:
                members = await event.bot.call_action(
                    "get_group_member_list", group_id=int(event.get_group_id())
                )
                lookup = {}
                for m in members or []:
                    uid = str(m.get("user_id") or "")
                    for key in (m.get("card"), m.get("nickname")):
                        if key:
                            lookup[str(key).strip()] = uid
                for name in names:
                    if name in lookup:
                        found.append((lookup[name], name))
            except Exception:
                pass
        seen = set()
        lines = []
        for qq, name in found:
            key = (qq, name)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{name or '某人'}=QQ {qq}")
        if not lines:
            return ""
        return "[本机提示] 需要 at 时请输出 [at:QQ号]：" + "；".join(lines)

    def _is_short_plain(self, text: str) -> bool:
        """极短、单行、无 Markdown/JSON/代码样式的纯文本，才走文字消息。"""
        value = str(text or "").strip()
        limit = int(self._cfg("short_text_max_chars", 120) or 120)
        if not value or len(value) > limit:
            return False
        if "\n" in value or "\r" in value:
            return False
        if value.startswith(("{", "[", "```", "|", "#", ">", "- ", "* ")):
            return False
        if re.search(r"```|`|\*\*|__|~~|\|", value):
            return False
        if re.search(r"^\s*(#{1,6}\s|[-+*]\s|\d+\.\s|>\s)", value):
            return False
        return True

    async def _send_generated_files(self, umo: str, files) -> None:
        """把 DSH 本轮生成/修改的文件发到 QQ。"""
        if not files or not bool(self._cfg("send_generated_files", True)):
            return
        limit = int(self._cfg("max_generated_files", 5) or 5)
        for item in list(files)[:limit]:
            path = Path(str((item or {}).get("path") or ""))
            if not path.is_file():
                continue
            try:
                await self.context.send_message(
                    umo, MessageChain([File(name=path.name, file=str(path))])
                )
                logger.info(f"[{PLUGIN_NAME}] 已发送 DSH 生成文件: {path}")
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 发送文件失败 {path}: {exc}")

    def _write_reply_markdown(self, umo: str, text: str) -> Path:
        """把长回复写成 Markdown：元信息 + 正文，避免 JSON 转义影响阅读。"""
        base = _PLUGIN_DIR / "files"
        base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        path = base / f"dsh-reply-{stamp}.md"
        created = time.strftime("%Y-%m-%d %H:%M:%S")
        body = (
            "# DSH 回复\n\n"
            f"- 时间：{created}\n"
            f"- 会话：{umo}\n"
            f"- 字数：{len(text)}\n"
            "\n---\n\n"
            f"{text}\n"
        )
        path.write_text(body, encoding="utf-8")
        try:
            files = sorted(base.glob("dsh-reply-*.md"), key=lambda p: p.stat().st_mtime)
            for old in files[:-100]:
                old.unlink(missing_ok=True)
        except Exception:
            pass
        return path

    async def _send_text(self, umo: str, text: str) -> None:
        """短纯文本直接发；其余统一写 JSON 文件发送，避免截断和格式问题。"""
        value = str(text or "").strip()
        if not value:
            return
        # DSH 不知道 QQ 时会输出 [at：xxxxxxx]，只有一个目标时替换成真实 QQ
        if len(getattr(self, "_at_targets", [])) == 1:
            value = re.sub(
                r"\[at[:：]\s*x{3,}\s*\]",
                f"[at:{self._at_targets[0]}]",
                value,
                flags=re.I,
            )
        try:
            if self.AT_PATTERN.search(value):
                await self.context.send_message(umo, self._at_chain(value))
                logger.info(f"[{PLUGIN_NAME}] 已发送带 At 的文字: {value[:60]}")
                return
            if self._is_short_plain(value):
                await self.context.send_message(umo, MessageChain([Plain(value)]))
                logger.info(f"[{PLUGIN_NAME}] 已直接发送文字: {value[:60]}")
                return
            path = self._write_reply_markdown(umo, value)
            await self.context.send_message(
                umo, MessageChain([File(name=path.name, file=str(path))])
            )
            logger.info(
                f"[{PLUGIN_NAME}] 回复已写 Markdown 文件发送: {path.name} "
                f"({len(value)} 字, {path})"
            )
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 发送回复失败: {exc}")
            # 文件发送失败时兜底成一段文字，至少别丢内容
            try:
                await self.context.send_message(
                    umo, MessageChain([Plain(value[:1200])])
                )
            except Exception:
                pass

    # ------------------------------------------------------------- command
    async def terminate(self):
        """插件重载时取消后台推送任务，否则旧循环会继续发，造成重复消息。"""
        type(self)._notify_generation += 1
        task = self._notify_task
        self._notify_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        logger.info(f"[{PLUGIN_NAME}] 完成推送任务已停止")

    # ------------------------------------------------------- 完成后推送
    def _notify_umo(self) -> str:
        umo = str(self._cfg("notify_umo", "") or "").strip()
        if umo:
            return umo
        qq = str(self._cfg("notify_qq", "") or "").strip()
        platform = str(self._cfg("notify_platform", "") or "").strip() or "大肥鱼"
        if qq:
            return f"{platform}:FriendMessage:{qq}"
        return ""

    def _notify_state_path(self) -> Path:
        base = _PLUGIN_DIR / "files"
        base.mkdir(parents=True, exist_ok=True)
        return base / "notify_state.json"

    def _load_notify_state(self) -> dict[str, int]:
        path = self._notify_state_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("sessions") if isinstance(data, dict) else None
            return {str(k): int(v) for k, v in (raw or {}).items()}
        except Exception:
            return {}

    def _save_notify_state(self) -> None:
        try:
            self._notify_state_path().write_text(
                json.dumps({"sessions": self._notify_state}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 保存推送状态失败: {exc}")

    @staticmethod
    def _max_done_turn(events: list[dict]) -> int:
        turns = [
            int((e.get("data") or {}).get("turn") or 0)
            for e in events
            if e.get("type") == "turn/end"
        ]
        return max(turns, default=0)

    @staticmethod
    def _turn_assistant_text(events: list[dict], turn: int) -> str:
        parts: list[str] = []
        for event in events:
            if event.get("type") != "assistant/message":
                continue
            data = event.get("data") or {}
            if int(data.get("turn") or 0) != turn:
                continue
            message = data.get("message") or {}
            for item in message.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text") or "").strip()
                    if text:
                        parts.append(text)
        return "\n".join(parts).strip()

    def _write_notify_markdown(self, session_id: str, turn: int, text: str) -> Path:
        base = _PLUGIN_DIR / "files"
        base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        path = base / f"dsh-notify-{stamp}.md"
        body = (
            "# DSH 任务完成\n\n"
            f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 会话：{session_id}\n"
            f"- 轮次：{turn}\n"
            f"- 字数：{len(text)}\n"
            "\n---\n\n"
            f"{text}\n"
        )
        path.write_text(body, encoding="utf-8")
        return path

    async def _notify_turn(self, target_umo: str, session_id: str, turn: int, text: str) -> None:
        try:
            await self.context.send_message(
                target_umo,
                MessageChain([Plain(f"DSH 任务完成：{session_id} 第 {turn} 轮")]),
            )
            path = self._write_notify_markdown(session_id, turn, text)
            await self.context.send_message(
                target_umo, MessageChain([File(name=path.name, file=str(path))])
            )
            if bool(self._cfg("send_screenshot", True)) and win_ui is not None:
                try:
                    hwnd = win_ui.find_window(str(self._cfg("window_keyword", "DeepSeek Harness")))
                    if hwnd:
                        shot = _PLUGIN_DIR / "shots" / f"dsh-notify-{int(time.time())}.png"
                        img_path = win_ui.capture_window(hwnd, shot)
                        await self.context.send_message(
                            target_umo, MessageChain([Image.fromFileSystem(img_path)])
                        )
                except Exception as exc:
                    logger.warning(f"[{PLUGIN_NAME}] 推送截图失败: {exc}")
            logger.info(
                f"[{PLUGIN_NAME}] 已推送 DSH 完成通知: session={session_id} turn={turn} "
                f"chars={len(text)} -> {target_umo}"
            )
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 推送 DSH 完成通知失败: {exc}")

    async def _notify_loop(self) -> None:
        poll = max(5.0, float(self._cfg("notify_poll_seconds", 15) or 15))
        await asyncio.sleep(min(5.0, poll))
        while True:
            if getattr(self, "_notify_gen", 0) != type(self)._notify_generation:
                logger.info(f"[{PLUGIN_NAME}] 旧推送循环退出")
                return
            try:
                target = self._notify_umo()
                if not target:
                    await asyncio.sleep(poll)
                    continue
                root = self._dsh_home() / "sessions"
                if not root.is_dir():
                    await asyncio.sleep(poll)
                    continue
                now = time.time()
                scan_hours = float(self._cfg("notify_scan_hours", 48) or 48)
                for path in root.glob(f"**/{SESSION_FILE}"):
                    try:
                        if now - path.stat().st_mtime > scan_hours * 3600:
                            continue
                    except OSError:
                        continue
                    events = self._read_events(path, force=True)
                    if not events:
                        continue
                    key = str(path)
                    done = self._max_done_turn(events)
                    last = int(self._notify_state.get(key, 0) or 0)
                    if key not in self._notify_state:
                        # 首次见到：以当前已完成轮次为基线，不回放历史
                        self._notify_state[key] = done
                        self._save_notify_state()
                        continue
                    if done <= last:
                        continue
                    session_id = path.parent.name
                    for turn in range(last + 1, done + 1):
                        text = self._turn_assistant_text(events, turn)
                        if not text:
                            continue
                        # 通过 QQ 调用的轮次由命令链路自己发，这里不重复推
                        if self._busy or self._sent_turns.get(session_id) == turn:
                            continue
                        if int(self._notify_state.get(key, 0) or 0) >= turn:
                            continue
                        await self._notify_turn(target, session_id, turn, text)
                        self._notify_state[key] = turn
                        self._save_notify_state()
                    self._notify_state[key] = done
                    self._save_notify_state()
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 完成推送循环异常: {exc}")
            await asyncio.sleep(poll)

    # ------------------------------------------------------- 会话管理
    def _override_path(self) -> Path:
        base = _PLUGIN_DIR / "files"
        base.mkdir(parents=True, exist_ok=True)
        return base / "session_override.json"

    def _load_session_override(self) -> str:
        try:
            data = json.loads(self._override_path().read_text(encoding="utf-8"))
            return str(data.get("session_id") or "").strip()
        except Exception:
            return ""

    def _save_session_override(self, session_id: str) -> None:
        self._session_override = session_id
        try:
            self._override_path().write_text(
                json.dumps({"session_id": session_id}, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 保存会话选择失败: {exc}")

    def _list_sessions_disk(self) -> list[dict]:
        root = self._dsh_home() / "sessions"
        result: list[dict] = []
        if not root.is_dir():
            return result
        for path in root.glob(f"**/{SESSION_FILE}"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            title = ""
            events = self._read_events(path, force=True)
            for event in reversed(events):
                if event.get("type") == "session/title":
                    title = str((event.get("data") or {}).get("title") or "").strip()
                    break
            if not title:
                for event in events:
                    if event.get("type") != "user/message":
                        continue
                    msg = (event.get("data") or {}).get("message") or {}
                    for item in msg.get("content") or []:
                        if isinstance(item, dict) and item.get("type") == "text":
                            title = str(item.get("text") or "").strip().replace("\n", " ")
                            break
                    if title:
                        break
            done_turn = 0
            for event in reversed(events):
                if event.get("type") == "turn/end":
                    try:
                        done_turn = int((event.get("data") or {}).get("turn") or 0)
                    except Exception:
                        done_turn = 0
                    break
            # 新建后还没用过的空会话不列出，避免一堆“无标题”
            if not title and done_turn == 0:
                continue
            if not title:
                title = f"未命名会话（{path.parent.name[-6:]}）"
            result.append(
                {
                    "id": path.parent.name,
                    "title": (title[:40] + "…") if len(title) > 40 else title,
                    "mtime": mtime,
                }
            )
        result.sort(key=lambda x: x["mtime"], reverse=True)
        return result

    def _http_json(self, path: str, payload: dict, timeout: float = 30) -> dict:
        import urllib.error
        import urllib.request

        base = str(self._cfg("bridge_url", "http://127.0.0.1:5795/prompt") or "")
        base = base.rsplit("/", 1)[0]
        url = f"{base}/{path.lstrip('/')}"
        secret = str(self._cfg("bridge_secret", "") or "").strip()
        data = json.dumps(payload, ensure_ascii=False).encode()

        def _do() -> dict:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json", "x-bridge-secret": secret},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", "ignore") or "{}")
            except urllib.error.HTTPError as exc:
                try:
                    return json.loads(exc.read().decode("utf-8", "ignore") or "{}")
                except Exception:
                    return {"ok": False, "error": f"HTTP {exc.code}"}

        return _do()

    async def _handle_subcommand(self, prompt: str, event: AstrMessageEvent) -> bool:
        """处理 /dsh 会话/切换/新建/模型/推理；返回 True 表示已处理。"""
        if not prompt:
            return False
        parts = prompt.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        umo = event.unified_msg_origin
        if cmd not in {"会话", "列表", "sessions", "切换", "新建", "模型", "推理", "停止"}:
            return False

        if cmd in {"会话", "列表", "sessions"}:
            items = await asyncio.to_thread(self._list_sessions_disk)
            if not items:
                await self._send_text(umo, "没找到 DSH 会话记录")
                return True
            current = self._session_override or self._bridge_session_id()
            lines = [f"DSH 会话共 {len(items)} 个（最近 20 个）："]
            for idx, item in enumerate(items[:20], 1):
                mark = " ← 当前" if item["id"] == current else ""
                lines.append(f"{idx}. {item['title']}{mark}")
            lines.append("切换：/dsh 切换 <序号或 session-id>")
            await self._send_text(umo, "\n".join(lines))
            return True

        if cmd == "切换":
            if not arg:
                await self._send_text(umo, "用法：/dsh 切换 <序号或 session-id>")
                return True
            items = await asyncio.to_thread(self._list_sessions_disk)
            target = ""
            if arg.isdigit() and 1 <= int(arg) <= len(items):
                target = items[int(arg) - 1]["id"]
            elif arg.startswith("session-") or re.match(r"^[0-9a-f-]{8,}$", arg):
                target = arg
            else:
                await self._send_text(umo, "序号超出范围，先发 /dsh 会话 看列表")
                return True
            self._save_session_override(target)
            await self._send_text(umo, f"已切换转发目标到 {target}")
            return True

        if cmd == "新建":
            if self._busy:
                await self._send_text(umo, "正在跑任务，等它完再新建")
                return True
            result = await asyncio.to_thread(self._http_json, "session/new", {})
            sid = str(result.get("sessionId") or "")
            if not sid:
                await self._send_text(umo, f"新建失败：{result.get('error') or result}")
                return True
            self._save_session_override(sid)
            await self._send_text(
                umo,
                f"已在后台新建会话并切换转发目标：{sid}；DSH 窗口不会自动跳转，可在左侧会话列表手动打开它",
            )
            return True

        if cmd == "停止":
            target = self._session_override or self._bridge_session_id()
            if not target:
                await self._send_text(umo, "没有可停止的会话，先发 /dsh 会话 或 /dsh 新建")
                return True
            if not self._busy and not self._pending_question:
                await self._send_text(umo, "当前没有正在执行的任务")
                return True
            result = await asyncio.to_thread(
                self._http_json, "stop", {"sessionId": target}
            )
            self._busy = False
            self._busy_since = 0.0
            self._pending_question = None
            if result.get("ok"):
                await self._send_text(umo, "已发送停止指令，等 DSH 收尾")
            elif str(result.get("error") or "") == "not-running":
                await self._send_text(umo, "DSH 当前没有在跑任务")
            else:
                await self._send_text(umo, f"停止失败：{result.get('error') or result}")
            return True

        if cmd in {"模型", "推理"}:
            if self._busy:
                await self._send_text(umo, "正在跑任务，等它完再改")
                return True
            target = self._session_override or self._bridge_session_id()
            if not target:
                await self._send_text(umo, "没有可用会话，先发 /dsh 会话 或 /dsh 新建")
                return True
            payload: dict = {"sessionId": target}
            if cmd == "模型":
                if not arg:
                    await self._send_text(umo, "用法：/dsh 模型 <模型名>")
                    return True
                payload["model"] = arg
            else:
                if arg not in {"low", "medium", "high"}:
                    await self._send_text(umo, "推理强度可选：low / medium / high")
                    return True
                payload["reasoningEffort"] = arg
            result = await asyncio.to_thread(self._http_json, "model", payload)
            if result.get("ok"):
                await self._send_text(umo, f"已切换 {cmd}：{json.dumps(result.get('selection'), ensure_ascii=False)}")
            else:
                await self._send_text(umo, f"切换失败：{result.get('error') or result}")
            return True

        return False

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10 ** 9 - 100)
    async def on_question_answer(self, event: AstrMessageEvent):
        pending = self._pending_question
        if not pending:
            return
        if time.time() - float(pending.get("ts") or 0) > 1800:
            self._pending_question = None
            self._busy = False
            self._busy_since = 0.0
            return
        if str(event.unified_msg_origin or "") != str(pending.get("umo") or ""):
            return
        if not self._is_admin(event):
            return
        text = str(event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        self._pending_question = None
        event.stop_event()
        await self.context.send_message(
            event.unified_msg_origin, MessageChain([Plain("收到，继续执行…")])
        )
        task = asyncio.create_task(self._answer_question(pending, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer_question(self, pending: dict, answer: str) -> None:
        session_id = str(pending.get("sessionId") or "")
        umo = str(pending.get("umo") or "")
        question = pending.get("question") or {}
        ids = [str(q.get("id") or "") for q in question.get("questions") or []]
        answers = [{"id": qid, "selected": [], "custom": answer} for qid in ids if qid] or answer
        payload = {
            "sessionId": session_id,
            "answers": answers,
            "timeoutMs": int(float(self._cfg("timeout_seconds", 1500) or 1500) * 1000),
        }
        timeout = float(self._cfg("timeout_seconds", 1500) or 1500) + 60
        try:
            result = await asyncio.to_thread(self._http_json, "answer", payload, timeout)
        except Exception as exc:
            self._busy = False
            self._busy_since = 0.0
            await self._send_text(umo, f"提交答案失败：{exc}")
            return
        try:
            if not result.get("ok"):
                err = str(result.get("error") or "unknown")
                if err == "cancelled":
                    await self._send_text(umo, "任务已被手动终止")
                elif err == "timeout":
                    await self._send_text(umo, "DSH 执行超时，已中止收集")
                else:
                    await self._send_text(umo, f"继续执行失败：{err}")
                return
            text = str(result.get("text") or "").strip()
            if not text:
                text = "（执行完了，但 DSH 没返回文本）"
            await self._send_text(umo, text)
            await self._send_generated_files(umo, result.get("files"))
            if bool(self._cfg("send_screenshot", True)) and win_ui is not None:
                try:
                    hwnd = win_ui.find_window(str(self._cfg("window_keyword", "DeepSeek Harness")))
                    if hwnd:
                        shot = _PLUGIN_DIR / "shots" / f"dsh_{int(time.time())}.png"
                        img_path = win_ui.capture_window(hwnd, shot)
                        await self.context.send_message(
                            umo, MessageChain([Image.fromFileSystem(img_path)])
                        )
                except Exception as exc:
                    logger.warning(f"[{PLUGIN_NAME}] 截图失败: {exc}")
        finally:
            self._busy = False
            self._busy_since = 0.0

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10 ** 9 - 600)
    async def on_dsh_command(self, event: AstrMessageEvent):
        try:
            if not bool(self._cfg("enabled", True)):
                return
            if not self._is_admin(event):
                return
            text = str(event.message_str or "").strip()
            if not text:
                return
            # 子命令兜底：不依赖 prefixes 配置，/dsh会话、dsh 新建 等都能命中
            sub_match = re.match(
                r"^(?:@\S+\s*)*[/／]?(?:调用dsh|dsh)\s*(会话|列表|sessions|切换|新建|模型|推理|停止)"
                r"(?:\s+([\s\S]+))?$",
                text,
                re.I,
            )
            if sub_match:
                cmd = sub_match.group(1)
                arg = (sub_match.group(2) or "").strip()
                logger.info(f"[{PLUGIN_NAME}] 子命令命中: {cmd} {arg[:30]}")
                try:
                    await self._handle_subcommand(f"{cmd} {arg}".strip(), event)
                except Exception as exc:
                    logger.error(f"[{PLUGIN_NAME}] 子命令处理失败: {exc}", exc_info=True)
                    await self._send_text(event.unified_msg_origin, f"命令执行失败：{exc}")
                event.stop_event()
                return
            prefixes = self._cfg("prefixes", ["/调用dsh", "/dsh"]) or []
            matched = None
            for prefix in prefixes:
                prefix = str(prefix or "").strip()
                if prefix and text.lower().startswith(prefix.lower()):
                    matched = text[len(prefix) :].strip()
                    break
            if matched is None:
                # 容忍 /调用dsh 前面带 @ 或空白
                m = re.match(r"^(?:@\S+\s*)*[/／]?(?:调用dsh|dsh)\s+([\s\S]+)$", text, re.I)
                if not m:
                    return
                matched = m.group(1).strip()
            prompt = matched.strip(" \t\r\n:：")
            logger.info(f"[{PLUGIN_NAME}] 指令命中: {prompt[:40]}")
            try:
                if await self._handle_subcommand(prompt, event):
                    event.stop_event()
                    return
            except Exception as exc:
                logger.error(f"[{PLUGIN_NAME}] 子命令处理失败: {exc}", exc_info=True)
                await self._send_text(
                    event.unified_msg_origin, f"命令执行失败：{exc}"
                )
                event.stop_event()
                return
            if not prompt:
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([Plain("用法：/调用dsh <要让它做的事>")]),
                )
                event.stop_event()
                return
            if self._busy:
                elapsed = time.time() - float(self._busy_since or 0)
                limit = float(self._cfg("timeout_seconds", 1500) or 1500) + 180
                if elapsed > limit:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 忙锁已 {int(elapsed)}s，超过上限 {int(limit)}s，强制释放"
                    )
                    self._busy = False
                    self._busy_since = 0.0
                else:
                    minutes = int(elapsed // 60)
                    suffix = f"（已 {minutes} 分钟）" if minutes >= 1 else ""
                    await self.context.send_message(
                        event.unified_msg_origin,
                        MessageChain(
                            [Plain(f"上一个 dsh 任务还在跑{suffix}，等它完")]
                        ),
                    )
                    event.stop_event()
                    return
            umo = event.unified_msg_origin
            event.stop_event()
            await self.context.send_message(
                umo, MessageChain([Plain("收到，丢给 dsh 了")])
            )
            at_hint = await self._at_hints(event)
            self._at_targets = re.findall(r"QQ (\d+)", at_hint)
            if at_hint:
                prompt = prompt + chr(10) + chr(10) + at_hint
            logger.info(f"[{PLUGIN_NAME}] 转发到 DSH: {prompt[:60]}")
            self._busy = True
            self._busy_since = time.time()
            task = asyncio.create_task(self._run(prompt, umo))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 指令处理失败: {exc}")

    def _bridge_session_id(self) -> str:
        sid = str(self._cfg("session_id", "") or "").strip()
        if sid:
            return sid
        path = self._session_path()
        return path.parent.name if path is not None else ""

    async def _run_http(self, prompt: str, umo: str) -> None:
        import urllib.error
        import urllib.request

        url = str(self._cfg("bridge_url", "http://127.0.0.1:5795/prompt") or "").strip()
        secret = str(self._cfg("bridge_secret", "") or "").strip()
        if not secret:
            await self._send_text(
                umo,
                "还没配置 bridge_secret：DSH 端和 bot 端必须填同一个密钥（见 info.json）",
            )
            return
        session_id = self._bridge_session_id()
        if not session_id:
            await self._send_text(umo, "没找到当前 DSH 会话 id，请在配置里填 session_id")
            return
        timeout = float(self._cfg("timeout_seconds", 1500) or 1500)
        body = json.dumps(
            {"prompt": prompt, "sessionId": session_id, "timeoutMs": int(timeout * 1000)},
            ensure_ascii=False,
        ).encode()
        headers = {"Content-Type": "application/json", "x-bridge-secret": secret}
        timeout = timeout + 30

        def _post() -> dict:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", "ignore") or "{}")
            except urllib.error.HTTPError as exc:
                # 409 / 504 也是 JSON，要读出来按业务错误处理，不能当网络异常
                try:
                    return json.loads(exc.read().decode("utf-8", "ignore") or "{}")
                except Exception:
                    return {"ok": False, "error": f"HTTP {exc.code}"}

        logger.info(
            f"[{PLUGIN_NAME}] HTTP 等待 DSH: session={session_id} timeout={int(timeout)}s"
        )
        try:
            # 关键：urlopen 是阻塞调用，必须丢到线程里，否则会卡死 AstrBot 事件循环
            payload = await asyncio.to_thread(_post)
        except Exception as exc:
            detail = str(exc)
            if "Connection refused" in detail or "10061" in detail:
                detail = "DSH 桥没在监听（检查端口/DSH 是否启动/插件是否加载）"
            logger.error(f"[{PLUGIN_NAME}] 调 DSH 桥失败: {detail[:300]}")
            await self._send_text(umo, f"调 DSH 桥失败：{detail[:300]}")
            return
        if not isinstance(payload, dict):
            payload = {"ok": False, "error": "invalid-response"}
        logger.info(f"[{PLUGIN_NAME}] HTTP 返回: ok={payload.get('ok')}")
        try:
            if payload.get("ok") and payload.get("turn"):
                self._sent_turns[session_id] = int(payload["turn"])
        except Exception:
            pass
        question = payload.get("question")
        if payload.get("ok") and isinstance(question, dict):
            self._pending_question = {
                "sessionId": session_id,
                "umo": umo,
                "question": question,
                "ts": time.time(),
            }
            lines = ["DSH 在等你回答，直接发消息即可："]
            for idx, item in enumerate(question.get("questions") or [], 1):
                header = str(item.get("header") or "").strip()
                body = str(item.get("question") or "").strip()
                opts = item.get("options") or []
                line = f"{idx}. {header + '：' if header else ''}{body}"
                if opts:
                    line += "（可选：" + " / ".join(str(o) for o in opts) + "）"
                lines.append(line)
            await self._send_text(umo, "\n".join(lines))
            return
        if not payload.get("ok"):
            err = str(payload.get("error") or "unknown")
            mapping = {
                "session-not-live": "这个会话不在 DSH 进程里活着（桌面里没开着它）",
                "bad secret": "共享密钥不对（DSH 和 bot 两边要一致）",
                "cancelled": "任务已被手动终止，未发送完成通知和截图",
                "session-busy": "这个会话正在执行上一条，等它跑完再发",
                "timeout": "DSH 执行超过等待上限，已中止收集",
            }
            err = mapping.get(err, err)
            partial = str(payload.get("partial") or "").strip()
            if partial:
                await self._send_text(umo, f"{err}\n\n已产生的部分输出：\n{partial}")
            else:
                await self._send_text(umo, f"DSH 桥拒绝：{err}")
            return
        text = str(payload.get("text") or "").strip()
        if not text:
            text = "（执行完了，但 DSH 没返回文本）"
        await self._send_text(umo, text)
        await self._send_generated_files(umo, payload.get("files"))
        if bool(self._cfg("send_screenshot", True)) and win_ui is not None:
            try:
                hwnd = win_ui.find_window(str(self._cfg("window_keyword", "DeepSeek Harness")))
                if hwnd:
                    shot = _PLUGIN_DIR / "shots" / f"dsh_{int(time.time())}.png"
                    path = win_ui.capture_window(hwnd, shot)
                    await self.context.send_message(umo, MessageChain([Image.fromFileSystem(path)]))
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 截图失败: {exc}")

    async def _run_acp(self, prompt: str, umo: str) -> None:
        import acp_client as acp

        session_id = str(self._cfg("session_id", "") or "").strip()
        cwd = str(self._cfg("acp_cwd", "") or "").strip() or str(Path.home())
        timeout = float(self._cfg("timeout_seconds", 1500) or 1500)
        try:
            answer = await asyncio.to_thread(acp.run_prompt, session_id, prompt, cwd, timeout)
        except Exception as exc:
            detail = str(exc)
            if "active write handle" in detail or "already active" in detail:
                detail = "这个会话正在 DSH 桌面里开着，ACP 拿不到写锁；换一个空闲会话（填 session_id）或改回 window 后端"
            await self._send_text(umo, f"ACP 调用失败：{detail[:300]}")
            return
        if not answer:
            answer = "（执行完了，但 ACP 没返回文本）"
        await self._send_text(umo, answer)
        if bool(self._cfg("send_screenshot", True)):
            try:
                hwnd = win_ui.find_window(str(self._cfg("window_keyword", "DeepSeek Harness")))
                if hwnd:
                    shot = _PLUGIN_DIR / "shots" / f"dsh_{int(time.time())}.png"
                    path = win_ui.capture_window(hwnd, shot)
                    await self.context.send_message(umo, MessageChain([Image.fromFileSystem(path)]))
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 截图失败: {exc}")

    # ------------------------------------------------------------- worker
    async def _run(self, prompt: str, umo: str) -> None:
        self._busy = True
        try:
            backend = str(self._cfg("backend", "http") or "http").strip().lower()
            if backend == "window" and win_ui is None:
                await self._send_text(umo, "窗口后端需要 pywin32：pip install pywin32")
                return
            if backend == "http":
                await self._run_http(prompt, umo)
                return
            if backend == "acp":
                await self._run_acp(prompt, umo)
                return
            session = self._session_path()
            if session is None:
                await self._send_text(umo, "没找到 DSH 会话记录，检查 DSH_HOME / session_id")
                return
            before = self._read_events(session, force=True)
            pre_turn = self._last_turn(before)
            pre_seq = max((int(e.get("seq") or 0) for e in before), default=0)

            hwnd = win_ui.find_window(str(self._cfg("window_keyword", "DeepSeek Harness")))
            if not hwnd:
                await self._send_text(umo, "没找到 DeepSeek Harness 窗口，先把它打开")
                return
            win_ui.focus_window(hwnd)
            await asyncio.sleep(0.4)
            win_ui.click_ratio(
                hwnd,
                float(self._cfg("input_x_ratio", 0.5) or 0.5),
                float(self._cfg("input_y_ratio", 0.94) or 0.94),
            )
            win_ui.set_clipboard(prompt)
            win_ui.press_ctrl_v()
            await asyncio.sleep(0.3)
            win_ui.press_enter()
            logger.info(f"[{PLUGIN_NAME}] 已粘贴并回车，等待 turn>{pre_turn}")

            timeout = float(self._cfg("timeout_seconds", 1500) or 1500)
            poll = max(1.0, float(self._cfg("poll_interval", 2.0) or 2.0))
            deadline = time.time() + timeout
            done_turn = 0
            while time.time() < deadline:
                await asyncio.sleep(poll)
                events = self._read_events(session, force=True)
                for event in reversed(events):
                    if event.get("type") != "turn/end":
                        continue
                    turn = int((event.get("data") or {}).get("turn") or 0)
                    if turn > pre_turn:
                        done_turn = turn
                    break
                if done_turn:
                    break
            if not done_turn:
                await self._send_text(umo, f"等了 {int(timeout)} 秒还没跑完，可能还在执行或卡住了")
                return

            events = self._read_events(session, force=True)
            answer = self._assistant_text(events, done_turn, pre_seq)
            if not answer:
                answer = self._assistant_text(events, 0, pre_seq)
            if not answer:
                answer = "（执行完了，但会话记录里没找到文本回复）"
            await self._send_text(umo, answer)

            if bool(self._cfg("send_screenshot", True)) and win_ui is not None:
                try:
                    shot = _PLUGIN_DIR / "shots" / f"dsh_{int(time.time())}.png"
                    path = win_ui.capture_window(hwnd, shot)
                    await self.context.send_message(
                        umo, MessageChain([Image.fromFileSystem(path)])
                    )
                except Exception as exc:
                    logger.warning(f"[{PLUGIN_NAME}] 截图发送失败: {exc}")
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 执行失败: {exc}")
            try:
                await self._send_text(umo, f"转给 dsh 失败：{exc}")
            except Exception:
                pass
        finally:
            if not self._pending_question:
                self._busy = False
                self._busy_since = 0.0
