# Screen Recorder

> Record a region, a window or the whole screen to MP4 with system audio and
> your mic. Then trim it on a filmstrip and export MP4 or GIF, all from the
> notch. Press **Ctrl+Alt+M** to start.

![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![Python](https://img.shields.io/badge/python-3.8%2B-3776AB)
![Version](https://img.shields.io/badge/version-2.0.0-7C5CFF)
![Encoder](https://img.shields.io/badge/encoder-ffmpeg%20%2F%20x264-007808)
![Plugin for](https://img.shields.io/badge/plugin%20for-Fenn-111)

A plugin for **Fenn**, the floating notch and taskbar for Windows.

| | |
|---|---|
| **Hotkey** | `Ctrl+Alt+M` (pick a region, then stop) |
| **Tile** | small (wide adds record and pause buttons) |
| **Page** | 640 × 460 |
| **Output** | MP4 (H.264 + AAC), GIF |
| **Dependencies** | ffmpeg (uses yours, or downloads it on request); tkinter |

## Highlights

- **Region, window or full screen.** A live overlay lets you drag a region or
  click a window.
- **System audio and microphone** captured with WASAPI and mixed into one track.
- **Countdown, real pause and resume, and a click-through border** that never
  shows up in the video.
- **Built-in editor:** filmstrip, timeline and precise trim handles.
- **Export MP4 or GIF** with clean palettes, progress and time left.
- **Copy the file** to paste into Explorer, chat apps and mail.
- **No extra Python packages.** Everything outside ffmpeg is done with `ctypes`.

## Install

Clone the repo into Fenn's plugins folder. The folder name must be `screen-recorder`:

```powershell
git clone https://github.com/Geguchh024/fenn-screen-recorder "$env:APPDATA\Fenn\plugins\screen-recorder"
```

You can also download the ZIP and extract it to the same place. If `ffmpeg`
isn't on your `PATH`, the page offers a one-click download (see
[ffmpeg](#ffmpeg)).

## Recording

- **Ctrl+Alt+M**, **Record a region** on the page, or the record button on a
  wide tile opens the region picker, a dimmed live overlay. **Drag** a region
  or **click** a window, and recording starts as soon as you let go. **F**
  takes the whole monitor; **Esc** or right-click cancels.
- The page also has **Full screen** (the monitor Fenn is on) and **Last
  region**, which start without the picker.
- Clicking the small tile opens the page.
- A **countdown** (off, 3 or 5 s) is shown in the middle of the region. Then
  a **border** marks the region while it records. The border is drawn just
  outside the region and lets clicks through, so it never ends up in the video.
- While recording, the pill shows a **ring with the elapsed time**, and the
  tile turns into a **stop** button (plus **pause** on a wide tile). The page
  shows the time, live **audio meters**, **Pause / Resume**, **Stop and
  edit** and **Discard** (to the Recycle Bin). Ctrl+Alt+M stops, or cancels
  the countdown.
- **Pause** really pauses: nothing is recorded until **Resume**. When you
  stop, the parts are joined into one file without re-encoding.

### Settings (the gear)

| Setting | Options |
|---|---|
| Frame rate | 15, 30, 60 |
| Quality | Compact, Balanced, Crisp (x264 CRF 30, 24, 18) |
| Countdown | off, 3 s, 5 s |
| Mouse cursor | on / off |
| Border | on / off |
| System audio | on / off |
| Microphone | on / off, and which one |

## Editing

When you stop, the page opens on the recording:

- A **filmstrip** of the clip, with a **timeline** under it. Click the
  timeline to move the nearest trim handle there. The **Start / End** sliders
  trim precisely and show the frame under the handle while you drag.
- **Export MP4** (H.264 CRF 21, with or without audio) or **Export GIF** (480,
  640 or 800 px wide at 10, 15 or 24 fps, using `palettegen` + `paletteuse`
  for clean colors). Both show progress, time left and a **Cancel** button.
  Exports are saved next to the recording as `…-trim.mp4/.gif`.
- **Rename** in the name field (press Enter), **Play** in your video player,
  **Copy** the file the way Explorer does (`CF_HDROP`, so it pastes into
  Explorer, chat apps and mail), **Show in Explorer**, or **Delete** (click
  twice; it goes to the Recycle Bin).

The home page lists **recent recordings and exports**. Click a recording to
edit it again.

Recordings are saved to `Videos\Fenn Recordings\rec-<date>-<time>.mp4`.

## Audio

System audio is captured with **WASAPI loopback** of the default output, and
the microphone with WASAPI too, both through `ctypes` with no extra packages.
Each one streams raw PCM into ffmpeg through a named pipe, and they're mixed
into one AAC track.

Loopback only delivers sound while something plays, so the plugin fills the
silences using the clock. The audio also starts with as much silence as
ffmpeg took to grab its first frame, so sound and picture line up.

## ffmpeg

Recording uses ffmpeg with `gdigrab`. If `ffmpeg` is on `PATH`, that one is
used. Otherwise the page offers a **Download** button:

- It fetches gyan.dev's static "essentials" build (about 110 MB) from its
  GitHub mirror, which is much faster than gyan.dev itself, and falls back to
  gyan.dev.
- Before extracting, the zip is checked against the SHA-256 that gyan.dev
  publishes for that version.
- A dropped or stalled connection resumes where it stopped, with up to five
  tries per mirror. The page shows speed and time left. **Cancel** keeps the
  partial file, so **Retry** continues it.
- Only `ffmpeg.exe` is extracted into the plugin's data folder, and the zip is
  deleted.

Nothing is downloaded until you press the button, and the plugin makes no
other network calls.

Stopping sends ffmpeg `q` on stdin, so the MP4 is always finalized. If Fenn
quits mid-recording, the file is finalized the same way.

## Under the hood: why its own windows, and why tkinter

A plugin can only open its own page, and Fenn's canvas only reports clicks,
so drawing a region needs a window run by the plugin. `picker.py` uses
**tkinter**, which ships with the python.org installer.

- The picker is **live** rather than a frozen screenshot, so it opens at once.
  A plugin can't tell Fenn's panel to fold, but under a live overlay the
  panel simply folds away as the mouse moves.
- Two windows make the picker: a translucent black sheet that takes the mouse
  and keyboard, with a clear hole where the region is, and a click-through
  layer above it for the crisp outline, size label and hint.
- The picker is per-monitor DPI aware, so the region is in physical pixels,
  the same units ffmpeg's `gdigrab` captures in.
- Before the overlay appears, it lists the visible windows (using DWM frame
  bounds, and leaving out Fenn's own), so hovering outlines a window and a
  click takes it.
- `frame.py` is the countdown and border: a topmost, click-through tkinter
  window with a transparent middle.

Every helper process (the picker, and ffmpeg for previews and exports) gets
an empty stdin. The plugin's own stdin is the pipe Fenn talks through. A child
that inherited it could read Fenn's messages, and on Windows a child that
inherits a pipe with a read pending stalls at startup until that read
completes.

## Data

Everything lives in `%APPDATA%\Fenn\plugin-data\screen-recorder\`:

| File | What it is |
|---|---|
| `ffmpeg\ffmpeg.exe` | Downloaded ffmpeg, if you used the button |
| `settings.json` | Your recording settings |
| `last_rect.json` | The region for *Last region* |
| `exports.json` | Which exports belong to which recording |
| `preview.png`, `strip.png` | The editor's frame and filmstrip |
| `ffmpeg-record.log` | The last recording's ffmpeg output, for troubleshooting |

## Files

| File | What it is |
|---|---|
| `main.py` | Tile, pages, recording state, editor and exports |
| `ff.py` | Finding or downloading ffmpeg, recording, probing, previews, filmstrips, joining and exports |
| `audio.py` | WASAPI loopback and microphone capture into named pipes, with live levels |
| `picker.py` | The live region and window picker (tkinter) |
| `frame.py` | The countdown and click-through recording border (tkinter) |
| `screen.py` | Screen capture and the file clipboard (`ctypes`) |
| `fenn.py` | The Fenn plugin helper |
| `plugin.toml` | Plugin manifest |

## Requirements

- Windows 10 or 11
- Python 3.8+ on `PATH` as `python`, with tkinter (included in the python.org installer)
- ffmpeg on `PATH`, or let the plugin download it
