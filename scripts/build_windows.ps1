# Build the Baymon CLI for Windows: dist\baymon.exe (single file).
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#   $env:BAYMON_DEFAULT_API_URL = "https://your-api.onrender.com"; .\scripts\build_windows.ps1
#
# Must run on Windows (PyInstaller can't cross-compile). Requires uv.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$buildFile = "src\baymon\cli\_build.py"
$created = $false
if ($env:BAYMON_DEFAULT_API_URL) {
    "DEFAULT_API_URL = `"$($env:BAYMON_DEFAULT_API_URL.TrimEnd('/'))`"" | Set-Content -Encoding utf8 $buildFile
    $created = $true
}
try {
    uv sync --group dev --quiet
    $env:ONEFILE = "1"
    uv run pyinstaller --noconfirm --clean --log-level WARN `
        --distpath dist --workpath build\pyinstaller\work packaging\baymon.spec
    Write-Host "Built dist\baymon.exe"
} finally {
    if ($created) { Remove-Item $buildFile -ErrorAction SilentlyContinue }
    Remove-Item Env:ONEFILE -ErrorAction SilentlyContinue
}
