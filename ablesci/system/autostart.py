"""Optional start-with-Windows: a shortcut in the user's Startup folder (shell:startup) that runs
pythonw.exe from the project .venv with "-m ablesci start". No registry, no administrator rights; default OFF."""
import os
import sys
from ablesci.system.runtime_paths import python_executable
from pathlib import Path

LINK_NAME = "KYTDL Background Monitor.lnk"   # ASCII: WScript.Shell mangles non-ASCII shortcut names
CMD_NAME = "KYTDL Background Monitor.cmd"


def default_startup_dir():
    base = os.environ.get("APPDATA")
    if not base:
        raise OSError("找不到 APPDATA，无法定位“启动”文件夹")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


class Autostart:
    def __init__(self, root, startup_dir=None, allowed=True):
        self.root = Path(root)
        self.startup_dir = Path(startup_dir) if startup_dir else None
        self.last_error = ""
        self.allowed = allowed   # False for isolated servers: they must never touch the real Startup folder

    def folder(self):
        return self.startup_dir or default_startup_dir()

    def paths(self):
        folder = self.folder()
        return folder / LINK_NAME, folder / CMD_NAME

    def target(self):
        return python_executable(self.root, windowed=True)

    def enabled(self):
        if not self.allowed:
            return False
        try:
            return any(p.exists() for p in self.paths())
        except OSError:
            return False

    def enable(self):
        if not self.allowed:
            raise ValueError("当前为隔离运行环境，不修改“启动”文件夹")
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)
        link, cmd = self.paths()
        try:
            self._shortcut(link)
            cmd.unlink(missing_ok=True)
            return "lnk"
        except Exception as exc:
            self.last_error = repr(exc)
            # PowerShell unavailable: a tiny .cmd in the same folder starts the same command without a console window.
            line = '@echo off\r\ncd /d "%s"\r\nstart "" "%s" -m ablesci start\r\n' % (self.root, self.target())
            cmd.write_bytes(line.encode("mbcs" if sys.platform == "win32" else "utf-8"))
            link.unlink(missing_ok=True)
            return "cmd"

    def disable(self):
        if not self.allowed:
            raise ValueError("当前为隔离运行环境，不修改“启动”文件夹")
        for path in self.paths():
            path.unlink(missing_ok=True)

    def _shortcut(self, link):
        """A real .lnk through Windows PowerShell's WScript.Shell object (no extra Python package, no console window)."""
        import subprocess

        def q(text):
            return "'" + str(text).replace("'", "''") + "'"
        script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s);$s.TargetPath=%s;$s.Arguments='-m ablesci start';"
                  "$s.WorkingDirectory=%s;$s.Description='AbleSciDownloader-AI background service (no window)';$s.WindowStyle=7;$s.Save()"
                  % (q(link), q(self.target()), q(self.root)))
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not link.exists():
            raise OSError("shortcut was not created")
