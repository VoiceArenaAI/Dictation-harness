import ctypes
import ctypes.util
import subprocess
import time

_ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode(), "big")


class _Addr(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


_SYSTEM = 1
_DEFAULT_INPUT = _Addr(_fourcc("dIn "), _fourcc("glob"), 0)
_DEVICES = _Addr(_fourcc("dev#"), _fourcc("glob"), 0)
_NAME = _Addr(_fourcc("lnam"), _fourcc("glob"), 0)
_INPUT_STREAMS = _Addr(_fourcc("stm#"), _fourcc("inpt"), 0)

_ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
                                               ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
_ca.AudioObjectSetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
                                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
_cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFRelease.argtypes = [ctypes.c_void_p]


def _size(obj: int, addr: _Addr) -> int:
    size = ctypes.c_uint32(0)
    _ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size))
    return size.value


def _name(dev: int) -> str:
    ref = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    if _ca.AudioObjectGetPropertyData(dev, ctypes.byref(_NAME), 0, None, ctypes.byref(size), ctypes.byref(ref)):
        return ""
    buf = ctypes.create_string_buffer(512)
    _cf.CFStringGetCString(ref, buf, 512, 0x08000100)
    _cf.CFRelease(ref)
    return buf.value.decode()


def input_devices() -> dict[str, int]:
    n = _size(_SYSTEM, _DEVICES) // 4
    ids = (ctypes.c_uint32 * n)()
    size = ctypes.c_uint32(n * 4)
    _ca.AudioObjectGetPropertyData(_SYSTEM, ctypes.byref(_DEVICES), 0, None, ctypes.byref(size), ids)
    return {_name(d): d for d in ids if _size(d, _INPUT_STREAMS) > 0}


def get_default_input() -> int:
    dev = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    _ca.AudioObjectGetPropertyData(_SYSTEM, ctypes.byref(_DEFAULT_INPUT), 0, None, ctypes.byref(size),
                                   ctypes.byref(dev))
    return dev.value


def set_default_input(dev: int) -> None:
    val = ctypes.c_uint32(dev)
    err = _ca.AudioObjectSetPropertyData(_SYSTEM, ctypes.byref(_DEFAULT_INPUT), 0, None, 4, ctypes.byref(val))
    if err:
        raise RuntimeError(f"couldn't set default input (OSStatus {err})")


def find_input(substr: str) -> tuple[int, str]:
    for name, dev in input_devices().items():
        if substr.lower() in name.lower():
            return dev, name
    raise SystemExit(f"No input device matching '{substr}'. Inputs: {list(input_devices())}")


def device_name(dev: int) -> str:
    return _name(dev)


_RUNNING_SOMEWHERE = _Addr(_fourcc("gone"), _fourcc("glob"), 0)


def is_running_somewhere(dev: int) -> bool:
    val = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    err = _ca.AudioObjectGetPropertyData(dev, ctypes.byref(_RUNNING_SOMEWHERE), 0, None, ctypes.byref(size),
                                         ctypes.byref(val))
    if err:
        raise RuntimeError(f"couldn't read device state (OSStatus {err})")
    return bool(val.value)


def running_inputs() -> list[str]:
    return [name for name, dev in input_devices().items() if is_running_somewhere(dev)]


_PID_TO_PROCESS = _Addr(_fourcc("id2p"), _fourcc("glob"), 0)
_PROCESS_RUNNING_INPUT = _Addr(_fourcc("piri"), _fourcc("glob"), 0)


def process_object(pid: int) -> int:
    qualifier = ctypes.c_int32(pid)
    obj = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    err = _ca.AudioObjectGetPropertyData(_SYSTEM, ctypes.byref(_PID_TO_PROCESS), 4, ctypes.byref(qualifier),
                                         ctypes.byref(size), ctypes.byref(obj))
    return 0 if err else obj.value


def process_input_running(obj: int) -> bool | None:
    val = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    err = _ca.AudioObjectGetPropertyData(obj, ctypes.byref(_PROCESS_RUNNING_INPUT), 0, None, ctypes.byref(size),
                                         ctypes.byref(val))
    return None if err else bool(val.value)


class CaptureFlag:

    def __init__(self, app_path: str) -> None:
        self.app_path, self.pids, self.obj = app_path, [], 0

    def refresh(self) -> None:
        out = subprocess.run(["pgrep", "-f", self.app_path], capture_output=True, text=True).stdout
        self.pids, self.obj = [int(p) for p in out.split()], 0

    def find(self) -> int:
        for pid in self.pids:
            obj = process_object(pid)
            if obj and process_input_running(obj):
                return obj
        return 0

    def wait(self, timeout: float) -> bool:
        t0 = time.monotonic()
        while not self.obj and time.monotonic() - t0 < timeout:
            self.obj = self.find()
            time.sleep(0.005)
        return bool(self.obj)

    def running(self) -> bool | None:
        return process_input_running(self.obj) if self.obj else None


_PROCESS_LIST = _Addr(_fourcc("prs#"), _fourcc("glob"), 0)
_PROCESS_PID = _Addr(_fourcc("ppid"), _fourcc("glob"), 0)


def capturing_processes() -> dict[int, int]:
    n = _size(_SYSTEM, _PROCESS_LIST) // 4
    ids = (ctypes.c_uint32 * n)()
    size = ctypes.c_uint32(n * 4)
    _ca.AudioObjectGetPropertyData(_SYSTEM, ctypes.byref(_PROCESS_LIST), 0, None, ctypes.byref(size), ids)
    found = {}
    for obj in ids:
        if process_input_running(obj):
            pid, size = ctypes.c_int32(0), ctypes.c_uint32(4)
            if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(_PROCESS_PID), 0, None, ctypes.byref(size),
                                              ctypes.byref(pid)) == 0:
                found[pid.value] = obj
    return found
