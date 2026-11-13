"""Screen capture for the region picker and the file clipboard, with nothing
but the standard library (ctypes). Pixels are top-down BGRA bytes, which is
what GDI hands back."""

import ctypes
import os
import struct
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

for fn, res, args in [
    (user32.GetDC, wintypes.HDC, [wintypes.HWND]),
    (user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, wintypes.HDC]),
    (gdi32.CreateCompatibleDC, wintypes.HDC, [wintypes.HDC]),
    (gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, [wintypes.HDC, ctypes.c_int, ctypes.c_int]),
    (gdi32.SelectObject, wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]),
    (gdi32.BitBlt, wintypes.BOOL, [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                   wintypes.DWORD]),
    (gdi32.GetDIBits, ctypes.c_int, [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                     ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]),
    (gdi32.DeleteObject, wintypes.BOOL, [wintypes.HGDIOBJ]),
    (gdi32.DeleteDC, wintypes.BOOL, [wintypes.HDC]),
    (user32.OpenClipboard, wintypes.BOOL, [wintypes.HWND]),
    (user32.SetClipboardData, wintypes.HANDLE, [wintypes.UINT, wintypes.HANDLE]),
    (kernel32.GlobalAlloc, wintypes.HGLOBAL, [wintypes.UINT, ctypes.c_size_t]),
    (kernel32.GlobalLock, ctypes.c_void_p, [wintypes.HGLOBAL]),
    (kernel32.GlobalUnlock, wintypes.BOOL, [wintypes.HGLOBAL]),
    (kernel32.GlobalFree, wintypes.HGLOBAL, [wintypes.HGLOBAL]),
]:
    fn.restype, fn.argtypes = res, args

SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
CF_HDROP = 15


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def dpi_aware():
    """Physical pixels everywhere. Call before creating any window."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            user32.SetProcessDPIAware()


def virtual_screen():
    """(x, y, width, height) of all monitors together."""
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def capture(x, y, w, h):
    """BGRA bytes (top-down) of a screen rectangle."""
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.BitBlt(mem, 0, 0, w, h, screen, x, y, SRCCOPY | CAPTUREBLT)
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return buf.raw
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)


class Shot:
    """A captured image: top-down BGRA bytes plus its size."""

    def __init__(self, bgra, w, h):
        self.bgra, self.w, self.h = bgra, w, h

    def rgb(self):
        out = bytearray(self.w * self.h * 3)
        out[0::3], out[1::3], out[2::3] = self.bgra[2::4], self.bgra[1::4], self.bgra[0::4]
        return bytes(out)

    def ppm(self, table=None):
        """PPM bytes for tkinter.PhotoImage; `table` maps each channel value."""
        rgb = self.rgb()
        if table:
            rgb = rgb.translate(table)
        return b"P6 %d %d 255\n" % (self.w, self.h) + rgb


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def monitor_at_cursor():
    """(x, y, w, h) of the monitor under the mouse, in physical pixels once
    dpi_aware() has been called."""
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    mon = user32.MonitorFromPoint(pt, 2)
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        r = mi.rcMonitor
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def grab(x, y, w, h):
    data = bytearray(capture(x, y, w, h))
    data[3::4] = b"\xff" * (w * h)  # GDI leaves alpha at 0; make it opaque
    return Shot(bytes(data), w, h)


# Clipboard ------------------------------------------------------------------

def _clipboard(formats):
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise OSError("the clipboard is busy")
    try:
        user32.EmptyClipboard()
        for fmt, data in formats:
            h = kernel32.GlobalAlloc(0x0002, len(data))
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(fmt, h):
                kernel32.GlobalFree(h)
    finally:
        user32.CloseClipboard()


def copy_files(paths):
    """Puts files on the clipboard the way Explorer's Copy does (CF_HDROP),
    so they can be pasted into Explorer, chats and mail."""
    files = "\0".join(os.path.normpath(os.path.abspath(p)) for p in paths) + "\0\0"
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)  # DROPFILES, fWide = 1
    _clipboard([(CF_HDROP, header + files.encode("utf-16-le"))])


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR)]


def trash(paths):
    """Moves files to the Recycle Bin (so a delete can be undone there)."""
    paths = [os.path.normpath(os.path.abspath(p)) for p in paths if os.path.exists(p)]
    if not paths:
        return
    op = SHFILEOPSTRUCTW(None, 3, "\0".join(paths) + "\0", None,  # FO_DELETE
                         0x0040 | 0x0010 | 0x0004 | 0x0400)  # undo, no confirm, silent, no UI
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if res or op.fAnyOperationsAborted:
        raise OSError(f"couldn't move to the Recycle Bin (code {res})")
