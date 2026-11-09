# -*- coding: utf-8 -*-
"""最小 ACP 客户端：通过 `dsh --profile acp` 的 stdio JSON-RPC 跑一个 prompt。

ACP 是 DSH 官方暴露的自动化接口（initialize / session/resume / session/prompt），
不依赖窗口，比 UI 自动化稳定。注意：桌面端正开着的会话有写锁，
resume 会报 "already owned by an active write handle"。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

class AcpError(Exception):
    pass


def _dsh_path() -> str:
    """按 环境变量 → PATH → 常见安装目录 的顺序找 dsh 启动器。"""
    env = os.environ.get("DSH_CLI")
    if env and Path(env).exists():
        return env
    found = shutil.which("dsh") or shutil.which("dsh.cmd")
    if found:
        return found
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    tails = [
        r"Deepseek\resources\runtime\cli\bin\dsh.cmd",
        r"Deepseek\resources\runtime\cli\bin\dsh.exe",
        r"DeepSeek\resources\runtime\cli\bin\dsh.cmd",
    ]
    for root in roots:
        if not root:
            continue
        for tail in tails:
            candidate = Path(root) / tail
            if candidate.exists():
                return str(candidate)
    return "dsh"


class AcpSession:
    def __init__(self, cwd: str, timeout: float = 600):
        self.cwd = cwd
        self.timeout = timeout
        self.proc: subprocess.Popen | None = None
        self._stderr: list[str] = []

    def __enter__(self):
        dsh = _dsh_path()
        cmd = ["cmd.exe", "/c", dsh, "--profile", "acp"] if dsh.lower().endswith(".cmd") else [dsh, "--profile", "acp"]
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def _err():
            assert self.proc is not None and self.proc.stderr is not None
            for line in self.proc.stderr:
                text = line.decode("utf-8", "ignore").strip()
                if text:
                    self._stderr.append(text)
                    if len(self._stderr) > 50:
                        self._stderr.pop(0)

        threading.Thread(target=_err, daemon=True).start()
        return self

    def __exit__(self, *exc):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _send(self, obj: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _read(self, deadline: float) -> dict | None:
        assert self.proc is not None and self.proc.stdout is not None
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                return None
            text = line.decode("utf-8", "ignore").strip()
            if not text:
                continue
            try:
                return json.loads(text)
            except Exception:
                continue
        return None

    def _wait_id(self, msg_id: int, timeout: float) -> dict:
        deadline = time.time() + timeout
        while True:
            msg = self._read(deadline)
            if msg is None:
                raise AcpError("ACP 进程无响应或超时")
            if msg.get("id") == msg_id:
                return msg

    # ------------------------------------------------------------------
    def initialize(self) -> None:
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": 1, "clientCapabilities": {}}})
        result = self._wait_id(1, 30)
        if "error" in result:
            raise AcpError(f"initialize 失败: {result['error']}")

    def new_session(self) -> str:
        self._send({"jsonrpc": "2.0", "id": 4, "method": "session/new",
                    "params": {"cwd": self.cwd, "mcpServers": []}})
        result = self._wait_id(4, 90)
        if "error" in result:
            raise AcpError(str(result["error"].get("message") or result["error"]))
        sid = str((result.get("result") or {}).get("sessionId") or "")
        if not sid:
            raise AcpError("session/new 没返回 sessionId")
        return sid

    def resume(self, session_id: str) -> None:
        self._send({"jsonrpc": "2.0", "id": 2, "method": "session/resume",
                    "params": {"sessionId": session_id, "cwd": self.cwd, "mcpServers": []}})
        result = self._wait_id(2, 90)
        if "error" in result:
            detail = result["error"].get("data", {}).get("details") or result["error"].get("message")
            raise AcpError(str(detail))

    def prompt(self, session_id: str, text: str) -> str:
        self._send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                    "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}})
        deadline = time.time() + self.timeout
        parts: list[str] = []
        while True:
            msg = self._read(deadline)
            if msg is None:
                raise AcpError("等待 ACP 回复超时")
            if msg.get("id") == 3:
                if "error" in msg:
                    raise AcpError(str(msg["error"].get("message") or msg["error"]))
                return "".join(parts).strip()
            if msg.get("method") == "session/update":
                upd = (msg.get("params") or {}).get("update") or {}
                kind = upd.get("sessionUpdate") or upd.get("type") or ""
                content = upd.get("content")
                if kind in ("agent_message_chunk", "agent_message"):
                    if isinstance(content, dict) and content.get("type") == "text":
                        parts.append(str(content.get("text") or ""))
                    elif isinstance(content, str):
                        parts.append(content)


def run_prompt(session_id: str, text: str, cwd: str, timeout: float = 600) -> str:
    with AcpSession(cwd, timeout=timeout) as session:
        session.initialize()
        sid = session_id or session.new_session()
        if session_id:
            session.resume(session_id)
        return session.prompt(sid, text)
