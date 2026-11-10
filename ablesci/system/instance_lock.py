"""Single-instance lock file (data/.kytdl.lock) with stale-lock detection.
It complements the port probe: two services must never drive the same account at once.

The lock holds "PID<TAB>process name<TAB>process start time". A PID alone is not enough: Windows reuses PIDs, and on
2026-11-09 a lock left by a crashed service pointed at a PID that msedge.exe had since taken, so the app refused to
start. A lock is therefore stale when the PID is gone, when the start time of the process now holding that PID differs
from the recorded one, or (old PID-only locks) when that process is clearly not a Python interpreter."""
import os
import sys
from pathlib import Path


class AlreadyRunning(RuntimeError):
    def __init__(self, pid):
        super().__init__("文献通互助 已在运行（PID %s）" % pid)
        self.pid = pid


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5   # access denied: the process exists
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_identity(pid):
    """(executable name in lower case, start time as an opaque string) of a running process, or None when unknown."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                created, exited, kern, user = (wintypes.FILETIME() for _ in range(4))
                if not kernel.GetProcessTimes(ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(exited),
                                              ctypes.byref(kern), ctypes.byref(user)):
                    return None
                size = wintypes.DWORD(1024)
                buffer = ctypes.create_unicode_buffer(size.value)
                name = ""
                if kernel.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buffer, ctypes.byref(size)):
                    name = Path(buffer.value).name.lower()
                return name, str((created.dwHighDateTime << 32) | created.dwLowDateTime)
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
        stat = Path("/proc/%d/stat" % pid)
        if stat.is_file():
            text = stat.read_text()
            name = text[text.index("(") + 1:text.rindex(")")].lower()
            return name, text[text.rindex(")") + 2:].split()[19]   # field 22: starttime
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return None


def looks_like_python(name):
    return "python" in (name or "")


class InstanceLock:
    def __init__(self, path, pid=None, alive=pid_alive, identity=process_identity):
        self.path = Path(path)
        self.pid = pid or os.getpid()
        self.alive = alive
        self.identity = identity
        self.held = False

    def _read(self):
        """(pid, name, start) from the lock file; name/start are None for an old PID-only lock. (None, None, None) if unreadable."""
        try:
            parts = self.path.read_text("utf-8").strip().split("\t")
            return int(parts[0]), (parts[1] if len(parts) > 2 else None), (parts[2] if len(parts) > 2 else None)
        except (OSError, ValueError, IndexError):
            return None, None, None

    def owner(self):
        return self._read()[0]

    def owner_running(self):
        """Is the process recorded in the lock still the service that wrote it?"""
        pid, name, start = self._read()
        if pid is None or not self.alive(pid):
            return False
        now = self.identity(pid)
        if now is None:
            return True   # cannot tell: stay on the safe side and treat it as running
        if start is not None:
            return now[1] == start
        return not now[0] or looks_like_python(now[0])   # old PID-only lock: a non-Python process took the PID over

    def _content(self):
        own = self.identity(self.pid)
        return "%d\t%s\t%s" % (self.pid, own[0] or "?", own[1]) if own else str(self.pid)

    def acquire(self):
        """Take the lock or raise AlreadyRunning. A lock whose process is gone or was replaced (crash, power loss, PID reuse)
        is taken over."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                with open(self.path, "x", encoding="utf-8") as handle:
                    handle.write(self._content())
                self.held = True
                return self
            except FileExistsError:
                owner = self.owner()
                if owner == self.pid:
                    self.held = True
                    return self
                if owner is not None and self.owner_running():
                    raise AlreadyRunning(owner)
                try:
                    self.path.unlink()   # stale, reused PID or unreadable
                except FileNotFoundError:
                    pass
        raise AlreadyRunning(self.owner())

    def release(self):
        if self.held and self.owner() == self.pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.held = False
