"""Which Python and which browser to use, without changing system settings.
Browser order: a browser placed in runtime/browsers (optional) > installed Google Chrome > installed Microsoft Edge
(every Windows 10/11 has it) > Playwright's own Chromium (only if `python -m playwright install chromium` was run)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # project folder (data/ lives here), two levels above ablesci/<area>/


def python_executable(root=ROOT, windowed=False):
    root = Path(root)
    name = "pythonw.exe" if windowed else "python.exe"
    portable = root / "runtime" / "python" / name
    return portable if portable.is_file() else root / ".venv" / "Scripts" / name


def bundled_browser(root=ROOT):
    folder = Path(root) / "runtime" / "browsers"
    for pattern in ("chromium-*/chrome-win64/chrome.exe", "chromium-*/chrome-win/chrome.exe"):
        for candidate in sorted(folder.glob(pattern), reverse=True):
            if candidate.is_file():
                return candidate
    return None


def _installed(parts, env=None):
    env = os.environ if env is None else env
    for base in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if env.get(base) and Path(env[base], *parts).is_file():
            return True
    return False


def installed_channel(env=None):
    """'chrome' | 'msedge' | None (then Playwright's own Chromium is used)."""
    if _installed(("Google", "Chrome", "Application", "chrome.exe"), env):
        return "chrome"
    if _installed(("Microsoft", "Edge", "Application", "msedge.exe"), env):
        return "msedge"
    return None


def browser_options(root=ROOT):
    browser = bundled_browser(root)
    if browser:
        return {"executable_path": str(browser)}
    channel = installed_channel()
    return {"channel": channel} if channel else {}
