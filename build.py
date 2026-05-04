"""
Build script for smsFetcher.exe (portable Windows binary).

Generates icon.ico from the same logic the tray uses, then invokes PyInstaller.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Generate icon.ico from the in-app icon factory
sys.path.insert(0, str(ROOT))
from smsFetcher import make_icon_image  # noqa: E402

ICO_PATH = ROOT / "icon.ico"
img = make_icon_image()
img.save(ICO_PATH, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
print(f"wrote {ICO_PATH}")

# Clean previous build outputs
for d in ("build", "dist"):
    p = ROOT / d
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
spec = ROOT / "smsFetcher.spec"
if spec.exists():
    spec.unlink()

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--noconsole",
    "--name", "smsFetcher",
    "--icon", str(ICO_PATH),
    "--distpath", str(ROOT),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
    "--clean",
    str(ROOT / "smsFetcher.py"),
]
print("running:", " ".join(cmd))
subprocess.check_call(cmd)
print("\nbuild complete:", ROOT / "smsFetcher.exe")
