#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MOBILE_DIR="$PROJECT_DIR/mobile"
ANDROID_DIR="$MOBILE_DIR/android"
APK_NAME="aoitalk-mobile.apk"
RELEASE_REPO="ttttdiva/AoiTalk"
VERSION=$(cd "$PROJECT_DIR" && node -e "console.log(require('./mobile/app.json').expo.version)")
if [ -z "$VERSION" ]; then
  echo "[ERROR] Failed to read mobile/app.json expo.version"
  exit 1
fi
ARTIFACT_DIR="$PROJECT_DIR/artifacts/releases/mobile/v${VERSION}"
APK_PATH="$ARTIFACT_DIR/$APK_NAME"

if [ -z "${JAVA_HOME:-}" ]; then
  JAVA_HOME="$(find "/c/Program Files/Microsoft" -maxdepth 1 -type d -name 'jdk-17*' 2>/dev/null | sort -r | head -n 1)"
fi
if [ -z "${JAVA_HOME:-}" ]; then
  echo "[ERROR] JAVA_HOME is not set and a Microsoft JDK 17 installation was not found."
  exit 1
fi
if [ -z "${ANDROID_HOME:-}" ]; then
  ANDROID_HOME="${LOCALAPPDATA:-$HOME/AppData/Local}/Android/Sdk"
  if command -v cygpath >/dev/null 2>&1; then
    ANDROID_HOME="$(cygpath -u "$ANDROID_HOME")"
  fi
fi
export JAVA_HOME
export ANDROID_HOME
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME}"
export _JAVA_OPTIONS="-Djdk.net.unixdomain.tmpdir=C:\\tmp"
export GRADLE_OPTS="-Djdk.net.unixdomain.tmpdir=C:\\tmp"
export NODE_ENV="production"

mkdir -p /c/tmp
mkdir -p "$ARTIFACT_DIR"

echo "[INFO] Running local Expo prebuild for Android..."
cd "$MOBILE_DIR"
npx expo prebuild --platform android --clean

echo "[INFO] Applying stable Gradle settings..."
python - "$ANDROID_DIR/gradle.properties" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
content = path.read_text(encoding="utf-8")
content = re.sub(
    r"(?m)^org\.gradle\.jvmargs=.*$",
    "org.gradle.jvmargs=-Xmx4g -Djdk.net.unixdomain.tmpdir=C:/tmp",
    content,
)
line = "android.packagingOptions.pickFirsts=**/libreactnative.so"
if line not in content.splitlines():
    if not content.endswith("\n"):
        content += "\n"
    content += line + "\n"
path.write_text(content, encoding="utf-8")
PY

cd "$ANDROID_DIR"
./gradlew assembleRelease --no-daemon -PreactNativeArchitectures=arm64-v8a,x86_64
cp app/build/outputs/apk/release/app-release.apk "$APK_PATH"

cd "$PROJECT_DIR"
DATE=$(date +%Y-%m-%d)

if ! gh auth status -h github.com >/dev/null 2>&1; then
  echo "[ERROR] GitHub CLI authentication is required for release upload and latest.json update."
  echo "[ERROR] This is separate from git push over SSH. Run: gh auth login"
  exit 1
fi

if gh release view "v${VERSION}" --repo "$RELEASE_REPO" > /dev/null 2>&1; then
  gh release upload "v${VERSION}" "$APK_PATH" --clobber --repo "$RELEASE_REPO"
else
  gh release create "v${VERSION}" "$APK_PATH" --repo "$RELEASE_REPO" --title "v${VERSION}" --notes "v${VERSION} リリース"
fi

JSON_CONTENT=$(cat <<JSONEOF
{
  "mobile": {
    "version": "${VERSION}",
    "url": "https://github.com/${RELEASE_REPO}/releases/download/v${VERSION}/${APK_NAME}",
    "notes": "v${VERSION} リリース",
    "date": "${DATE}"
  }
}
JSONEOF
)

FILE_SHA=$(gh api "repos/${RELEASE_REPO}/contents/latest.json" --jq '.sha' 2>/dev/null || true)
B64=$(echo -n "$JSON_CONTENT" | base64 -w 0)

if [ -z "$FILE_SHA" ]; then
  gh api "repos/${RELEASE_REPO}/contents/latest.json" \
    --method PUT \
    --field message="latest.json を v${VERSION} に更新" \
    --field "content=${B64}"
else
  gh api "repos/${RELEASE_REPO}/contents/latest.json" \
    --method PUT \
    --field message="latest.json を v${VERSION} に更新" \
    --field "content=${B64}" \
    --field "sha=${FILE_SHA}"
fi

echo "Built $APK_PATH and published v${VERSION}"
