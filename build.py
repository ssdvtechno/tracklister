"""Build a double-clickable app for the current OS.

    macOS   -> dist/Tracklister.app      (+ dist/Tracklister-macOS.zip with --zip)
    Windows -> dist/Tracklister.exe      (single file; + dist/Tracklister-Windows.zip with --zip)

Usage: python build.py [--zip]
"""

import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

from tracklister import __version__

ROOT = Path(__file__).resolve().parent
SEP = ";" if sys.platform == "win32" else ":"

args = [
    str(ROOT / "run.py"),
    "--noconfirm",
    "--windowed",
    "--name", "Tracklister",
    "--add-data", f"{ROOT / 'tracklister' / 'ui'}{SEP}tracklister/ui",
    "--collect-submodules", "yt_dlp",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
]
if sys.platform == "darwin":
    args += ["--icon", str(ROOT / "assets" / "icon.icns"), "--osx-bundle-identifier", "io.github.ssdvtechno.tracklister"]
elif sys.platform == "win32":
    args += ["--onefile", "--icon", str(ROOT / "assets" / "icon.ico")]
else:
    args += ["--onefile"]

PyInstaller.__main__.run(args)

if sys.platform == "darwin":
    plist = ROOT / "dist" / "Tracklister.app" / "Contents" / "Info.plist"
    for key, val in (("CFBundleShortVersionString", __version__), ("CFBundleVersion", __version__),
                     ("NSHighResolutionCapable", "true")):
        kind = "-bool" if val == "true" else "-string"
        subprocess.run(["plutil", "-replace", key, kind, val, str(plist)], check=True)

if "--zip" in sys.argv:
    dist = ROOT / "dist"
    if sys.platform == "darwin":
        out = dist / "Tracklister-macOS.zip"
        out.unlink(missing_ok=True)
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(dist / "Tracklister.app"), str(out)], check=True)
    else:
        name = "Tracklister-Windows" if sys.platform == "win32" else "Tracklister-Linux"
        exe = "Tracklister.exe" if sys.platform == "win32" else "Tracklister"
        stage = dist / name
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir()
        shutil.copy2(dist / exe, stage / exe)
        out = Path(shutil.make_archive(str(dist / name), "zip", dist, name))
    print(f"Packaged: {out}")
