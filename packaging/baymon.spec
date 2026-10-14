# PyInstaller spec for the Baymon CLI. Build with scripts/build_deb.sh or scripts/build_windows.ps1.
# ONEFILE=1 produces a single executable (Windows); otherwise a folder (faster startup, used by the .deb).
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

onefile = os.environ.get("ONEFILE") == "1"
root = os.path.abspath(os.path.join(SPECPATH, ".."))

hiddenimports = collect_submodules("rich._unicode_data") + collect_submodules("baymon.cli")
if sys.platform != "linux":
    hiddenimports.append("sounddevice")

# The CLI talks to the Baymon API; server-only packages must never be bundled.
excludes = [
    "baymon.api", "baymon.services", "baymon.completions", "baymon.formatting",
    "fastapi", "starlette", "uvicorn", "supabase", "supabase_auth", "postgrest", "storage3",
    "realtime", "groq", "openai", "dotenv", "tkinter", "pytest", "numpy",
]

a = Analysis(
    [os.path.join(SPECPATH, "baymon_cli.py")],
    pathex=[os.path.join(root, "src")],
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

if onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="baymon", console=True, upx=False, strip=False)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="baymon", console=True, upx=False, strip=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="baymon", upx=False, strip=False)
