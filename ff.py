"""ffmpeg: finding it, downloading a static build on request, recording with
gdigrab (plus WASAPI audio through named pipes), probing, preview frames,
filmstrips, joining paused segments and exports."""

import hashlib
import http.client
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

CREATE_NO_WINDOW = 0x08000000
# Every helper ffmpeg gets an empty stdin. Inheriting the plugin's stdin (the
# pipe from Fenn) would let ffmpeg read Fenn's messages as key presses, and a
# child that inherits a pipe with a read pending on it stalls at startup.
QUIET = {"stdin": subprocess.DEVNULL, "creationflags": CREATE_NO_WINDOW}
# gyan.dev's "essentials" static build (libx264, gdigrab, palettegen...). The
# same zip is published on gyan.dev and on its GitHub mirror; GitHub is far
# faster, gyan.dev is the fallback. Either way the file is checked against the
# SHA-256 that gyan.dev publishes for that version before anything is extracted.
VERSION_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"
SHA_URL = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-{v}-essentials_build.zip.sha256"
MIRRORS = ["https://github.com/GyanD/codexffmpeg/releases/download/{v}/ffmpeg-{v}-essentials_build.zip",
           "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-{v}-essentials_build.zip"]
LATEST_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
BUILD_MB = 110
HEADERS = {"User-Agent": "fenn-screen-recorder"}
NET_ERRORS = (urllib.error.URLError, http.client.HTTPException, socket.timeout, ConnectionError,
              TimeoutError)


class Cancelled(Exception):
    pass


def local_exe(data_dir):
    return os.path.join(data_dir, "ffmpeg", "ffmpeg.exe")


def find(data_dir):
    """ffmpeg on PATH, else the copy downloaded into the data folder, else None."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    exe = local_exe(data_dir)
    return exe if os.path.isfile(exe) else None


def version(exe):
    try:
        p = subprocess.run([exe, "-nostdin", "-hide_banner", "-version"], capture_output=True,
                           timeout=10, **QUIET)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(rb"ffmpeg version (\S+)", p.stdout)
    return m.group(1).decode("ascii", "replace") if m else ""


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
        return r.read().decode("ascii", "replace").strip()


def _latest():
    """(version, sha256) of the current release build."""
    v = _get(VERSION_URL)
    if not re.fullmatch(r"[0-9][0-9A-Za-z.\-]*", v):
        raise RuntimeError(f"unexpected release version {v[:40]!r}")
    sha = _get(SHA_URL.format(v=v)).split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise RuntimeError("gyan.dev didn't publish a SHA-256 for this build")
    return v, sha


def _fetch(url, part, progress, cancelled):
    """Appends url to `part`, resuming where it stopped. Returns when the file
    is complete; raises a network error if the connection drops or stalls."""
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    headers = dict(HEADERS)
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:  # already complete
            return
        raise
    with r:
        if have and r.status != 206:  # the server ignored the range: start over
            have = 0
        total = have + int(r.headers.get("Content-Length") or 0)
        host = urllib.parse.urlsplit(url).hostname or ""
        started, base, last = time.monotonic(), have, 0.0
        with open(part, "ab" if have else "wb") as f:
            while True:
                if cancelled():
                    raise Cancelled()
                chunk = r.read(256 * 1024)  # each read times out after 20 s of silence
                if not chunk:
                    break
                f.write(chunk)
                have += len(chunk)
                now = time.monotonic()
                if now - last >= 0.25:
                    last = now
                    speed = (have - base) / max(0.1, now - started)
                    eta = (total - have) / speed if speed > 1 and total else 0
                    progress(f"{have / 1e6:.0f} of {total / 1e6:.0f} MB · {speed / 1e6:.1f} MB/s"
                             f"{f' · {fmt_eta(eta)} left' if eta else ''} · {host}",
                             have / total if total else 0)
        if total and have < total:
            raise ConnectionError(f"the connection closed at {have / 1e6:.0f} of {total / 1e6:.0f} MB")


def fmt_eta(s):
    s = int(s)
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(data_dir, progress, cancelled=lambda: False):
    """Downloads the build, verifies it and extracts ffmpeg.exe. Survives
    dropped and stalled connections by resuming, and switches mirrors when
    one keeps failing. progress(text, fraction) is called with status lines."""
    folder = os.path.join(data_dir, "ffmpeg")
    os.makedirs(folder, exist_ok=True)
    for stale in ("download.zip",):  # left behind by version 1.0
        try:
            os.remove(os.path.join(folder, stale))
        except OSError:
            pass
    progress("Looking up the latest build…", 0)
    try:
        v, expected = _latest()
        urls = [m.format(v=v) for m in MIRRORS]
    except NET_ERRORS + (RuntimeError, IndexError) as e:
        raise RuntimeError(f"couldn't reach gyan.dev ({reason(e)})")
    part = os.path.join(folder, f"ffmpeg-{v}-essentials.zip.part")
    for old in os.listdir(folder):  # partial downloads of older versions
        if old.endswith(".zip.part") and old != os.path.basename(part):
            try:
                os.remove(os.path.join(folder, old))
            except OSError:
                pass
    errors = []
    done = False
    for url in urls:
        for attempt in range(5):
            try:
                _fetch(url, part, progress, cancelled)
                done = True
                break
            except Cancelled:
                raise
            except NET_ERRORS as e:
                errors.append(reason(e))
                if isinstance(e, urllib.error.HTTPError) and e.code in (403, 404, 410):
                    break  # this mirror doesn't have it: try the next one
                for wait in range(2 + attempt * 2, 0, -1):
                    if cancelled():
                        raise Cancelled()
                    progress(f"Connection dropped ({reason(e)}). Resuming in {wait} s…",
                             _frac(part))
                    time.sleep(1)
        if done:
            break
    if not done:
        raise RuntimeError(f"the download kept failing ({errors[-1] if errors else 'no mirror'}). "
                           "Press Retry to resume where it stopped.")
    progress("Checking the SHA-256…", 1)
    if _sha256(part) != expected:
        os.remove(part)
        raise RuntimeError("the file doesn't match its published SHA-256 and was deleted. "
                           "Press Retry to download it again.")
    progress("Extracting ffmpeg.exe…", 1)
    exe = local_exe(data_dir)
    try:
        with zipfile.ZipFile(part) as z:
            name = next((n for n in z.namelist() if n.endswith("/bin/ffmpeg.exe")), None)
            if not name:
                raise RuntimeError("ffmpeg.exe isn't in the zip")
            with z.open(name) as src, open(exe + ".tmp", "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(exe + ".tmp", exe)
    except (zipfile.BadZipFile, OSError) as e:
        raise RuntimeError(f"couldn't extract ffmpeg.exe ({reason(e)})")
    finally:
        for p in (part, exe + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass
    if not version(exe):
        raise RuntimeError("the extracted ffmpeg.exe doesn't run")
    return exe


def _frac(part):
    try:
        return min(1.0, os.path.getsize(part) / (BUILD_MB * 1.05e6))
    except OSError:
        return 0.0


def reason(e):
    """A short human reason for a network or file error."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        e = e.reason if isinstance(e.reason, Exception) else e
        if isinstance(e, socket.gaierror):
            return "no internet connection"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(e, ConnectionResetError):
        return "connection reset"
    text = str(e) or type(e).__name__
    return text[:120]


# Recording --------------------------------------------------------------------

QUALITY = {  # name: (crf, x264 preset)
    "Compact": (30, "veryfast"),
    "Balanced": (24, "veryfast"),
    "Crisp": (18, "faster"),
}


def record(exe, rect, out, log_path, fps=30, cursor=True, quality="Balanced", audio=()):
    """Starts recording rect (x, y, w, h in physical pixels). audio is a list
    of (pipe_name, ffmpeg_sample_format, rate, channels): each is mixed into
    one AAC track. Stop by writing b"q" to the process's stdin."""
    x, y, w, h = rect
    crf, preset = QUALITY.get(quality, QUALITY["Balanced"])
    if w * h * fps > 1920 * 1080 * 30 * 1.2:  # keep up with big regions at high frame rates
        preset = "ultrafast" if fps >= 60 else "superfast"
    args = [exe, "-hide_banner", "-y", "-thread_queue_size", "512",
            "-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1" if cursor else "0",
            "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}",
            "-i", "desktop"]
    for pipe, fmt, rate, channels in audio:
        args += ["-thread_queue_size", "1024", "-f", fmt, "-ar", str(rate), "-ac", str(channels),
                 "-i", pipe]
    args += ["-map", "0:v"]
    if len(audio) == 1:
        args += ["-map", "1:a"]
    elif len(audio) > 1:
        ins = "".join(f"[{i + 1}:a]" for i in range(len(audio)))
        args += ["-filter_complex",
                 f"{ins}amix=inputs={len(audio)}:duration=first:dropout_transition=0:normalize=0[a]",
                 "-map", "[a]"]
    args += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
             "-r", str(fps)]
    if audio:
        args += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    args += ["-movflags", "+faststart", out]
    log = open(log_path, "wb")
    try:
        return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=log, creationflags=CREATE_NO_WINDOW)
    finally:
        log.close()  # the child keeps its own handle


def video_start(log_path):
    """Wall-clock time of the first captured frame, from the recording log
    (gdigrab stamps frames with the wall clock)."""
    try:
        with open(log_path, "rb") as f:
            m = re.search(rb"Input #0, gdigrab.*?start: (\d+\.\d+)", f.read(8192), re.S)
    except OSError:
        return None
    return float(m.group(1)) if m else None


def concat(exe, parts, out, list_path):
    """Joins segments recorded with the same settings, without re-encoding."""
    with open(list_path, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '" + p.replace("\\", "/").replace("'", r"'\''") + "'\n")
    p = subprocess.run([exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "concat",
                        "-safe", "0", "-i", list_path, "-c", "copy", "-movflags", "+faststart",
                        out], capture_output=True, **QUIET)
    if p.returncode != 0 or not os.path.isfile(out):
        err = p.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(err.splitlines()[-1] if err else "ffmpeg couldn't join the parts")
    return out


# Probing and previews -----------------------------------------------------------

def probe(exe, path):
    """{"duration", "width", "height", "audio", "fps"} of a recording."""
    p = subprocess.run([exe, "-nostdin", "-hide_banner", "-i", path], capture_output=True,
                       **QUIET)
    err = p.stderr
    info = {"duration": 0.0, "width": 0, "height": 0, "audio": b"Audio:" in err, "fps": 0}
    m = re.search(rb"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(rb"Video:.*?, (\d{2,5})x(\d{2,5})", err)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(rb"([\d.]+) fps", err)
    if m:
        info["fps"] = round(float(m.group(1)))
    return info


def frame(exe, path, t, out, height=360):
    """Writes the frame at t seconds as a PNG."""
    tmp = out + ".tmp.png"
    subprocess.run([exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{max(0, t):.3f}",
                    "-i", path, "-frames:v", "1", "-vf", f"scale=-2:'min({height},ih)'", tmp],
                   capture_output=True, **QUIET)
    if os.path.isfile(tmp):
        os.replace(tmp, out)  # swap in whole, so Fenn never reads half a PNG
        return True
    return False


def filmstrip(exe, path, duration, out, count=10, tw=112, th=64):
    """Writes `count` thumbnails spread over the clip side by side as one PNG."""
    args = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for i in range(count):
        args += ["-ss", f"{duration * (i + 0.5) / count:.3f}", "-i", path]
    chains = [f"[{i}:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},"
              f"setsar=1[t{i}]" for i in range(count)]
    graph = ";".join(chains) + ";" + "".join(f"[t{i}]" for i in range(count)) + \
        f"hstack=inputs={count}"
    tmp = out + ".tmp.png"
    subprocess.run(args + ["-filter_complex", graph, "-frames:v", "1", tmp], capture_output=True,
                   **QUIET)
    if os.path.isfile(tmp):
        os.replace(tmp, out)
        return True
    return False


# Export -------------------------------------------------------------------------

def export(exe, src, start, end, out, kind, gif_width=640, gif_fps=15, audio=True,
           progress=None, on_start=None):
    """Trims [start, end] and writes MP4 or GIF. progress(fraction) while
    working; on_start(process) lets the caller cancel it."""
    args = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}", "-i", src]
    if kind == "gif":
        args += ["-vf", f"fps={gif_fps},scale='min({gif_width},iw)':-2:flags=lanczos,split[a][b];"
                        "[a]palettegen=stats_mode=diff[p];"
                        "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
                 "-loop", "0"]
    else:
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart"]
        args += ["-c:a", "aac", "-b:a", "160k"] if audio else ["-an"]
    args += ["-progress", "pipe:1", "-nostats", out]
    length = max(0.1, end - start)
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         **QUIET)
    if on_start:
        on_start(p)
    for line in p.stdout:
        if line.startswith(b"out_time_us=") and progress:
            try:
                progress(min(1.0, int(line.split(b"=")[1]) / 1e6 / length))
            except ValueError:
                pass
    err = p.stderr.read().decode("utf-8", "replace").strip()
    if p.wait() != 0:
        try:
            os.remove(out)
        except OSError:
            pass
        raise RuntimeError(err.splitlines()[-1] if err else f"ffmpeg exited with {p.returncode}")
    return out
