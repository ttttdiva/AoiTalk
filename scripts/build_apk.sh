#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MOBILE_DIR="$PROJECT_DIR/mobile"
ANDROID_DIR="$MOBILE_DIR/android"
APK_NAME="aoitalk-mobile.apk"
RELEASE_REPO="ttttdiva/AoiTalk"
if [[ -n "${RELEASE_TARGET:-}" && "${RELEASE_TARGET}" != "main" ]]; then
  echo "[ERROR] RELEASE_TARGET must remain main for the fixed origin/main release."
  exit 1
fi
RELEASE_TARGET="main"
PUBLISH_ONLY=0
if [[ "${1:-}" == "--publish-only" || "${1:-}" == "-PublishOnly" ]]; then
  PUBLISH_ONLY=1
  shift
fi
if [[ -n "${EXPO_PUBLIC_AOITALK_READ_ONLY_DIAGNOSTICS:-}" && "${AOITALK_MOBILE_QA_BUILD:-}" != "1" ]]; then
  echo "[ERROR] EXPO_PUBLIC_AOITALK_READ_ONLY_DIAGNOSTICS is a QA-only read-only diagnostics variant and must be unset for release builds."
  exit 1
fi
if [[ -n "${AOITALK_MOBILE_QA_BUILD:-}" ]]; then
  if [[ "$AOITALK_MOBILE_QA_BUILD" != "1" || "${AOITALK_BUILD_ONLY:-}" != "1" || "$PUBLISH_ONLY" == "1" ]]; then
    echo "[ERROR] QA branch-build requires explicit build-only mode and cannot publish."
    exit 1
  fi
fi
PYTHON_BIN="${PYTHON_BIN:-python}"
POWERSHELL_BIN="${POWERSHELL_BIN:-powershell.exe}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[ERROR] Python is required for the immutable Mobile artifact manifest."
  exit 1
fi

"$PYTHON_BIN" "$PROJECT_DIR/scripts/mobile_release_manifest.py" preflight \
  --project-root "$PROJECT_DIR" --apk-path "$PROJECT_DIR/artifacts/releases/mobile/__preflight__.apk"
VERSION=$(cd "$PROJECT_DIR" && node -e "console.log(require('./mobile/app.json').expo.version)")
VERSION_CODE=$(cd "$PROJECT_DIR" && node -e "console.log(require('./mobile/app.json').expo.android.versionCode)")
if [ -z "$VERSION" ]; then
  echo "[ERROR] Failed to read mobile/app.json expo.version"
  exit 1
fi
ARTIFACT_DIR="$PROJECT_DIR/artifacts/releases/mobile/v${VERSION}"
APK_PATH="$ARTIFACT_DIR/$APK_NAME"
MANIFEST_PATH="$APK_PATH.manifest.json"
QA_MARKER_PATH="$APK_PATH.qa-approved.json"

if (( PUBLISH_ONLY )); then
  if [[ "${AOITALK_BUILD_ONLY:-}" == "1" ]]; then
    echo "[ERROR] --publish-only cannot be combined with AOITALK_BUILD_ONLY=1."
    exit 1
  fi
  if [[ ! -f "$APK_PATH" ]]; then
    echo "[ERROR] Existing APK is missing: $APK_PATH"
    exit 1
  fi
  "$PYTHON_BIN" "$PROJECT_DIR/scripts/mobile_release_manifest.py" validate \
    --project-root "$PROJECT_DIR" --apk-path "$APK_PATH"
  if ! gh auth status -h github.com >/dev/null 2>&1; then
    echo "[ERROR] GitHub CLI authentication is required for release upload and latest.json update."
    exit 1
  fi
else
  mkdir -p "$ARTIFACT_DIR"
  "$PYTHON_BIN" "$PROJECT_DIR/scripts/mobile_release_manifest.py" preflight \
    --project-root "$PROJECT_DIR" --apk-path "$APK_PATH"
  "$PYTHON_BIN" "$PROJECT_DIR/scripts/mobile_release_manifest.py" invalidate \
    --project-root "$PROJECT_DIR" --apk-path "$APK_PATH"
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
  BUILD_TMP_ROOT="$PROJECT_DIR/.local/mobile-build-tmp-$$"
  export BUILD_TMP_ROOT
  export _JAVA_OPTIONS="-Djdk.net.unixdomain.tmpdir=${BUILD_TMP_ROOT}"
  export GRADLE_OPTS="-Djdk.net.unixdomain.tmpdir=${BUILD_TMP_ROOT}"
  export NODE_ENV="production"

  if [ -e "$BUILD_TMP_ROOT" ]; then
    echo "[ERROR] Owned Mobile build temp path already exists: $BUILD_TMP_ROOT"
    exit 1
  fi
  mkdir -p "$BUILD_TMP_ROOT"
  cleanup_owned_build_temp() {
    case "$BUILD_TMP_ROOT" in
      "$PROJECT_DIR/.local/mobile-build-tmp-$$")
        if [ -d "$BUILD_TMP_ROOT" ] && [ ! -L "$BUILD_TMP_ROOT" ]; then
          rm -rf -- "$BUILD_TMP_ROOT"
        fi
        ;;
      *)
        echo "[ERROR] Refusing cleanup outside the owned Mobile build temp path: $BUILD_TMP_ROOT" >&2
        return 1
        ;;
    esac
  }
  trap cleanup_owned_build_temp EXIT
  echo "[INFO] Running local Expo prebuild for Android..."
  cd "$MOBILE_DIR"
  npx expo prebuild --platform android --clean

  echo "[INFO] Applying stable Gradle settings..."
python - "$ANDROID_DIR/gradle.properties" <<'PY'
import os
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
content = path.read_text(encoding="utf-8")
content = re.sub(
    r"(?m)^org\.gradle\.jvmargs=.*$",
    "org.gradle.jvmargs=-Xmx4g -Djdk.net.unixdomain.tmpdir=" + pathlib.Path(os.environ["BUILD_TMP_ROOT"]).as_posix(),
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
  "$POWERSHELL_BIN" -NoProfile -ExecutionPolicy Bypass -File "$PROJECT_DIR/scripts/verify_mobile_apk.ps1" \
    -ApkPath "$APK_PATH" -ExpectedVersion "$VERSION" -ExpectedVersionCode "$VERSION_CODE"
  "$PYTHON_BIN" "$PROJECT_DIR/scripts/mobile_release_manifest.py" write \
    --project-root "$PROJECT_DIR" --apk-path "$APK_PATH"
  if [[ "${AOITALK_BUILD_ONLY:-}" == "1" ]]; then
    echo "Built and verified local APK: $APK_PATH"
    exit 0
  fi
fi
DATE=$(date +%Y-%m-%d)
if ! gh auth status -h github.com >/dev/null 2>&1; then
  echo "[ERROR] GitHub CLI authentication is required for release upload and latest.json update."
  echo "[ERROR] This is separate from git push over SSH. Run: gh auth login"
  exit 1
fi
if gh release view "v${VERSION}" --repo "$RELEASE_REPO" > /dev/null 2>&1; then
  if [[ "${ALLOW_SAME_VERSION_RELEASE:-}" != "1" ]]; then
    echo "[ERROR] Release v${VERSION} already exists; explicit same-version overwrite is required."
    exit 1
  fi
  gh release upload "v${VERSION}" "$APK_PATH" --clobber --repo "$RELEASE_REPO"
else
  gh release create "v${VERSION}" "$APK_PATH" --repo "$RELEASE_REPO" --target "$RELEASE_TARGET" --title "v${VERSION}" --notes "v${VERSION} リリース"
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
