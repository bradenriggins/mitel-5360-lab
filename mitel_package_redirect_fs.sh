#!/usr/bin/env bash
set -euo pipefail

ROOT="${RESEARCH_DIR:-./research}"
TOOLKIT="$ROOT/extracted/toolkit-full"
SOURCE="$ROOT/redirect-fs-apartment"
OUT_NAME="ApartmentLabRedirectFS.spx"
JAVA="/opt/homebrew/opt/openjdk/bin/java"
CP="$TOOLKIT/dist/HtmlAppPackagerAndInstaller.jar:$TOOLKIT/lib/*"

"$JAVA" -cp "$CP" packager.PackagerApp --set-keys-location "$TOOLKIT/keys" >/dev/null
mkdir -p "$ROOT/build"
rm -f "$SOURCE/$OUT_NAME" "$ROOT/build/$OUT_NAME" "${STATIC_FILE_DIR:-./static-files}/$OUT_NAME"
"$JAVA" -cp "$CP" packager.PackagerApp \
  -d "$SOURCE" \
  -f "$OUT_NAME" \
  -v ApartmentRedirect \
  -r 0.1 \
  -p "Mitel Licensed Applications"
mv "$SOURCE/$OUT_NAME" "$ROOT/build/$OUT_NAME"
cp "$ROOT/build/$OUT_NAME" "${STATIC_FILE_DIR:-./static-files}/$OUT_NAME"
shasum -a 256 "$ROOT/build/$OUT_NAME"
