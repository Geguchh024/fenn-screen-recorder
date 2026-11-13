"""Audio for Screen Recorder through WASAPI and ctypes (no pywin32): the
system mix (loopback of the default output) and microphones, streamed as raw
PCM into ffmpeg through named pipes, with a live peak level for the meters.

Loopback capture only delivers packets while something plays, so the gaps
are filled with silence by the clock; otherwise the audio would drift ahead
of the video after every quiet moment."""

import array
import ctypes
import msvcrt
import os
import struct
import threading
import time
from ctypes import POINTER, byref, c_int, c_int64, c_uint, c_uint64, c_ulong, c_ushort, c_void_p, \
    c_wchar_p

RENDER, CAPTURE = 0, 1
CLSCTX_ALL = 0x17
DEVICE_STATE_ACTIVE = 1
LOOPBACK = 0x00020000
SILENT = 0x2
WAVE_FORMAT_PCM, WAVE_FORMAT_FLOAT, WAVE_FORMAT_EXTENSIBLE = 1, 3, 0xFFFE

ole32 = ctypes.WinDLL("ole32")
ole32.CoTaskMemFree.argtypes = [c_void_p]
ole32.PropVariantClear.argtypes = [c_void_p]
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateNamedPipeW.restype = c_void_p
kernel32.CreateNamedPipeW.argtypes = [c_wchar_p, c_ulong, c_ulong, c_ulong, c_ulong, c_ulong,
                                      c_ulong, c_void_p]
kernel32.ConnectNamedPipe.argtypes = [c_void_p, c_void_p]
kernel32.CreateFileW.restype = c_void_p
kernel32.CreateFileW.argtypes = [c_wchar_p, c_ulong, c_ulong, c_void_p, c_ulong, c_ulong, c_void_p]
kernel32.CloseHandle.argtypes = [c_void_p]
INVALID = c_void_p(-1).value


class GUID(ctypes.Structure):
    _fields_ = [("d1", c_ulong), ("d2", c_ushort), ("d3", c_ushort), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, text):
        super().__init__()
        ole32.CLSIDFromString(c_wchar_p("{" + text.strip("{}") + "}"), byref(self))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("ptr", c_void_p), ("extra", c_void_p)]


CLSID_MMDeviceEnumerator = GUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioClient = GUID("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = GUID("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
PKEY_FriendlyName = PROPERTYKEY()
PKEY_FriendlyName.fmtid = GUID("A45C254E-DF1C-4EFD-8020-67D146A850E0")
PKEY_FriendlyName.pid = 14


def _call(ptr, index, *args, argtypes=()):
    """Calls method `index` of a COM object's vtable; raises OSError on failure."""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtbl[index])
    return fn(ptr, *args)


def _release(ptr):
    if ptr:
        vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
        ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])(ptr)


def _enumerator():
    ole32.CoInitializeEx(None, 0)  # multithreaded; S_FALSE if already done
    p = c_void_p()
    ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                           byref(IID_IMMDeviceEnumerator), byref(p))
    if not p:
        raise OSError("Windows audio isn't available")
    return p


def _wstr_out(ptr, index):
    s = c_void_p()
    _call(ptr, index, byref(s), argtypes=(POINTER(c_void_p),))
    try:
        return ctypes.wstring_at(s.value) if s.value else ""
    finally:
        ole32.CoTaskMemFree(s)


def _name(dev):
    store = c_void_p()
    try:
        _call(dev, 4, 0, byref(store), argtypes=(c_ulong, POINTER(c_void_p)))
    except OSError:
        return ""
    pv = PROPVARIANT()
    try:
        _call(store, 5, byref(PKEY_FriendlyName), byref(pv),
              argtypes=(POINTER(PROPERTYKEY), POINTER(PROPVARIANT)))
        return ctypes.wstring_at(pv.ptr) if pv.vt == 31 and pv.ptr else ""
    except OSError:
        return ""
    finally:
        ole32.PropVariantClear(byref(pv))
        _release(store)


def _default(enum, flow):
    dev = c_void_p()
    try:
        _call(enum, 4, flow, 0, byref(dev), argtypes=(c_uint, c_uint, POINTER(c_void_p)))
    except OSError:
        return None
    return dev


def microphones():
    """Active capture endpoints: [{"id", "name", "default"}], default first."""
    enum = _enumerator()
    out = []
    try:
        dflt = _default(enum, CAPTURE)
        default_id = _wstr_out(dflt, 5) if dflt else None
        _release(dflt)
        coll = c_void_p()
        _call(enum, 3, CAPTURE, DEVICE_STATE_ACTIVE, byref(coll),
              argtypes=(c_uint, c_ulong, POINTER(c_void_p)))
        try:
            n = c_uint()
            _call(coll, 3, byref(n), argtypes=(POINTER(c_uint),))
            for i in range(n.value):
                dev = c_void_p()
                _call(coll, 4, i, byref(dev), argtypes=(c_uint, POINTER(c_void_p)))
                try:
                    did = _wstr_out(dev, 5)
                    out.append({"id": did, "name": _name(dev) or "Microphone",
                                "default": did == default_id})
                finally:
                    _release(dev)
        finally:
            _release(coll)
    finally:
        _release(enum)
    out.sort(key=lambda d: not d["default"])
    return out


def output_name():
    """Friendly name of the default output, for the system-audio label."""
    enum = _enumerator()
    try:
        dev = _default(enum, RENDER)
        if not dev:
            return ""
        try:
            return _name(dev)
        finally:
            _release(dev)
    finally:
        _release(enum)


def _parse_format(ptr):
    """(ffmpeg sample format, rate, channels, bytes per frame) of a WAVEFORMATEX."""
    raw = ctypes.string_at(ptr, 18)
    tag, channels, rate, _avg, align, bits, extra = struct.unpack("<HHIIHHH", raw)
    if tag == WAVE_FORMAT_EXTENSIBLE and extra >= 22:
        tag = struct.unpack("<I", ctypes.string_at(ptr + 24, 4))[0]  # SubFormat.Data1
    if tag == WAVE_FORMAT_FLOAT and bits == 32:
        fmt = "f32le"
    elif tag == WAVE_FORMAT_PCM and bits in (16, 24, 32):
        fmt = {16: "s16le", 24: "s24le", 32: "s32le"}[bits]
    else:
        raise OSError(f"unsupported audio format (tag {tag}, {bits} bits)")
    return fmt, rate, channels, align


def _peak(data, fmt):
    """Peak level 0..1 of a block of samples (cheap: every 8th sample)."""
    if not data:
        return 0.0
    try:
        if fmt == "f32le":
            a = array.array("f")
            a.frombytes(data[:len(data) - len(data) % 4])
            return min(1.0, max((abs(v) for v in a[::8]), default=0.0))
        if fmt == "s16le":
            a = array.array("h")
            a.frombytes(data[:len(data) - len(data) % 2])
            return max((abs(v) for v in a[::8]), default=0) / 32768
        if fmt == "s32le":
            a = array.array("i")
            a.frombytes(data[:len(data) - len(data) % 4])
            return max((abs(v) for v in a[::8]), default=0) / 2147483648
    except (ValueError, OverflowError):
        pass
    return 0.0


class Source:
    """One audio input streamed into a named pipe for ffmpeg.

        src = Source("system")            # or Source("mic", device_id)
        src.open()                        # raises OSError; then src.fmt etc. are set
        ... start ffmpeg with -f src.fmt -ar src.rate -ac src.channels -i src.pipe
        src.stop()

    Capture starts when ffmpeg opens the pipe and stops when ffmpeg closes it
    or stop() is called."""

    def __init__(self, kind, device_id=None, video_start=None):
        self.kind, self.device_id = kind, device_id
        self.video_start = video_start  # () -> wall-clock time of ffmpeg's first frame
        self.pipe = r"\\.\pipe\fenn-rec-%d-%s-%d" % (os.getpid(), kind, int(time.time() * 1000))
        self.fmt = self.rate = self.channels = None
        self.level = 0.0
        self.error = ""
        self.connected = threading.Event()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._handle = None

    def open(self, timeout=4.0):
        threading.Thread(target=self._run, daemon=True).start()
        if not self._ready.wait(timeout):
            self.error = self.error or "the audio device didn't respond"
        if self.error:
            self.stop()
            raise OSError(self.error)

    def stop(self):
        self._stop.set()
        if not self.connected.is_set() and self._handle:
            # Unblock ConnectNamedPipe when ffmpeg never opened the pipe.
            h = kernel32.CreateFileW(self.pipe, 0x40000000, 0, None, 3, 0, None)
            if h and h != INVALID:
                kernel32.CloseHandle(h)

    def _run(self):
        enum = client = capture = None
        fmt_ptr = c_void_p()
        f = None
        try:
            enum = _enumerator()
            if self.kind == "system":
                dev = _default(enum, RENDER)
            elif self.device_id:
                dev = c_void_p()
                try:
                    _call(enum, 5, c_wchar_p(self.device_id), byref(dev),
                          argtypes=(c_wchar_p, POINTER(c_void_p)))
                except OSError:
                    dev = _default(enum, CAPTURE)  # unplugged: fall back to the default
            else:
                dev = _default(enum, CAPTURE)
            if not dev:
                raise OSError("no output device" if self.kind == "system" else "no microphone")
            client = c_void_p()
            try:
                _call(dev, 3, byref(IID_IAudioClient), CLSCTX_ALL, None, byref(client),
                      argtypes=(POINTER(GUID), c_ulong, c_void_p, POINTER(c_void_p)))
            finally:
                _release(dev)
            _call(client, 8, byref(fmt_ptr), argtypes=(POINTER(c_void_p),))  # GetMixFormat
            self.fmt, self.rate, self.channels, align = _parse_format(fmt_ptr.value)
            flags = LOOPBACK if self.kind == "system" else 0
            _call(client, 3, 0, flags, c_int64(2_000_000), c_int64(0), fmt_ptr, None,  # 200 ms
                  argtypes=(c_int, c_ulong, c_int64, c_int64, c_void_p, c_void_p))
            capture = c_void_p()
            _call(client, 14, byref(IID_IAudioCaptureClient), byref(capture),
                  argtypes=(POINTER(GUID), POINTER(c_void_p)))
            h = kernel32.CreateNamedPipeW(self.pipe, 2, 0, 1, 1 << 20, 1 << 20, 0, None)
            if not h or h == INVALID:
                raise OSError(f"couldn't create the audio pipe ({ctypes.get_last_error()})")
            self._handle = h
            f = os.fdopen(msvcrt.open_osfhandle(h, 0), "wb", buffering=0)
        except OSError as e:
            label = "System audio" if self.kind == "system" else "Microphone"
            self.error = f"{label}: {e.strerror or e}"
            self._ready.set()
            self._cleanup(enum, client, capture, fmt_ptr, f)
            return
        self._ready.set()
        try:
            ok = kernel32.ConnectNamedPipe(self._handle, None) or ctypes.get_last_error() == 535
            if not ok or self._stop.is_set():
                return
            self.connected.set()
            self._pump(client, capture, align, f)
        except OSError:
            pass  # ffmpeg closed the pipe
        finally:
            self._cleanup(enum, client, capture, fmt_ptr, f)

    def _pump(self, client, capture, align, f):
        _call(client, 10)  # Start
        rate = self.rate
        # ffmpeg grabs its first video frame while opening the inputs, before
        # it opens this pipe; start the audio with that much silence so both
        # tracks share the same zero.
        start = self.video_start() if self.video_start else None
        lead = time.time() - start if start else 0.0
        lead = lead if 0 < lead < 3 else 0.0
        written = int(lead * rate)
        f.write(bytes(written * align))
        t0 = time.perf_counter() - lead
        data_p, frames, flags = c_void_p(), c_uint(), c_ulong()
        pos, qpc = c_uint64(), c_uint64()
        packet = c_uint()
        try:
            while not self._stop.is_set():
                got = False
                while True:
                    _call(capture, 5, byref(packet), argtypes=(POINTER(c_uint),))
                    if not packet.value:
                        break
                    _call(capture, 3, byref(data_p), byref(frames), byref(flags), byref(pos),
                          byref(qpc), argtypes=(POINTER(c_void_p), POINTER(c_uint),
                                                POINTER(c_ulong), POINTER(c_uint64),
                                                POINTER(c_uint64)))
                    n = frames.value
                    if flags.value & SILENT or not data_p.value:
                        chunk = bytes(n * align)
                    else:
                        chunk = ctypes.string_at(data_p.value, n * align)
                    _call(capture, 4, c_uint(n), argtypes=(c_uint,))
                    self.level = max(self.level, _peak(chunk, self.fmt))  # held until read
                    f.write(chunk)
                    written += n
                    got = True
                # Fill silence up to the clock, leaving 30 ms for packets in flight.
                behind = int((time.perf_counter() - t0) * rate) - written - rate * 3 // 100
                if behind > 0 and not got:
                    f.write(bytes(behind * align))
                    written += behind
                time.sleep(0.01)
        finally:
            try:
                _call(client, 11)  # Stop
            except OSError:
                pass

    def _cleanup(self, enum, client, capture, fmt_ptr, f):
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
        elif self._handle:
            kernel32.CloseHandle(self._handle)
        self._handle = None
        _release(capture)
        _release(client)
        if fmt_ptr.value:
            ole32.CoTaskMemFree(fmt_ptr)
        _release(enum)
