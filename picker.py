"""Region picker for Screen Recorder: a live, dimmed overlay over all
monitors (nothing is frozen, so it opens at once, even while Fenn's panel is
still folding away).

    python picker.py [--accent #RRGGBB] [--exclude-pid PID]

- Hover outlines the window under the cursor; a click records it.
- Drag a region: letting go records it.
- F records the whole monitor under the cursor.
- Esc or right-click cancels.

Prints one JSON line: {"x", "y", "w", "h"} in physical screen pixels, or
{"cancel": true}.

Two windows: `dim` is a translucent black sheet that takes the mouse and the
keyboard, with a fully transparent hole where the region is while dragging;
`ink` sits above it, fully opaque but click-through, and draws the outline,
the size label and the hint, so they stay crisp instead of translucent.
"""

import argparse
import ctypes
import json
import os
import sys
from ctypes import wintypes

import screen

KEY = "#010203"  # transparent color in both windows
DIM_ALPHA = 0.42
u32 = screen.user32
u32.WindowFromPoint.restype = wintypes.HWND
u32.WindowFromPoint.argtypes = [wintypes.POINT]
u32.MonitorFromPoint.restype = wintypes.HMONITOR
u32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
u32.GetWindowLongW.restype = ctypes.c_long
u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
u32.GetParent.restype = ctypes.c_void_p
u32.GetParent.argtypes = [ctypes.c_void_p]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def emit(**msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _process_name(pid):
    k = screen.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        ok = k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value).lower() if ok else ""
    finally:
        k.CloseHandle(h)


def visible_windows(exclude_pid=0):
    """Top-level window rectangles, front to back, before the overlay exists.
    Fenn's own windows are left out: its panel folds away as you move."""
    rects = []
    dwm = ctypes.windll.dwmapi
    own = os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
            return True
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
        if cloaked.value:
            return True
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in (own, exclude_pid) or _process_name(pid.value) == "fenn.exe":
            return True
        r = wintypes.RECT()
        if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # frame
            u32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left >= 40 and r.bottom - r.top >= 40 and \
                u32.GetWindowTextLengthW(hwnd) > 0:
            rects.append((r.left, r.top, r.right, r.bottom))
        return True
    u32.EnumWindows(each, 0)
    return rects


def monitor_at(x, y):
    mon = u32.MonitorFromPoint(wintypes.POINT(x, y), 2)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    if mon and u32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        r = mi.rcMonitor
        return r.left, r.top, r.right, r.bottom
    return None


def hwnd_of(win):
    win.update_idletasks()
    return u32.GetParent(win.winfo_id()) or win.winfo_id()


def click_through(win):
    hwnd = hwnd_of(win)
    ex = u32.GetWindowLongW(hwnd, -20)
    # layered, transparent to the mouse, no taskbar button, never activated
    u32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x8000000)


class Picker:
    def __init__(self, accent, exclude_pid):
        import tkinter as tk
        self.tk = tk
        self.accent = accent
        self.vx, self.vy, self.vw, self.vh = screen.virtual_screen()
        self.windows = visible_windows(exclude_pid)
        self.start = None
        self.done = False
        geo = f"{self.vw}x{self.vh}+{self.vx}+{self.vy}"

        root = self.root = tk.Tk()  # the dim sheet: takes mouse and keyboard
        root.withdraw()
        root.overrideredirect(True)
        root.geometry(geo)
        root.configure(bg="black")
        root.attributes("-topmost", True)
        root.attributes("-alpha", DIM_ALPHA)
        root.attributes("-transparentcolor", KEY)
        d = self.dim = tk.Canvas(root, width=self.vw, height=self.vh, bg="black",
                                 highlightthickness=0, bd=0, cursor="crosshair")
        d.pack()
        self.hole = d.create_rectangle(0, 0, 0, 0, fill=KEY, outline="")

        ink = self.ink = tk.Toplevel(root)  # crisp outline, label and hint
        ink.withdraw()
        ink.overrideredirect(True)
        ink.geometry(geo)
        ink.configure(bg=KEY)
        ink.attributes("-topmost", True)
        ink.attributes("-transparentcolor", KEY)
        self.c = tk.Canvas(ink, width=self.vw, height=self.vh, bg=KEY, highlightthickness=0,
                           bd=0)
        self.c.pack()

        root.deiconify()
        ink.deiconify()
        click_through(ink)
        self.hint()
        d.bind("<Motion>", self.on_move)
        d.bind("<ButtonPress-1>", self.on_press)
        d.bind("<B1-Motion>", self.on_drag)
        d.bind("<ButtonRelease-1>", self.on_release)
        d.bind("<ButtonPress-3>", lambda e: self.finish(cancel=True))
        root.bind("<Escape>", lambda e: self.finish(cancel=True))
        root.bind("<KeyPress-f>", lambda e: self.full_monitor())
        root.bind("<KeyPress-F>", lambda e: self.full_monitor())
        root.after(10, self.focus)
        root.after(60, self.on_move)  # outline the window already under the cursor

    def focus(self):
        try:
            u32.SetForegroundWindow(hwnd_of(self.root))
        except Exception:
            pass
        self.root.focus_force()
        self.dim.focus_set()

    def cursor(self):
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        return pt.x - self.vx, pt.y - self.vy

    def hint(self):
        c = self.c
        x, y = self.cursor()
        m = self.monitor_rect(x, y)
        t = c.create_text((m[0] + m[2]) // 2, m[1] + 64, fill="#f2f2f2",
                          font=("Segoe UI", 11), tags="hint",
                          text="Drag a region or click a window to record   ·   "
                               "F  whole screen   ·   Esc  cancel")
        x0, y0, x1, y1 = c.bbox(t)
        c.create_rectangle(x0 - 18, y0 - 9, x1 + 18, y1 + 9, fill="#1c1c1c",
                           outline="#3a3a3a", tags="hint")
        c.tag_raise(t)

    def outline(self, rect, label, hole):
        c = self.c
        c.delete("sel")
        x0, y0, x1, y1 = [int(v) for v in rect]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.vw, x1), min(self.vh, y1)
        self.dim.coords(self.hole, *((x0, y0, x1, y1) if hole else (0, 0, 0, 0)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return
        c.create_rectangle(x0 - 1, y0 - 1, x1 + 1, y1 + 1, outline=self.accent, width=2,
                           tags="sel")
        w, h = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
        ty = y0 - 16 if y0 > 34 else y1 + 16
        t = c.create_text(x0 + 6, ty, text=f"{label}{w} × {h}", anchor="w", fill="white",
                          font=("Segoe UI", 9), tags="sel")
        bx0, by0, bx1, by1 = c.bbox(t)
        c.create_rectangle(bx0 - 7, by0 - 3, bx1 + 7, by1 + 3, fill="#1c1c1c", outline="",
                           tags="sel")
        c.tag_raise(t)

    def window_at(self, x, y):
        ax, ay = x + self.vx, y + self.vy
        for r in self.windows:
            if r[0] <= ax < r[2] and r[1] <= ay < r[3]:
                return (r[0] - self.vx, r[1] - self.vy, r[2] - self.vx, r[3] - self.vy)
        return None

    def on_move(self, e=None):
        if self.start is not None or self.done:
            return
        x, y = (e.x, e.y) if e is not None else self.cursor()
        r = self.window_at(x, y)
        if r:
            self.outline(r, "Window  ·  ", hole=False)
        else:
            self.c.delete("sel")

    def on_press(self, e):
        self.start = (e.x, e.y)

    def on_drag(self, e):
        if self.start is None:
            return
        x0, y0 = self.start
        if abs(e.x - x0) < 6 and abs(e.y - y0) < 6:
            return
        self.c.delete("hint")
        self.outline((min(x0, e.x), min(y0, e.y), max(x0, e.x), max(y0, e.y)), "", hole=True)

    def on_release(self, e):
        if self.start is None:
            return
        x0, y0 = self.start
        self.start = None
        if abs(e.x - x0) < 6 and abs(e.y - y0) < 6:
            r = self.window_at(e.x, e.y) or self.monitor_rect(e.x, e.y)
        else:
            r = (min(x0, e.x), min(y0, e.y), max(x0, e.x), max(y0, e.y))
        self.pick(r)

    def monitor_rect(self, x, y):
        m = monitor_at(x + self.vx, y + self.vy)
        if not m:
            return (0, 0, self.vw, self.vh)
        return (m[0] - self.vx, m[1] - self.vy, m[2] - self.vx, m[3] - self.vy)

    def full_monitor(self):
        self.pick(self.monitor_rect(*self.cursor()))

    def pick(self, r):
        x0, y0, x1, y1 = [int(v) for v in r]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.vw, x1), min(self.vh, y1)
        if x1 - x0 < 16 or y1 - y0 < 16:
            self.outline((0, 0, 0, 0), "", hole=False)  # too small: try again
            return
        self.finish(x=x0 + self.vx, y=y0 + self.vy, w=x1 - x0, h=y1 - y0)

    def finish(self, **msg):
        if self.done:
            return
        self.done = True
        emit(**msg)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accent", default="#7C5CFF")
    ap.add_argument("--exclude-pid", type=int, default=0)
    args = ap.parse_args()
    screen.dpi_aware()
    Picker(args.accent, args.exclude_pid).run()


if __name__ == "__main__":
    main()
