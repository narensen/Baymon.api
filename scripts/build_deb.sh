#!/usr/bin/env bash
# Build the Baymon CLI as a Debian/Ubuntu package: dist/baymon_<version>_<arch>.deb
#
#   scripts/build_deb.sh
#   BAYMON_DEFAULT_API_URL=https://your-api.onrender.com scripts/build_deb.sh   # bake in your server
#
# Install with: sudo apt install ./dist/baymon_<version>_<arch>.deb
set -euo pipefail
cd "$(dirname "$0")/.."

uv sync --group dev --quiet
read -r VERSION AUTHOR < <(uv run python - <<'PY'
import tomllib
p = tomllib.load(open("pyproject.toml", "rb"))["project"]
a = p.get("authors", [{}])[0]
print(p["version"], f"{a.get('name', 'Baymon')} <{a.get('email', 'noreply@example.com')}>")
PY
)
ARCH=$(dpkg --print-architecture)
BUILD_FILE=src/baymon/cli/_build.py

if [[ -n "${BAYMON_DEFAULT_API_URL:-}" ]]; then
  echo "DEFAULT_API_URL = \"${BAYMON_DEFAULT_API_URL%/}\"" > "$BUILD_FILE"
  trap 'rm -f "$BUILD_FILE"' EXIT
fi

uv run pyinstaller --noconfirm --clean --log-level WARN \
  --distpath build/pyinstaller/dist --workpath build/pyinstaller/work packaging/baymon.spec

PKG="build/deb/baymon_${VERSION}_${ARCH}"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/bin"
cp -r build/pyinstaller/dist/baymon "$PKG/opt/baymon"
ln -s /opt/baymon/baymon "$PKG/usr/bin/baymon"

cat > "$PKG/DEBIAN/control" <<EOF
Package: baymon
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Recommends: alsa-utils
Maintainer: ${AUTHOR}
Installed-Size: $(du -sk "$PKG/opt" | cut -f1)
Homepage: https://github.com/
Description: Baymon - personal AI companion and coding agent for the terminal
 Chat, talk hands-free, and let Baymon read, edit and run code in your
 projects. Baymon remembers what matters about you across conversations.
EOF

mkdir -p dist
dpkg-deb --build --root-owner-group "$PKG" dist/ >/dev/null
echo "Built dist/baymon_${VERSION}_${ARCH}.deb"
