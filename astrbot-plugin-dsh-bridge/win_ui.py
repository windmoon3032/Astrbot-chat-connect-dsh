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


def _process_exe(pid: int) -> str:
    """用 QueryFullProcessImageNameW 取进程完整路径（不依赖 psutil）。"""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(2048)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value or ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def find_window(keyword: str, exclude: str = "") -> int | None:
    """按标题关键字找 DSH 窗口。

    只靠标题会误伤浏览器（例如 GitHub 仓库页标题里含仓库描述
    "DeepSeek Harness"），所以再按进程名/标题后缀筛一遍。
    """
    import win32process

    keyword_lower = keyword.lower()
    window_matches: list[tuple[int, str, bool]] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        if not keyword or keyword not in title or (exclude and exclude in title):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe = _process_exe(pid).lower()
        except Exception:
            exe = ""
        is_dsh_exe = "deepseek" in exe and "harness" in exe
        title_tail = title.strip().lower().endswith(keyword_lower) or title.strip().lower().endswith(
            f"— {keyword_lower}"
        ) or title.strip().lower().endswith(f"- {keyword_lower}")
        window_matches.append((hwnd, title, is_dsh_exe or title_tail))
        return True

    win32gui.EnumWindows(_cb, None)
    if not window_matches:
        return None
    preferred = [item for item in window_matches if item[2]] or window_matches

    def _area(item):
        try:
            l, t, r, b = win32gui.GetWindowRect(item[0])
            return (r - l) * (b - t)
        except Exception:
            return 0

    preferred.sort(key=_area, reverse=True)
    return preferred[0][0]


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


def _print_window(hwnd: int):
    """用 PrintWindow 抓窗口自身内容，返回 PIL.Image 或 None。

    被别的窗口挡住也能抓到（PW_RENDERFULLCONTENT=2），
    这是和 ImageGrab 按屏幕区域截图的本质区别。
    """
    import win32ui
    from PIL import Image

    l, t, r, b = win32gui.GetWindowRect(hwnd)
    width, height = max(1, r - l), max(1, b - t)
    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    bitmap = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        if not ok:
            return None
        raw = bitmap.GetBitmapBits(True)
        return Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
    except Exception:
        return None
    finally:
        try:
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
        except Exception:
            pass
        try:
            if save_dc is not None:
                save_dc.DeleteDC()
        except Exception:
            pass
        try:
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            if hwnd_dc is not None:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def _looks_blank(img) -> bool:
    try:
        gray = img.convert("L")
        lo, hi = gray.getextrema()
        return hi - lo < 8
    except Exception:
        return True


def capture_window(hwnd: int, out_path: str | Path) -> str:
    """截 DSH 窗口本身：优先 PrintWindow（被遮挡也能截），失败再切前台截屏。"""
    from PIL import ImageGrab

    if win32gui.IsIconic(hwnd):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)
        except Exception:
            pass

    img = _print_window(hwnd)
    if img is None or _looks_blank(img):
        # 个别 Electron/GPU 窗口 PrintWindow 会返回黑图，退回“切前台+屏幕区域”
        try:
            focus_window(hwnd)
            time.sleep(0.2)
        except Exception:
            pass
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return str(out)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    return win32gui.GetWindowRect(hwnd)
