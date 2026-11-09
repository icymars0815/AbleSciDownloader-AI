"""Module-level helpers shared by app.py and the api_* route modules."""
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ".token"


def write_token(data_root, token):
    """The per-run API token for the local command line (kytdl_client reads it). Rewritten on every start."""
    path = Path(data_root) / TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")


def now_stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def open_with_system(path):
    if not hasattr(os, "startfile"):
        raise ValueError("当前系统不支持直接打开")
    os.startfile(path)

