"""Screen Recorder: Ctrl+Alt+M or the tile's button opens a region picker;
the page can also record the full screen or the last region straight away.
After an optional countdown it records to MP4 with ffmpeg (gdigrab), with the
system audio and the microphone if chosen, and a border around the region
that stays out of the video. Pause and resume are joined into one file.
Afterwards the page trims on a filmstrip and exports MP4 or GIF; recent
recordings are listed on the page."""

import json
import math
import os
import subprocess
import sys
import threading
import time

import fenn
import ff
import screen

try:
    import audio
except (OSError, ImportError):  # no Windows audio stack: record without sound
    audio = None

app = fenn.Plugin()
screen.dpi_aware()  # monitor sizes in physical pixels, like the picker and gdigrab
HERE = os.path.dirname(os.path.abspath(__file__))
CREATE_NO_WINDOW = 0x08000000
OUT_DIR = os.path.join(os.path.expanduser("~"), "Videos", "Fenn Recordings")
PREVIEW = os.path.join(app.data_dir, "preview.png")
STRIP = os.path.join(app.data_dir, "strip.png")
LOG = os.path.join(app.data_dir, "ffmpeg-record.log")
STRIP_N, STRIP_W, STRIP_H = 10, 112, 64
FPS = [15, 30, 60]
QUALITIES = list(ff.QUALITY)
COUNTDOWNS = [0, 3, 5]
GIF_WIDTHS = [480, 640, 800]
GIF_FPS = [10, 15, 24]
FORMATS = ["mp4", "gif"]
MIN_LEN = 0.2

DEFAULTS = {"fps": 30, "quality": "Balanced", "countdown": 3, "cursor": True, "border": True,
            "system_audio": True, "mic": False, "mic_id": None,
            "format": "mp4", "gif_w": 640, "gif_fps": 15, "mp4_audio": True}
settings = {**DEFAULTS, **(app.load("settings", {}) or {})}

state = {
    "ffmpeg": ff.find(app.data_dir),
    "dl": None, "dl_frac": 0.0, "dl_error": "",
    # idle | picking | countdown | recording | paused | stopping | joining
    "mode": "idle",
    "view": "home",              # home | editor | settings
    "take": 0,                   # bumps per recording, so stale messages are ignored
    "rect": None, "last_rect": app.load("last_rect", None),
    "out": None, "segments": [], "acc": 0.0, "seg_started": 0.0, "capturing": False,
    "count_end": 0.0, "resume_pending": False, "discard": False,
    "clip": None, "loading": None,
    "in": 0.0, "out_t": 0.0, "playhead": 0.0,
    "exporting": None, "export_frac": 0.0, "export_started": 0.0, "export_cancel": False,
    "exports": [],
    "library": [],
    "mics": None, "output": None,
    "confirm_delete": False, "name_n": 0,
    "error": "", "warn": "", "note": "",
    "page_w": 576,
}
procs = {"recorder": None, "picker": None, "frame": None, "export": None}
sources = []
exports_db = app.load("exports", {}) or {}
dl_cancel = threading.Event()
preview_lock = threading.Lock()
preview_job = {"busy": False, "next": None}
last_tick = [None]


def post(message):
    app._inbox.put(message)


def save_settings():
    app.save("settings", settings)


def clock(seconds, tenths=False):
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{int(h)}:{int(m):02d}:{int(s):02d}"
    return f"{int(m)}:{s:04.1f}" if tenths else f"{int(m)}:{int(s):02d}"


def human_size(path_or_n):
    try:
        n = path_or_n if isinstance(path_or_n, int) else os.path.getsize(path_or_n)
    except OSError:
        return ""
    if n >= 1e9:
        return f"{n / 1e9:.2f} GB"
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{max(1, n / 1e3):.0f} KB"


def when(ts):
    t, now = time.localtime(ts), time.localtime()
    day = time.strftime("%H:%M", t)
    if t.tm_yday == now.tm_yday and t.tm_year == now.tm_year:
        return f"Today {day}"
    if time.localtime(time.time() - 86400).tm_yday == t.tm_yday:
        return f"Yesterday {day}"
    return time.strftime("%b %d ", t).replace(" 0", " ") + day


def unique(path):
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path, n = f"{stem} ({n}){ext}", n + 1
    return path


def elapsed():
    e = state["acc"]
    if state["mode"] == "recording" and state["capturing"]:
        e += time.monotonic() - state["seg_started"]
    return e


def countdown_left():
    return max(0, math.ceil(state["count_end"] - time.monotonic()))


def scan_library():
    try:
        names = os.listdir(OUT_DIR)
    except OSError:
        names = []
    files = []
    for n in names:
        if n.startswith(".") or not n.lower().endswith((".mp4", ".gif")):
            continue
        p = os.path.join(OUT_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        files.append((st.st_mtime, p, st.st_size))
    files.sort(reverse=True)
    state["library"] = files[:40]


def audio_summary():
    parts = []
    if audio and settings["system_audio"]:
        parts.append("system audio")
    if audio and settings["mic"]:
        parts.append("mic")
    return " + ".join(parts) if parts else "no audio"


# Views ----------------------------------------------------------------------------

def hrow(children, gap=6):
    """A vertically centered row. Fenn sizes a centered row without a spacer
    to the whole remaining height, so end it with one: then it keeps its own
    height."""
    return fenn.row(list(children) + [fenn.spacer()], gap=gap, align="center")


def tile_view():
    mode = state["mode"]
    w = app.tile_width or 44
    wide = app.tile_size in ("wide", "large") and w >= 90
    if mode == "countdown":
        n = countdown_left()
        num = fenn.text(str(max(1, n)), size="big", color="accent")
        if wide:
            return fenn.row([fenn.button("discard", icon="x", tooltip="Cancel"), fenn.column([
                fenn.text("Starting", size="small", color="dim", bold=True),
                fenn.text(f"in {max(1, n)} s", size="title")], gap=0)], gap=6, align="center")
        return fenn.column([num], align="center", justify="center",
                           tooltip="Recording starts soon (Ctrl+Alt+M cancels)")
    if mode in ("recording", "paused"):
        rec = mode == "recording"
        t = clock(elapsed())
        stop = fenn.button("stop", icon="stop", primary=True, tooltip="Stop and edit")
        if wide:
            pause = fenn.button("pause" if rec else "resume", icon="pause" if rec else "play",
                                tooltip="Pause" if rec else "Resume")
            kids = [stop]
            if w >= 120 or app.tile_size == "large":
                kids.append(pause)
            kids.append(fenn.column([
                fenn.text("● REC" if rec else "Paused", size="small",
                          color="red" if rec else "yellow", bold=True),
                fenn.text(t, size="title")], gap=0))
            return fenn.row(kids, gap=6, align="center")
        return fenn.column([stop, fenn.text(t, size="small", color="red" if rec else "yellow")],
                           gap=0, align="center", justify="center")
    busy = {"picking": "Picking a region…", "stopping": "Saving the recording…",
            "joining": "Joining the parts…"}.get(mode) or state["exporting"] or \
        (state["dl"] and "Downloading ffmpeg…") or (state["loading"] and "Loading…")
    if busy:
        return fenn.column([fenn.icon("spinner", size=22, color="accent")], align="center",
                           justify="center", tooltip=busy)
    if not wide:
        # Small: no button, so a click anywhere opens the page (the record
        # button used to fill the tile and catch those clicks).
        return fenn.column([fenn.icon("record", size=24, color="red")], align="center",
                           justify="center", tooltip="Screen Recorder · Ctrl+Alt+M records")
    rec = fenn.button("record", icon="record", tooltip="Record a region (Ctrl+Alt+M)")
    sub = when(state["library"][0][0]) if state["library"] else "Ctrl+Alt+M"
    return fenn.row([rec, fenn.column([
        fenn.text("Recorder", size="small", color="dim", bold=True),
        fenn.text(sub, size="small")], gap=0)], gap=6, align="center")


def messages():
    out = []
    for key, icon, color in (("error", "warning", "red"), ("warn", "warning", "yellow"),
                             ("note", "info", "dim")):
        if state[key]:
            out.append(fenn.row([
                fenn.icon(icon, size=16, color=color),
                fenn.text(state[key], size="small", color="red" if key == "error" else "dim",
                          wrap=True),
                fenn.spacer(),
                fenn.button(f"dismiss:{key}", icon="x", tooltip="Dismiss"),
            ], gap=6, align="center"))
    return out


def setup_card():
    downloading = bool(state["dl"])
    if downloading:
        title, text, color = "Downloading ffmpeg", state["dl"], "dim"
    elif state["dl_error"]:
        title, text, color = "The download didn't finish", state["dl_error"], "red"
    else:
        title = "One-time setup: ffmpeg"
        text = (f"Recording uses ffmpeg. Download the static build by gyan.dev (about "
                f"{ff.BUILD_MB} MB, from its GitHub mirror, checked against the SHA-256 "
                "gyan.dev publishes) into this plugin's data folder.")
        color = "dim"
    if downloading:
        btn = fenn.button("dl_cancel", label="Cancel", icon="x")
    else:
        btn = fenn.button("download", label="Retry" if state["dl_error"] else
                          f"Download {ff.BUILD_MB} MB",
                          icon="arrow-clockwise" if state["dl_error"] else "download-simple",
                          primary=True)
    rows = [fenn.row([
        fenn.icon("spinner" if downloading else "download-simple", size=22, color="accent"),
        fenn.text(title, bold=True),
        fenn.spacer(),
        btn,
    ], gap=10, align="center"),
        fenn.text(text, size="small", wrap=True, color=color)]
    if downloading:
        rows.append(fenn.progress(state["dl_frac"]))
    return fenn.card(rows, gap=6)


def home_view():
    body = []
    if not state["ffmpeg"]:
        body.append(setup_card())
    can = bool(state["ffmpeg"])
    buttons = [fenn.button("record_page", label="Record a region", icon="selection-plus",
                           primary=can),
               fenn.button("record_full", label="Full screen", icon="monitor",
                           tooltip="The monitor Fenn is on")]
    lr = state["last_rect"]
    if lr:
        buttons.append(fenn.button("record_last", label="Last region",
                                   icon="arrow-counter-clockwise",
                                   tooltip=f"{lr[2]} × {lr[3]} at {lr[0]}, {lr[1]}"))
    body.append(fenn.row(buttons + [fenn.spacer(), fenn.text("Ctrl+Alt+M", size="small",
                                                              color="faint")],
                         gap=6, align="center"))
    cd = settings["countdown"]
    summary = hrow([
        fenn.icon("video-camera", size=14, color="faint"),
        fenn.text(f"{settings['fps']} fps · {settings['quality']} · {audio_summary()}"
                  f" · {f'{cd} s countdown' if cd else 'no countdown'}",
                  size="small", color="dim"),
    ])
    summary["tooltip"] = "Change these with the gear above"
    body.append(summary)
    body += messages()
    body.append(fenn.divider())
    lib = state["library"]
    body.append(fenn.row([fenn.text("Recent", bold=True), fenn.spacer(),
                          fenn.text(f"{len(lib)} in Videos\\Fenn Recordings" if lib else "",
                                    size="small", color="faint")], align="center"))
    if lib:
        items = []
        for i, (mtime, path, size) in enumerate(lib[:12]):
            gif = path.lower().endswith(".gif")
            items.append(fenn.item(f"lib:{i}", os.path.basename(path),
                                   subtitle=f"{when(mtime)} · {human_size(size)}"
                                   f"{' · opens in your viewer' if gif else ''}",
                                   icon="gif" if gif else "film-strip",
                                   trailing="GIF" if gif else "Edit"))
        body.append(fenn.list_view(items))
    else:
        body.append(fenn.card([hrow([
            fenn.icon("film-strip", size=22, color="faint"),
            fenn.column([fenn.text("No recordings yet", color="dim"),
                         fenn.text("Pick a region, a window or the whole screen. When you "
                                   "stop, the recording opens here to trim and export.",
                                   size="small", color="faint", wrap=True)], gap=2)],
            gap=10)]))
    return body


def meter(kind, level):
    v = min(1.0, max(0.0, level) ** 0.5)
    return hrow([
        fenn.icon("speaker-high" if kind == "system" else "microphone", size=16, color="dim",
                  tooltip="System audio" if kind == "system" else "Microphone"),
        fenn.progress(v, color="green" if v < 0.85 else "orange"),
    ], gap=8)


def recording_view():
    mode = state["mode"]
    body = []
    if mode == "countdown":
        n = max(1, countdown_left())
        body.append(fenn.card([fenn.row([
            fenn.ring(n / max(1, settings["countdown"]), label=str(n), size=48),
            fenn.column([fenn.text("Get ready", size="small", color="dim", bold=True),
                         fenn.text(f"Recording starts in {n} s", size="title")], gap=2),
            fenn.spacer(),
            fenn.button("discard", label="Cancel", icon="x"),
        ], gap=12, align="center")]))
        body += messages()
        return body
    rec = mode == "recording"
    status = {"recording": "Recording", "paused": "Paused", "stopping": "Saving…",
              "joining": "Joining the parts…"}[mode]
    color = "red" if rec else ("yellow" if mode == "paused" else "dim")
    x, y, w, h = state["rect"] or (0, 0, 0, 0)
    head = fenn.row([
        fenn.icon("record" if rec else ("pause" if mode == "paused" else "spinner"), size=26,
                  color=color, fill=rec),
        fenn.column([fenn.text(status, size="small", color=color, bold=True),
                     fenn.text(clock(elapsed()), size="huge", mono=True)], gap=0),
        fenn.spacer(),
        fenn.column([fenn.text(f"{w} × {h}", size="small"),
                     fenn.text(f"{settings['fps']} fps · {settings['quality']}", size="small",
                               color="dim"),
                     fenn.text(f"{len(state['segments'])} parts" if len(state["segments"]) > 1
                               else os.path.basename(state["out"] or ""), size="small",
                               color="faint")], gap=1, align="end"),
    ], gap=10, align="center")
    card = [head]
    if rec and sources:
        for s in sources:
            card.append(meter(s.kind, s.level))
            s.level *= 0.35  # the source holds its peak until the next redraw
    elif rec:
        card.append(fenn.text("No audio. Turn on system audio or the mic in settings.",
                              size="small", color="faint"))
    elif mode == "paused":
        card.append(fenn.text("Nothing is recorded while paused. Resume adds to the same file.",
                              size="small", color="faint", wrap=True))
    body.append(fenn.card(card, gap=8))
    if mode in ("recording", "paused"):
        body.append(fenn.row([
            fenn.button("pause" if rec else "resume", label="Pause" if rec else "Resume",
                        icon="pause" if rec else "play"),
            fenn.button("stop_page", label="Stop and edit", icon="stop", primary=True),
            fenn.spacer(),
            fenn.button("discard", label="Discard", icon="trash",
                        tooltip="Stop and move the recording to the Recycle Bin"),
        ], gap=6, align="center"))
        body.append(fenn.text("Ctrl+Alt+M stops. The border around the region isn't recorded.",
                              size="small", color="faint"))
    body += messages()
    return body


def fit(w, h, box_w, box_h):
    if not w or not h:
        return box_w, box_h
    s = min(box_w / w, box_h / h)
    return max(1, int(w * s)), max(1, int(h * s))


def timeline(width):
    c = state["clip"]
    dur = max(0.01, c["duration"])
    pad = 7

    def X(t):
        return pad + (width - 2 * pad) * min(max(t, 0), dur) / dur

    xi, xo, xp = X(state["in"]), X(state["out_t"]), X(state["playhead"])
    shapes = [
        fenn.rect(pad, 13, width - 2 * pad, 6, fill="faint", radius=3),
        fenn.rect(xi, 13, max(2, xo - xi), 6, fill="accent", radius=3),
        fenn.line([[xp, 4], [xp, 28]], color="text", width=1.5),
        fenn.rect(xi - 4, 6, 8, 20, fill="accent", radius=4),
        fenn.rect(xo - 4, 6, 8, 20, fill="accent", radius=4),
        fenn.line([[xi, 11], [xi, 21]], color="on_accent", width=1.5),
        fenn.line([[xo, 11], [xo, 21]], color="on_accent", width=1.5),
    ]
    return fenn.canvas(32, shapes, width=width, id="timeline",
                       tooltip="Click to move the nearest handle there")


def export_card(busy):
    c = state["clip"]
    fmt = settings["format"]
    rows = [fenn.row([
        fenn.text("Export", bold=True),
        fenn.chips("format", ["MP4", "GIF"], FORMATS.index(fmt)),
        fenn.spacer(),
        fenn.button("export", label=f"Export {fmt.upper()}", icon="export", primary=not busy),
    ], gap=8, align="center")]
    if fmt == "gif":
        rows.append(hrow([
            fenn.text("Width", size="small", color="dim"),
            fenn.chips("gif_w", [str(v) for v in GIF_WIDTHS],
                       GIF_WIDTHS.index(settings["gif_w"]) if settings["gif_w"] in GIF_WIDTHS
                       else 1),
            fenn.spacer(size=6),
            fenn.text("FPS", size="small", color="dim"),
            fenn.chips("gif_fps", [str(v) for v in GIF_FPS],
                       GIF_FPS.index(settings["gif_fps"]) if settings["gif_fps"] in GIF_FPS
                       else 1),
        ]))
        length = state["out_t"] - state["in"]
        if length > 20:
            rows.append(fenn.text(f"{clock(length)} is long for a GIF: expect a big file. "
                                  "Trim it or pick a smaller width.", size="small",
                                  color="yellow", wrap=True))
    elif c["audio"]:
        rows.append(fenn.toggle("mp4_audio", settings["mp4_audio"], label="Keep the audio"))
    else:
        rows.append(fenn.text("H.264, ready to share. This recording has no audio.",
                              size="small", color="faint"))
    if busy:
        frac = state["export_frac"]
        spent = time.monotonic() - state["export_started"]
        eta = f" · {ff.fmt_eta(spent / frac * (1 - frac))} left" if frac > 0.05 else ""
        rows.append(fenn.row([fenn.text(f"{state['exporting']} {int(frac * 100)}%{eta}",
                                        size="small", color="dim"),
                              fenn.spacer(),
                              fenn.button("export_cancel", label="Cancel", icon="x")],
                             align="center"))
        rows.append(fenn.progress(frac))
    return fenn.card(rows, gap=6)


def editor_view():
    c = state["clip"]
    if not c:
        return [fenn.card([hrow([fenn.icon("spinner", size=20, color="accent"),
                                 fenn.text("Loading the recording…", color="dim")], gap=8)])]
    width = max(300, state["page_w"])
    dur = c["duration"]
    busy = bool(state["exporting"])
    name = os.path.splitext(os.path.basename(c["path"]))[0]
    meta = " · ".join(v for v in (clock(dur, tenths=dur < 60),
                                  f"{c['width']} × {c['height']}" if c["width"] else "",
                                  f"{c['fps']} fps" if c["fps"] else "",
                                  human_size(c["path"]),
                                  "audio" if c["audio"] else "no audio") if v)
    head = fenn.row([
        fenn.text_input(f"name:{state['name_n']}", name, hint="Name",
                        width=max(160, min(320, width - 200)),
                        tooltip="Type a new name and press Enter to rename"),
        fenn.spacer(),
        fenn.button("play_clip", icon="play", tooltip="Play in your video player"),
        fenn.button("copy_clip", icon="copy", tooltip="Copy the file (paste in Explorer, chats, "
                                                      "mail)"),
        fenn.button("folder_clip", icon="folder-open", tooltip="Show in Explorer"),
        fenn.button("delete_clip", label="Delete?" if state["confirm_delete"] else None,
                    icon="trash", primary=state["confirm_delete"],
                    tooltip="Click again to move it to the Recycle Bin"
                    if state["confirm_delete"] else "Move to the Recycle Bin"),
    ], gap=6, align="center")
    box_w = min(280, width // 2)
    pw, ph = fit(c["width"], c["height"], box_w, int(box_w * 9 / 16))
    preview = fenn.image(path=PREVIEW, width=pw, height=ph, rounding=6,
                         tooltip=f"Frame at {clock(state['playhead'], True)}") \
        if os.path.isfile(PREVIEW) else fenn.card([fenn.text("No preview", color="faint")])
    length = state["out_t"] - state["in"]
    trims = fenn.column([
        fenn.row([fenn.icon("scissors", size=14, color="dim"),
                  fenn.text("Trim", bold=True), fenn.spacer(),
                  fenn.button("reset_trim", icon="arrow-counter-clockwise",
                              tooltip="Use the whole recording")], gap=6, align="center"),
        fenn.row([fenn.text("Start", size="small", color="dim"), fenn.spacer(),
                  fenn.text(clock(state["in"], True), size="small", mono=True)]),
        fenn.slider("in", round(state["in"], 1), min=0, max=round(dur, 1), step=0.1),
        fenn.row([fenn.text("End", size="small", color="dim"), fenn.spacer(),
                  fenn.text(clock(state["out_t"], True), size="small", mono=True)]),
        fenn.slider("out", round(state["out_t"], 1), min=0, max=round(dur, 1), step=0.1),
        fenn.row([fenn.text("Length", size="small", color="dim"), fenn.spacer(),
                  fenn.text(clock(length, True), size="small", mono=True, bold=True,
                            color="accent" if length < dur - 0.05 else "text")]),
    ], gap=3)
    body = [fenn.column([head, fenn.text(meta, size="small", color="dim")], gap=3),
            fenn.row([preview, trims], gap=12)]
    inner = width - 14
    if os.path.isfile(STRIP):
        body.append(fenn.row([fenn.spacer(size=7), fenn.image(
            path=STRIP, width=inner, height=max(16, int(inner / STRIP_N * STRIP_H / STRIP_W)),
            rounding=4)], gap=0))
    body.append(timeline(width))
    body.append(export_card(busy))
    body += messages()
    for i, path in reversed(list(enumerate(state["exports"]))):
        gif = path.lower().endswith(".gif")
        body.append(fenn.row([
            fenn.icon("gif" if gif else "file-video", size=16, color="green"),
            fenn.text(os.path.basename(path)),
            fenn.text(human_size(path), size="small", color="dim"),
            fenn.spacer(),
            fenn.button(f"open_x:{i}", icon="play", tooltip="Open"),
            fenn.button(f"copy_x:{i}", icon="copy", tooltip="Copy the file"),
            fenn.button(f"folder_x:{i}", icon="folder-open", tooltip="Show in Explorer"),
        ], gap=6, align="center"))
    return body


def settings_view():
    def line(label, control, hint=None):
        # A grid, not [label, spacer, chips]: after a spacer Fenn lays out
        # right to left, which would show the chips in reverse order.
        return fenn.grid(2, [fenn.column([fenn.text(label)] + (
            [fenn.text(hint, size="small", color="faint", wrap=True)] if hint else []), gap=0),
            control], gap=8)

    body = [
        fenn.text("Video", size="small", color="dim", bold=True),
        line("Frame rate", fenn.chips("set_fps", [f"{v}" for v in FPS],
                                      FPS.index(settings["fps"]) if settings["fps"] in FPS else 1),
             "60 for games and fast motion"),
        line("Quality", fenn.chips("set_quality", QUALITIES,
                                   QUALITIES.index(settings["quality"])
                                   if settings["quality"] in QUALITIES else 1),
             "Crisp keeps text sharp, Compact keeps files small"),
        line("Countdown", fenn.chips("set_countdown", ["Off", "3 s", "5 s"],
                                     COUNTDOWNS.index(settings["countdown"])
                                     if settings["countdown"] in COUNTDOWNS else 1)),
        fenn.toggle("set_cursor", settings["cursor"], label="Show the mouse cursor"),
        fenn.toggle("set_border", settings["border"],
                    label="Show a border around the region while recording (not recorded)"),
        fenn.divider(),
        fenn.text("Audio", size="small", color="dim", bold=True),
    ]
    if not audio:
        body.append(fenn.text("Windows audio isn't available, so recordings have no sound.",
                              size="small", color="yellow"))
    else:
        out = state["output"] or "the default output"
        body.append(fenn.toggle("set_system_audio", settings["system_audio"],
                                label=f"Record system audio ({out})"))
        body.append(fenn.toggle("set_mic", settings["mic"], label="Record the microphone"))
        if settings["mic"]:
            mics = state["mics"] or []
            if mics:
                chosen = settings["mic_id"] if any(m["id"] == settings["mic_id"] for m in mics) \
                    else None
                body.append(fenn.list_view([fenn.item(
                    f"mic:{i}", m["name"], subtitle="Default microphone" if m["default"] else None,
                    icon="microphone", selected=(m["id"] == chosen) or (chosen is None and
                                                                        m["default"]))
                    for i, m in enumerate(mics)]))
            else:
                body.append(fenn.text("No microphone found.", size="small", color="yellow"))
    body += [
        fenn.divider(),
        fenn.text("Files", size="small", color="dim", bold=True),
        line("Saved to", fenn.button("open_out", label="Videos\\Fenn Recordings",
                                     icon="folder-open")),
    ]
    if state["ffmpeg"]:
        v = ff.version(state["ffmpeg"]) if "ff_version" not in state else state["ff_version"]
        state["ff_version"] = v
        body.append(fenn.text(f"ffmpeg {v.split('-')[0] or ''} · {state['ffmpeg']}",
                              size="small", color="faint", wrap=True))
    body += messages()
    return body


def page_actions():
    if state["mode"] in ("countdown", "recording", "paused", "stopping", "joining"):
        return None
    if state["view"] == "editor":
        return [{"id": "back", "icon": "list-bullets", "tooltip": "All recordings"}]
    if state["view"] == "settings":
        return [{"id": "back", "icon": "x", "tooltip": "Close settings"}]
    return [{"id": "settings", "icon": "gear", "tooltip": "Recording settings"},
            {"id": "open_out", "icon": "folder-open", "tooltip": "Open Videos\\Fenn Recordings"}]


def page_view():
    mode = state["mode"]
    if mode in ("countdown", "recording", "paused", "stopping", "joining"):
        body, key = recording_view(), "rec"
    elif state["view"] == "settings":
        body, key = settings_view(), "settings"
    elif state["view"] == "editor":
        body, key = editor_view(), "editor"
    else:
        body, key = home_view(), "home"
    return fenn.column(body, gap=8, scroll=True, id=f"v-{key}")


def draw_page():
    app.page(page_view(), actions=page_actions())


def draw():
    app.tile(tile_view())
    draw_page()
    badge()


def badge():
    mode = state["mode"]
    if mode == "countdown":
        n = countdown_left()
        app.badge_ring(n / max(1, settings["countdown"]), str(max(1, n)))
    elif mode == "recording":
        e = elapsed()
        app.badge_ring((e % 60) / 60, clock(e))
    elif mode == "paused":
        app.badge_glyph("pause")
    elif mode in ("stopping", "joining"):
        app.badge_pulse()
    else:
        app.clear_badge()


@app.every(0.25)
def tick():
    mode = state["mode"]
    if mode not in ("countdown", "recording", "paused"):
        return
    key = (mode, countdown_left() if mode == "countdown" else int(elapsed()))
    if key != last_tick[0]:
        last_tick[0] = key
        badge()
        app.tile(tile_view())
        if app.page_open:
            draw_page()
    elif app.page_open and mode == "recording" and sources:
        draw_page()  # the meters


# Recording ----------------------------------------------------------------------

def need_ffmpeg():
    if state["ffmpeg"]:
        return True
    state["error"] = "Download ffmpeg first: it does the recording."
    state["view"] = "home"
    app.show()
    draw()
    return False


def pick_region():
    if state["mode"] != "idle" or not need_ffmpeg():
        return
    # The overlay is live, so it can open at once: Fenn's panel folds away
    # underneath it as soon as the mouse moves. Fenn (our parent) is left out
    # of the windows a click can pick.
    args = [sys.executable, os.path.join(HERE, "picker.py"), "--accent", app.accent,
            "--exclude-pid", str(os.getppid())]
    state["mode"] = "picking"
    state["error"] = state["warn"] = state["note"] = ""
    try:
        procs["picker"] = proc = subprocess.Popen(args, cwd=HERE, stdin=subprocess.DEVNULL,
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                  creationflags=CREATE_NO_WINDOW)
    except OSError as e:
        state["mode"] = "idle"
        state["error"] = f"Couldn't open the region picker: {e}"
        draw()
        return

    def read():
        out, err = proc.communicate()
        try:
            msg = json.loads(out.decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError):
            msg = {"cancel": True,
                   "error": (err.decode("utf-8", "replace").strip().splitlines() or [""])[-1]}
        post({"type": "x-picked", **msg})
    threading.Thread(target=read, daemon=True).start()
    draw()


def quick_start(rect):
    """Full screen or the last region, from the page: wait for the panel to fold."""
    if state["mode"] != "idle" or not need_ffmpeg():
        return
    app.close_page()
    state["mode"] = "picking"
    take = state["take"]
    threading.Timer(0.45, lambda: post({"type": "x-begin", "rect": list(rect),
                                        "take": take})).start()
    draw()


def begin(rect):
    x, y, w, h = [int(v) for v in rect]
    w, h = w - w % 2, h - h % 2  # x264 with yuv420p needs even sizes
    if w < 16 or h < 16:
        state["mode"] = "idle"
        state["error"] = "That region is too small to record."
        draw()
        return
    state["take"] += 1
    state.update(rect=(x, y, w, h), last_rect=[x, y, w, h], segments=[], acc=0.0,
                 capturing=False, resume_pending=False, discard=False,
                 error="", warn="", note="")
    app.save("last_rect", [x, y, w, h])
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except OSError as e:
        state["mode"] = "idle"
        state["error"] = f"Couldn't create {OUT_DIR}: {e}"
        draw()
        return
    state["out"] = unique(os.path.join(OUT_DIR, time.strftime("rec-%Y%m%d-%H%M%S.mp4")))
    n = settings["countdown"]
    if settings["border"] or n:
        open_frame((x, y, w, h), n)
    if procs["frame"] is not None:
        state["mode"] = "countdown"
        state["count_end"] = time.monotonic() + n
        app.active(True)
        draw()
    else:
        start_segment()


def open_frame(rect, countdown):
    args = [sys.executable, os.path.join(HERE, "frame.py"), "--rect", ",".join(map(str, rect)),
            "--accent", app.accent, "--countdown", str(countdown)]
    if not settings["border"]:
        args.append("--no-border")
    try:
        p = subprocess.Popen(args, cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    except OSError:
        procs["frame"] = None
        return
    procs["frame"] = p
    take = state["take"]

    def read():
        went = False
        for line in p.stdout:
            if line.strip() == b"go" and not went:
                went = True
                post({"type": "x-go", "take": take})
        if not went:  # no tkinter, or closed early: record anyway
            post({"type": "x-go", "take": take})
    threading.Thread(target=read, daemon=True).start()


def frame_cmd(cmd):
    p = procs["frame"]
    if p is None or p.poll() is not None:
        return
    try:
        p.stdin.write(cmd.encode() + b"\n")
        p.stdin.flush()
    except (OSError, ValueError):
        pass


def close_frame():
    p = procs["frame"]
    procs["frame"] = None
    if p is None:
        return
    frame_cmd_to(p, "quit")

    def reap():
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
    threading.Thread(target=reap, daemon=True).start()


def frame_cmd_to(p, cmd):
    try:
        p.stdin.write(cmd.encode() + b"\n")
        p.stdin.flush()
        p.stdin.close()
    except (OSError, ValueError):
        pass


def start_segment():
    idx = len(state["segments"])
    stem = os.path.splitext(os.path.basename(state["out"]))[0]
    path = state["out"] if idx == 0 else os.path.join(OUT_DIR, f".{stem}-part{idx + 1}.mp4")
    opened = []
    if audio:
        wanted = []
        if settings["system_audio"]:
            wanted.append(audio.Source("system", video_start=lambda: ff.video_start(LOG)))
        if settings["mic"]:
            wanted.append(audio.Source("mic", settings["mic_id"],
                                       video_start=lambda: ff.video_start(LOG)))
        for s in wanted:
            try:
                s.open()
                opened.append(s)
            except OSError as e:
                state["warn"] = f"{e}. Recording without it."
    try:
        proc = ff.record(state["ffmpeg"], state["rect"], path, LOG, fps=settings["fps"],
                         cursor=settings["cursor"], quality=settings["quality"],
                         audio=[(s.pipe, s.fmt, s.rate, s.channels) for s in opened])
    except OSError as e:
        for s in opened:
            s.stop()
        state["error"] = f"Couldn't start ffmpeg: {e}"
        finish()
        return
    procs["recorder"] = proc
    sources[:] = opened
    state["segments"].append(path)
    state.update(mode="recording", capturing=False, seg_started=time.monotonic())
    take, srcs = state["take"], list(opened)

    def watch():
        # ffmpeg takes a moment to start; count from when it really captures.
        deadline = time.monotonic() + 15
        while proc.poll() is None and time.monotonic() < deadline:
            try:
                with open(LOG, "rb") as f:
                    if b"Press [q]" in f.read():
                        break
            except OSError:
                pass
            time.sleep(0.05)
        post({"type": "x-capturing", "take": take, "idx": idx})
        proc.wait()
        for s in srcs:
            s.stop()
        post({"type": "x-seg-ended", "take": take, "idx": idx, "path": path,
              "code": proc.returncode})
    threading.Thread(target=watch, daemon=True).start()
    app.active(True)
    draw()


def quit_recorder():
    proc = procs["recorder"]
    if proc is None:
        return

    def stop():
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
    threading.Thread(target=stop, daemon=True).start()


def pause():
    if state["mode"] != "recording":
        return
    state["acc"] = elapsed()
    state.update(mode="paused", capturing=False)
    quit_recorder()
    frame_cmd("pause")
    draw()


def resume():
    if state["mode"] != "paused":
        return
    frame_cmd("resume")
    if procs["recorder"] is not None:  # the last part is still being saved
        state["resume_pending"] = True
        return
    start_segment()


def stop_recording():
    mode = state["mode"]
    if mode == "countdown":
        discard()
    elif mode == "recording":
        state["acc"] = elapsed()
        state.update(mode="stopping", capturing=False)
        quit_recorder()
        close_frame()
        draw()
    elif mode == "paused":
        state.update(mode="stopping", resume_pending=False)
        close_frame()
        if procs["recorder"] is None:
            finish()
        else:
            draw()


def discard():
    mode = state["mode"]
    if mode not in ("countdown", "recording", "paused"):
        return
    state["discard"] = True
    close_frame()
    if procs["recorder"] is not None:
        state["mode"] = "stopping"
        quit_recorder()
        draw()
    else:
        finish()


def log_tail():
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith(("frame=", "["))]
        return lines[-1] if lines else ""
    except OSError:
        return ""


def finish():
    """The recorder has stopped for good: join the parts and open the editor."""
    close_frame()
    sources.clear()
    state["capturing"] = False
    segs = [p for p in state["segments"] if os.path.isfile(p) and os.path.getsize(p) > 0]
    if state["discard"]:
        try:
            screen.trash(segs)
        except OSError:
            pass
        state.update(mode="idle", discard=False, note="Recording discarded (it's in the Recycle "
                                                      "Bin)." if segs else "")
        app.active(False)
        scan_library()
        draw()
        return
    if not segs:
        state["mode"] = "idle"
        app.active(False)
        state["error"] = state["error"] or ("Nothing was recorded: " + (log_tail() or
                                                                        "ffmpeg didn't start"))
        draw()
        app.show()
        return
    out = state["out"]
    if len(segs) == 1:
        if segs[0] != out:
            os.replace(segs[0], out)
        state["mode"] = "idle"
        app.active(False)
        open_clip(out)
        app.show()
        return
    state["mode"] = "joining"
    take = state["take"]

    def work():
        tmp = os.path.join(OUT_DIR, f".{os.path.basename(out)}.joining.mp4")
        try:
            ff.concat(state["ffmpeg"], segs, tmp, os.path.join(app.data_dir, "parts.txt"))
            os.replace(tmp, out)
            for p in segs:
                if p != out:
                    try:
                        os.remove(p)  # the parts are now inside `out`
                    except OSError:
                        pass
            post({"type": "x-joined", "take": take, "path": out})
        except (RuntimeError, OSError) as e:
            post({"type": "x-joined", "take": take, "path": segs[0],
                  "error": f"Couldn't join the {len(segs)} parts ({e}); they're in the folder "
                           "as separate files."})
    threading.Thread(target=work, daemon=True).start()
    draw()


@app.on_message("x-picked")
def on_picked(m):
    if state["mode"] != "picking":
        return
    state["mode"] = "idle"
    if m.get("cancel"):
        if m.get("error"):
            state["error"] = "Region picker: " + m["error"]
        draw()
        return
    begin((m["x"], m["y"], m["w"], m["h"]))


@app.on_message("x-begin")
def on_begin(m):
    if state["mode"] == "picking" and m.get("take") == state["take"]:
        state["mode"] = "idle"
        begin(m["rect"])


@app.on_message("x-go")
def on_go(m):
    if state["mode"] == "countdown" and m.get("take") == state["take"]:
        start_segment()


@app.on_message("x-capturing")
def on_capturing(m):
    if m.get("take") == state["take"] and m.get("idx") == len(state["segments"]) - 1 and \
            state["mode"] == "recording":
        state["capturing"] = True
        state["seg_started"] = time.monotonic()
        draw()


@app.on_message("x-seg-ended")
def on_seg_ended(m):
    if m.get("take") != state["take"]:
        return
    procs["recorder"] = None
    path = m["path"]
    ok = os.path.isfile(path) and os.path.getsize(path) > 0
    mode = state["mode"]
    if not ok:
        state["error"] = "Recording failed: " + (log_tail() or f"ffmpeg exited with {m['code']}")
    if mode == "recording":  # ffmpeg stopped on its own, not because of Stop or Pause
        state["acc"] = elapsed()
        if ok:
            state["error"] = "ffmpeg stopped recording early: " + log_tail()
        state["mode"] = "stopping"
        finish()
    elif mode == "stopping":
        finish()
    elif mode == "paused" and state["resume_pending"]:
        state["resume_pending"] = False
        start_segment()
    else:
        draw()


@app.on_message("x-joined")
def on_joined(m):
    if m.get("take") != state["take"]:
        return
    state["mode"] = "idle"
    app.active(False)
    if m.get("error"):
        state["error"] = m["error"]
    open_clip(m["path"])
    app.show()


# Editor ---------------------------------------------------------------------------

def open_clip(path):
    state.update(view="editor", clip=None, loading=path, confirm_delete=False,
                 exports=[p for p in exports_db.get(path, []) if os.path.isfile(p)])
    state["name_n"] += 1
    exe = state["ffmpeg"]

    def work():
        info = ff.probe(exe, path)
        ff.frame(exe, path, 0, PREVIEW)
        post({"type": "x-clip", "path": path, "info": info})
        if info["duration"] > 0:
            ff.filmstrip(exe, path, info["duration"], STRIP, STRIP_N, STRIP_W, STRIP_H)
        post({"type": "x-preview"})
    if not exe:
        state["loading"] = None
        state["error"] = "Download ffmpeg first to edit recordings."
        state["view"] = "home"
    else:
        try:
            os.remove(STRIP)  # don't show the last clip's filmstrip meanwhile
        except OSError:
            pass
        threading.Thread(target=work, daemon=True).start()
    scan_library()
    draw()


@app.on_message("x-clip")
def on_clip(m):
    if m["path"] != state["loading"]:
        return
    info = m["info"]
    if info["duration"] <= 0:
        state.update(loading=None, view="home",
                     error=f"Couldn't read {os.path.basename(m['path'])}: it may be damaged.")
        draw()
        return
    state["clip"] = {"path": m["path"], **info}
    state.update(loading=None, playhead=0.0)
    state["in"], state["out_t"] = 0.0, info["duration"]
    draw()


def preview_at(t):
    clip = state["clip"]
    if not clip:
        return
    state["playhead"] = t
    path, exe = clip["path"], state["ffmpeg"]
    with preview_lock:
        if preview_job["busy"]:
            preview_job["next"] = t  # only the newest position matters
            return
        preview_job["busy"] = True

    def work(t=t):
        while True:
            ff.frame(exe, path, t, PREVIEW)
            post({"type": "x-preview"})
            with preview_lock:
                t, preview_job["next"] = preview_job["next"], None
                if t is None:
                    preview_job["busy"] = False
                    return
    threading.Thread(target=work, daemon=True).start()


@app.on_message("x-preview")
def on_preview(_m):
    if state["view"] == "editor" and app.page_open:
        draw_page()


def set_trim(which, t):
    dur = state["clip"]["duration"]
    t = min(max(0.0, t), dur)
    if which == "in":
        state["in"] = min(t, max(0.0, state["out_t"] - MIN_LEN))
        preview_at(state["in"])
    else:
        state["out_t"] = max(t, min(dur, state["in"] + MIN_LEN))
        preview_at(max(0.0, state["out_t"] - 0.05))


def start_export():
    clip = state["clip"]
    if state["exporting"] or not clip:
        return
    src = clip["path"]
    start, end = state["in"], state["out_t"]
    if end - start < MIN_LEN:
        state["error"] = "The trimmed part is too short."
        draw()
        return
    kind = settings["format"]
    stem = os.path.splitext(src)[0]
    trimmed = start > 0.05 or end < clip["duration"] - 0.05
    out = unique(f"{stem}{'-trim' if trimmed else '-export'}.{kind}")
    state.update(exporting=f"Exporting {kind.upper()}…", export_frac=0.0,
                 export_started=time.monotonic(), export_cancel=False, error="", note="")
    last = [0.0]

    def progress(frac):
        if frac - last[0] >= 0.02:
            last[0] = frac
            post({"type": "x-export-progress", "frac": frac})

    def on_start(p):
        procs["export"] = p

    def work():
        try:
            ff.export(state["ffmpeg"], src, start, end, out, kind, settings["gif_w"],
                      settings["gif_fps"], settings["mp4_audio"] and clip["audio"], progress,
                      on_start)
            post({"type": "x-exported", "src": src, "path": out})
        except Exception as e:
            post({"type": "x-exported", "src": src, "error": f"{e}"})
    threading.Thread(target=work, daemon=True).start()
    app.active(True)
    draw()


@app.on_message("x-export-progress")
def on_export_progress(m):
    state["export_frac"] = m["frac"]
    if app.page_open:
        draw_page()


@app.on_message("x-exported")
def on_exported(m):
    state["exporting"] = None
    procs["export"] = None
    app.active(False)
    if m.get("error"):
        if state["export_cancel"]:
            state["note"] = "Export cancelled."
        else:
            state["error"] = "Export failed: " + m["error"]
    else:
        exports_db.setdefault(m["src"], []).append(m["path"])
        app.save("exports", exports_db)
        if state["clip"] and state["clip"]["path"] == m["src"]:
            state["exports"].append(m["path"])
        state["note"] = f"Saved {os.path.basename(m['path'])} ({human_size(m['path'])})."
        scan_library()
    draw()


def rename_clip(new):
    clip = state["clip"]
    new = "".join(ch for ch in new.strip() if ch not in '<>:"/\\|?*').strip(" .")
    old = clip["path"]
    state["name_n"] += 1
    if not new or new == os.path.splitext(os.path.basename(old))[0]:
        return
    dst = os.path.join(os.path.dirname(old), new + os.path.splitext(old)[1])
    if os.path.exists(dst):
        state["error"] = f"{os.path.basename(dst)} already exists."
        return
    try:
        os.rename(old, dst)
    except OSError as e:
        state["error"] = f"Couldn't rename: {e.strerror or e}"
        return
    clip["path"] = dst
    if old in exports_db:
        exports_db[dst] = exports_db.pop(old)
        app.save("exports", exports_db)
    state["note"] = f"Renamed to {os.path.basename(dst)}."
    scan_library()


def delete_clip():
    clip = state["clip"]
    if not state["confirm_delete"]:
        state["confirm_delete"] = True
        return
    try:
        screen.trash([clip["path"]])
    except OSError as e:
        state["error"] = str(e)
        state["confirm_delete"] = False
        return
    exports_db.pop(clip["path"], None)
    app.save("exports", exports_db)
    state.update(clip=None, view="home", confirm_delete=False,
                 note=f"Moved {os.path.basename(clip['path'])} to the Recycle Bin.")
    scan_library()


# ffmpeg download ---------------------------------------------------------------

def start_download():
    state.update(dl="Starting…", dl_frac=0.0, dl_error="", error="")
    dl_cancel.clear()
    last = [0.0]

    def progress(text, frac):
        now = time.monotonic()
        if now - last[0] >= 0.25 or frac >= 1:
            last[0] = now
            post({"type": "x-dl", "text": text, "frac": frac})

    def work():
        try:
            ff.download(app.data_dir, progress, dl_cancel.is_set)
            post({"type": "x-dl", "done": True})
        except ff.Cancelled:
            post({"type": "x-dl", "cancelled": True})
        except Exception as e:
            post({"type": "x-dl", "error": f"Download failed: {e}"})
    threading.Thread(target=work, daemon=True).start()
    app.active(True)
    draw()


@app.on_message("x-dl")
def on_dl(m):
    if m.get("done") or m.get("error") or m.get("cancelled"):
        state["dl"] = None
        app.active(False)
        if m.get("done"):
            state["ffmpeg"] = ff.find(app.data_dir)
            state.pop("ff_version", None)
            state["note"] = "ffmpeg is ready. Press Record a region or Ctrl+Alt+M."
        elif m.get("error"):
            state["dl_error"] = m["error"]
        else:
            state["dl_error"] = "Download cancelled. Retry resumes where it stopped."
    else:
        state["dl"], state["dl_frac"] = m["text"], m["frac"]
    draw()


# Events ---------------------------------------------------------------------------

def reveal(path):
    if os.path.isfile(path):
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"', stdin=subprocess.DEVNULL)
    else:
        os.makedirs(OUT_DIR, exist_ok=True)
        app.open(os.path.dirname(path) if os.path.splitext(path)[1] else path)


def copy_file(path):
    try:
        screen.copy_files([os.path.abspath(path)])
        state["note"] = f"Copied {os.path.basename(path)}: paste it in Explorer, a chat or a mail."
    except OSError as e:
        state["error"] = f"Couldn't copy: {e}"


def open_settings():
    state["view"] = "settings"
    if audio:
        try:
            state["mics"] = audio.microphones()
            state["output"] = audio.output_name()
        except OSError:
            state["mics"] = []


@app.on_message("hotkey")
def on_hotkey(_m):
    mode = state["mode"]
    if mode in ("countdown", "recording", "paused"):
        stop_recording()
    elif mode == "idle":
        pick_region()


@app.on_message("layout")
def on_layout(m):
    if m.get("target") == "page" and m.get("width"):
        state["page_w"] = int(m["width"])
        draw_page()
    elif m.get("target") == "tile":
        app.tile(tile_view())


@app.on_message("page_open")
def on_page_open(_m):
    scan_library()
    draw()


@app.on_message("event")
def on_event(e):
    eid, kind, value = e.get("id", ""), e.get("kind"), e.get("value")
    clip = state["clip"]
    if eid != "delete_clip":
        state["confirm_delete"] = False
    if eid == "record":
        pick_region()
        return
    if eid == "record_page":
        app.close_page()
        pick_region()
        return
    if eid == "record_full":
        quick_start(screen.monitor_at_cursor())
        return
    if eid == "record_last" and state["last_rect"]:
        quick_start(state["last_rect"])
        return
    if eid in ("stop", "stop_page"):
        stop_recording()
        return
    if eid == "pause":
        pause()
        return
    if eid == "resume":
        resume()
        return
    if eid == "discard":
        discard()
        return
    if eid == "download" and not state["dl"]:
        start_download()
        return
    if eid == "dl_cancel":
        dl_cancel.set()
        return
    if eid.startswith("dismiss:"):
        state[eid.split(":", 1)[1]] = ""
    elif eid == "settings":
        open_settings()
    elif eid == "back":
        state["view"] = "home"
        scan_library()
    elif eid == "open_out":
        os.makedirs(OUT_DIR, exist_ok=True)
        app.open(OUT_DIR)
        return
    elif eid.startswith("lib:"):
        try:
            _mt, path, _sz = state["library"][int(eid[4:])]
        except (ValueError, IndexError):
            return
        if path.lower().endswith(".gif"):
            app.open(path)
            return
        open_clip(path)
        return
    elif eid.startswith("set_"):
        key = eid[4:]
        idx = int((value or {}).get("index", 0)) if isinstance(value, dict) else None
        if key == "fps" and idx is not None:
            settings["fps"] = FPS[idx]
        elif key == "quality" and idx is not None:
            settings["quality"] = QUALITIES[idx]
        elif key == "countdown" and idx is not None:
            settings["countdown"] = COUNTDOWNS[idx]
        elif key in ("cursor", "border", "system_audio", "mic"):
            settings[key] = bool(value)
            if key == "mic" and value and state["mics"] is None:
                open_settings()
        save_settings()
    elif eid.startswith("mic:"):
        try:
            settings["mic_id"] = (state["mics"] or [])[int(eid[4:])]["id"]
        except (ValueError, IndexError):
            return
        save_settings()
    elif eid == "format" and isinstance(value, dict):
        settings["format"] = FORMATS[int(value.get("index", 0))]
        save_settings()
    elif eid == "gif_w" and isinstance(value, dict):
        settings["gif_w"] = GIF_WIDTHS[int(value.get("index", 1))]
        save_settings()
    elif eid == "gif_fps" and isinstance(value, dict):
        settings["gif_fps"] = GIF_FPS[int(value.get("index", 1))]
        save_settings()
    elif eid == "mp4_audio":
        settings["mp4_audio"] = bool(value)
        save_settings()
    elif not clip:
        return
    elif eid in ("in", "out"):
        set_trim(eid, float(value or 0))
        if kind == "change":
            app.tile(tile_view())
            draw_page()
            return
    elif eid == "timeline" and isinstance(value, dict):
        width = max(300, state["page_w"])
        t = (float(value.get("x", 0)) - 7) / max(1, width - 14) * clip["duration"]
        set_trim("in" if abs(t - state["in"]) <= abs(t - state["out_t"]) else "out", t)
    elif eid == "reset_trim":
        state["in"], state["out_t"] = 0.0, clip["duration"]
        preview_at(0.0)
    elif eid == "export":
        start_export()
        return
    elif eid == "export_cancel" and procs["export"] is not None:
        state["export_cancel"] = True
        try:
            procs["export"].kill()
        except OSError:
            pass
        return
    elif eid.startswith("name:"):
        if kind != "submit":
            return
        rename_clip(str(value or ""))
    elif eid == "play_clip":
        app.open(clip["path"])
        return
    elif eid == "copy_clip":
        copy_file(clip["path"])
    elif eid == "folder_clip":
        reveal(clip["path"])
        return
    elif eid == "delete_clip":
        delete_clip()
    elif ":" in eid:
        action, idx = eid.split(":", 1)
        try:
            path = state["exports"][int(idx)]
        except (ValueError, IndexError):
            return
        if action == "open_x":
            app.open(path)
            return
        if action == "copy_x":
            copy_file(path)
        elif action == "folder_x":
            reveal(path)
            return
    else:
        return
    draw()


@app.on_message("shutdown")
def on_shutdown(_m):
    # Finish the file properly rather than leaving a broken MP4 behind.
    # Nothing here may raise: an exception would keep the process from exiting.
    dl_cancel.set()
    try:
        rec = procs["recorder"]
        if rec is not None and rec.poll() is None:
            rec.stdin.write(b"q")
            rec.stdin.flush()
    except (OSError, ValueError):
        pass
    for key in ("picker", "export"):
        try:
            p = procs[key]
            if p is not None and p.poll() is None:
                p.kill()
        except OSError:
            pass
    try:
        if procs["frame"] is not None:
            procs["frame"].kill()
    except OSError:
        pass


scan_library()
draw()
app.run()
