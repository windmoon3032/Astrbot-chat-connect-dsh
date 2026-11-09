# -*- coding: utf-8 -*-
"""Windows 窗口/输入/截图小工具，供 DSH 桥插件使用。"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from pathlib import Path

import win32api
import win32clipboard
import win32con
import win32gui


def find_window(keyword: str, exclude: str = "") -> int | None:
    """按标题关键字找可见顶层窗口，返回 hwnd。"""
    found: list[tuple[int, str]] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        if keyword and keyword in title and (not exclude or exclude not in title):
            found.append((hwnd, title))
        return True

    win32gui.EnumWindows(_cb, None)
    if not found:
        return None
    # 取面积最大的那个
    def _area(item):
        try:
            l, t, r, b = win32gui.GetWindowRect(item[0])
            return (r - l) * (b - t)
        except Exception:
            return 0

    found.sort(key=_area, reverse=True)
    return found[0][0]


def focus_window(hwnd: int) -> None:
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # SetForegroundWindow 偶发被前台锁拦，退化为点击标题栏
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            win32api.SetCursorPos(((l + r) // 2, t + 15))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        except Exception:
            pass
    time.sleep(0.4)


def click_ratio(hwnd: int, rx: float, ry: float) -> None:
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    x = int(l + (r - l) * rx)
    y = int(t + (b - t) * ry)
    win32api.SetCursorPos((x, y))
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.2)


def set_clipboard(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def press_ctrl_v() -> None:
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("V"), 0, 0, 0)
    time.sleep(0.08)
    win32api.keybd_event(ord("V"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.2)


def press_enter() -> None:
    win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)


def capture_window(hwnd: int, out_path: str | Path) -> str:
    """截整窗（用屏幕区域截，窗口需可见），返回路径。"""
    from PIL import ImageGrab

    l, t, r, b = win32gui.GetWindowRect(hwnd)
    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return str(out)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    return win32gui.GetWindowRect(hwnd)
