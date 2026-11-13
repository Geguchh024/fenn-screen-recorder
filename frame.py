"""Recording frame for Screen Recorder: a click-through border drawn just
outside the recorded region (so it never ends up in the video), and the
countdown before recording starts, drawn inside it.

    python frame.py --rect X,Y,W,H [--accent #RRGGBB] [--countdown N] [--no-border]

Prints "go" once the countdown is over and the number is off the screen.
Reads "pause", "resume" and "quit" lines on stdin.
"""

import argparse
import ctypes
import queue
import sys
import threading

import screen

B = 3      # border thickness
GAP = 2    # between the region and the border
KEY = "#010203"  # transparent color
u32 = screen.user32
u32.GetWindowLongW.restype = ctypes.c_long
u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
u32.GetParent.restype = ctypes.c_void_p
u32.GetParent.argtypes = [ctypes.c_void_p]
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x80, 0x8000000, 0x8
PAUSED = "#f59f00"


class Frame:
    def __init__(self, rect, accent, countdown, border):
        import tkinter as tk
        self.x, self.y, self.w, self.h = rect
        self.accent, self.border = accent, border
        self.left = countdown
        self.lines = queue.Queue()
        m = B + GAP
        root = self.root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.configure(bg=KEY)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", KEY)
        root.geometry(f"{self.w + 2 * m}x{self.h + 2 * m}+{self.x - m}+{self.y - m}")
        c = self.canvas = tk.Canvas(root, width=self.w + 2 * m, height=self.h + 2 * m, bg=KEY,
                                    highlightthickness=0, bd=0)
        c.pack()
        root.update_idletasks()
        hwnd = u32.GetParent(root.winfo_id()) or root.winfo_id()
        ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT |
                           WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
        root.deiconify()
        self.draw_border(accent if countdown else "#e03131")
        threading.Thread(target=self.read, daemon=True).start()
        root.after(50, self.poll)
        if countdown:
            self.tick()
        else:
            root.after(60, self.go)

    def draw_border(self, color, dash=None):
        c = self.canvas
        c.delete("border")
        if not self.border:
            return
        m = B + GAP
        w, h = self.w + 2 * m, self.h + 2 * m
        c.create_rectangle(B / 2, B / 2, w - B / 2, h - B / 2, outline=color, width=B, dash=dash,
                           tags="border")
        # Corner brackets make the region easy to spot even when the border is thin.
        L = min(22, self.w // 4, self.h // 4)
        for (x0, y0, dx, dy) in ((0, 0, 1, 1), (w, 0, -1, 1), (0, h, 1, -1), (w, h, -1, -1)):
            c.create_line(x0, y0 + dy * L, x0, y0, x0 + dx * L, y0, fill=color, width=B + 2,
                          capstyle="projecting", tags="border")

    def tick(self):
        c = self.canvas
        c.delete("count")
        if self.left <= 0:
            self.root.update_idletasks()
            self.root.after(90, self.go)  # let the screen repaint without the number
            return
        m = B + GAP
        cx, cy = m + self.w / 2, m + self.h / 2
        r = max(28, min(64, self.w // 6, self.h // 4))
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#141414", outline=self.accent,
                      width=3, tags="count")
        c.create_text(cx, cy - 2, text=str(self.left), fill="white",
                      font=("Segoe UI Semibold", int(r * 0.9)), tags="count")
        if self.h > 2 * r + 60:
            t = c.create_text(cx, cy + r + 22, text="Recording starts  ·  Ctrl+Alt+M cancels",
                              fill="white", font=("Segoe UI", 10), tags="count")
            x0, y0, x1, y1 = c.bbox(t)
            c.create_rectangle(x0 - 10, y0 - 4, x1 + 10, y1 + 4, fill="#141414", outline="",
                               tags="count")
            c.tag_raise(t)
        self.left -= 1
        self.root.after(1000, self.tick)

    def go(self):
        self.draw_border("#e03131")
        print("go", flush=True)

    def read(self):
        for line in sys.stdin:
            self.lines.put(line.strip())
        self.lines.put("quit")

    def poll(self):
        while True:
            try:
                cmd = self.lines.get_nowait()
            except queue.Empty:
                break
            if cmd == "quit":
                self.root.destroy()
                return
            if cmd == "pause":
                self.draw_border(PAUSED, dash=(6, 4))
            elif cmd == "resume":
                self.draw_border("#e03131")
        self.root.after(50, self.poll)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rect", required=True)
    ap.add_argument("--accent", default="#7C5CFF")
    ap.add_argument("--countdown", type=int, default=0)
    ap.add_argument("--no-border", action="store_true")
    a = ap.parse_args()
    screen.dpi_aware()
    rect = [int(v) for v in a.rect.split(",")]
    Frame(rect, a.accent, a.countdown, not a.no_border).root.mainloop()


if __name__ == "__main__":
    main()
