"""Self-update from GitHub.

On start (and when asked "check for updates") Moo reads version.json from the
public repo. If it's newer, she offers to update; on yes she downloads the
code, copies it over her folder (your settings, keys, memories and
profile_seed.json are untouched; they live elsewhere or aren't in the repo),
installs any new components, and restarts.
"""
import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

from . import __version__

log = logging.getLogger("moo")
APP_DIR = Path(__file__).resolve().parent.parent
KEEP = {".venv", "profile_seed.json", ".git"}  # never overwritten or removed


def _v(text):
    try:
        return tuple(int(x) for x in str(text).strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


def check(repo: str):
    """-> (newer_version, notes) or (None, None)."""
    url = f"https://raw.githubusercontent.com/{repo}/main/version.json"
    r = requests.get(url, timeout=10, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    info = r.json()
    latest = info.get("version", "0")
    if _v(latest) > _v(__version__):
        return latest, info.get("notes", "")
    return None, None


def install(repo: str, progress=lambda s: None) -> bool:
    progress("Downloading the update")
    r = requests.get(f"https://codeload.github.com/{repo}/zip/refs/heads/main", timeout=60)
    r.raise_for_status()
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            z.extractall(tmp)
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if not roots or not (roots[0] / "moo" / "__init__.py").exists():
            raise RuntimeError("The download didn't look like Moo.")
        src = roots[0]
        progress("Installing the update")
        for item in src.iterdir():
            if item.name in KEEP:
                continue
            dest = APP_DIR / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
    progress("Updating components")
    py = Path(sys.executable)
    py = py.with_name("python.exe") if py.name.lower() == "pythonw.exe" else py
    res = subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(APP_DIR / "requirements.txt")],
                         capture_output=True, text=True, timeout=900,
                         creationflags=0x08000000 if os.name == "nt" else 0)
    if res.returncode != 0:
        log.warning("pip during update failed: %s", res.stderr[-2000:])
    return True


def restart():
    """Start a fresh Moo and let the caller close this one."""
    pyw = Path(sys.executable)
    if os.name == "nt":
        pyw = pyw.with_name("pythonw.exe")
    subprocess.Popen([str(pyw), str(APP_DIR / "run_moo.pyw")], cwd=str(APP_DIR),
                     creationflags=0x00000008 if os.name == "nt" else 0)  # detached
