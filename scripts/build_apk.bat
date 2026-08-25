@echo off
setlocal

for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
set MOBILE_DIR=%PROJECT_DIR%\mobile
for /f "delims=" %%G in ('powershell -NoProfile -Command "[guid]::NewGuid().ToString('N').Substring(0, 8)"') do set "BUILD_ID=%%G"
if not defined BUILD_ID (
  echo [ERROR] Failed to allocate a unique build workspace id.
  exit /b 1
)
set SHORT_BUILD_ROOT=C:\tmp\a-%BUILD_ID%
set KC_STAGE=C:\tmp\k-%BUILD_ID%
set ANDROID_DIR=%SHORT_BUILD_ROOT%\android
set APK_NAME=aoitalk-mobile.apk
set RELEASE_REPO=ttttdiva/AoiTalk
rem The public release repository is separate from the source checkout.  Do
rem not let gh infer the local HEAD (which is not present in the public repo).
if not defined RELEASE_TARGET set "RELEASE_TARGET=main"

if not defined JAVA_HOME (
  for /f "delims=" %%J in ('powershell -NoProfile -Command "$root = Join-Path $env:ProgramFiles 'Microsoft'; if (Test-Path $root) { Get-ChildItem $root -Directory -Filter 'jdk-17*' | Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty FullName }"') do set "JAVA_HOME=%%J"
)
if not defined JAVA_HOME (
  echo [ERROR] JAVA_HOME is not set and a Microsoft JDK 17 installation was not found.
  exit /b 1
)
if not defined ANDROID_HOME set "ANDROID_HOME=%LOCALAPPDATA%\Android\Sdk"
if not defined ANDROID_SDK_ROOT set "ANDROID_SDK_ROOT=%ANDROID_HOME%"
set _JAVA_OPTIONS=-Djdk.net.unixdomain.tmpdir=C:\tmp
set GRADLE_OPTS=-Djdk.net.unixdomain.tmpdir=C:\tmp
set NODE_ENV=production
rem Native CMake builds for multiple ABIs can fail nondeterministically on Windows
rem (long generated paths and parallel prefab compilation).  arm64-v8a is the
rem supported distribution ABI; callers may opt into additional ABIs explicitly.
if not defined REACT_NATIVE_ARCHITECTURES set "REACT_NATIVE_ARCHITECTURES=arm64-v8a"
set RELEASE_EXISTS=0

for /f "delims=" %%V in ('node -e "console.log(require('./mobile/app.json').expo.version)"') do set VERSION=%%V
for /f "delims=" %%C in ('node -e "console.log(require('./mobile/app.json').expo.android.versionCode)"') do set VERSION_CODE=%%C
for /f "delims=" %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%D
set "ARTIFACT_DIR=%PROJECT_DIR%\artifacts\releases\mobile\v%VERSION%"
set "APK_PATH=%ARTIFACT_DIR%\%APK_NAME%"
if not exist "%ARTIFACT_DIR%" (
  mkdir "%ARTIFACT_DIR%"
  if errorlevel 1 exit /b 1
)

gh auth status -h github.com >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo [ERROR] GitHub CLI authentication is required for release upload and latest.json update.
  echo [ERROR] This is separate from git push over SSH. Run: gh auth login
  exit /b 1
)

gh release view "v%VERSION%" --repo %RELEASE_REPO% >nul 2>&1
if %ERRORLEVEL% EQU 0 (
  set RELEASE_EXISTS=1
  if /I not "%ALLOW_SAME_VERSION_RELEASE%"=="1" (
    echo [ERROR] Release v%VERSION% already exists.
    echo [ERROR] Mobile auto-update compares semantic versions, so overwriting the same version will not trigger update detection.
    echo [ERROR] Bump mobile/app.json expo.version before publishing, or set ALLOW_SAME_VERSION_RELEASE=1 only if you intentionally want a same-version overwrite.
    exit /b 1
  )
)

if not exist "C:\tmp" mkdir "C:\tmp"
echo [INFO] Preparing a short-path Android build workspace...
powershell -NoProfile -Command ^
  "$path = [System.IO.Path]::GetFullPath('%SHORT_BUILD_ROOT%');" ^
  "if ($path -notlike 'C:\tmp\a-*') { throw 'Unexpected short build path: ' + $path };" ^
  "if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force };" ^
  "New-Item -ItemType Directory -Path $path -Force | Out-Null"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

rem Never exclude a directory named "android": it would also remove native Expo modules.
rem Expo prebuild --clean replaces the copied top-level android directory in the next step.
robocopy "%MOBILE_DIR%" "%SHORT_BUILD_ROOT%" /E /XD node_modules .expo "%MOBILE_DIR%\modules\apk-installer\android\build" >nul
if %ERRORLEVEL% GEQ 8 (
  echo [ERROR] Failed to copy the mobile workspace to %SHORT_BUILD_ROOT%.
  exit /b %ERRORLEVEL%
)

if not exist "%SHORT_BUILD_ROOT%\modules\apk-installer\android\src\main\java\expo\modules\apkinstaller\ApkInstallerModule.kt" (
  echo [ERROR] The apk-installer Android source was omitted from the short-path workspace.
  exit /b 1
)

echo [INFO] Installing dependencies in the short-path workspace...
pushd "%SHORT_BUILD_ROOT%"
call npm ci
set INSTALL_RESULT=%ERRORLEVEL%
popd
if %INSTALL_RESULT% NEQ 0 exit /b %INSTALL_RESULT%

echo [INFO] Running Expo prebuild for Android...
pushd "%SHORT_BUILD_ROOT%"
call npx expo prebuild --platform android --clean
set PREBUILD_RESULT=%ERRORLEVEL%
popd
if %PREBUILD_RESULT% NEQ 0 exit /b %PREBUILD_RESULT%

echo [INFO] Applying stable Gradle settings...
powershell -NoProfile -Command ^
  "$stage = [System.IO.Path]::GetFullPath('%KC_STAGE%');" ^
  "if ($stage -notlike 'C:\tmp\k-*') { throw 'Unexpected keyboard-controller stage path: ' + $stage };" ^
  "if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

powershell -NoProfile -Command ^
  "$path = '%SHORT_BUILD_ROOT%\node_modules\@react-native\gradle-plugin\react-native-gradle-plugin\src\main\kotlin\com\facebook\react\tasks\GenerateAutolinkingNewArchitecturesFileTask.kt';" ^
  "$content = Get-Content -LiteralPath $path -Raw;" ^
  "$content = $content.Replace('${libraryName}_autolinked_build', '${libraryName.take(12)}_autolinked_build');" ^
  "[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

powershell -NoProfile -Command ^
  "$path = '%SHORT_BUILD_ROOT%\node_modules\react-native-keyboard-controller\android\src\main\jni\CMakeLists.txt';" ^
  "$content = Get-Content -LiteralPath $path -Raw;" ^
  "$needle = 'file(GLOB LIB_CODEGEN_SRCS CONFIGURE_DEPENDS ${LIB_ANDROID_GENERATED_JNI_DIR}/*.cpp ${LIB_ANDROID_GENERATED_COMPONENTS_DIR}/*.cpp)';" ^
  "$block = $needle + \"`r`n`r`n\" + 'file(GLOB LIB_CUSTOM_HEADERS CONFIGURE_DEPENDS ${LIB_COMMON_COMPONENTS_DIR}/*.h)' + \"`r`n\" + 'file(GLOB LIB_CODEGEN_HEADERS CONFIGURE_DEPENDS ${LIB_ANDROID_GENERATED_JNI_DIR}/*.h ${LIB_ANDROID_GENERATED_COMPONENTS_DIR}/*.h)' + \"`r`n\" + 'file(MAKE_DIRECTORY C:/tmp/k/custom C:/tmp/k/codegen)' + \"`r`n\" + 'file(COPY ${LIB_CUSTOM_SRCS} ${LIB_CUSTOM_HEADERS} DESTINATION C:/tmp/k/custom)' + \"`r`n\" + 'file(COPY ${LIB_CODEGEN_SRCS} ${LIB_CODEGEN_HEADERS} DESTINATION C:/tmp/k/codegen)' + \"`r`n\" + 'file(GLOB LIB_CUSTOM_SRCS CONFIGURE_DEPENDS C:/tmp/k/custom/*.cpp)' + \"`r`n\" + 'file(GLOB LIB_CODEGEN_SRCS CONFIGURE_DEPENDS C:/tmp/k/codegen/*.cpp)';" ^
  "if (-not $content.Contains($needle)) { throw 'Keyboard-controller CMake marker was not found' };" ^
  "$content = $content.Replace($needle, $block);" ^
  "[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

powershell -NoProfile -Command ^
  "$path = '%ANDROID_DIR%\gradle.properties';" ^
  "$content = Get-Content -Path $path -Raw;" ^
  "$content = [regex]::Replace($content, '(?m)^org\.gradle\.jvmargs=.*$', 'org.gradle.jvmargs=-Xmx4g -Djdk.net.unixdomain.tmpdir=C:/tmp');" ^
  "if ($content -notmatch '(?m)^android\.packagingOptions\.pickFirsts=\*\*/libreactnative\.so$') { if ($content -notmatch '\r?\n$') { $content += \"`r`n\" }; $content += 'android.packagingOptions.pickFirsts=**/libreactnative.so' + \"`r`n\" };" ^
  "[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

powershell -NoProfile -Command ^
  "$path = '%ANDROID_DIR%\app\build.gradle';" ^
  "$content = Get-Content -Path $path -Raw;" ^
  "$content = [regex]::Replace($content, '(?m)^\s*cliFile = new File\(\[\"node\", \"--print\", \"require\.resolve\(''@expo/cli'', \{ paths: \[require\.resolve\(''expo/package\.json''\)\] \}\)\"\]\.execute\(null, rootDir\)\.text\.trim\(\)\)$', '    cliFile = new File(rootDir, \"../node_modules/expo/node_modules/@expo/cli/build/bin/cli\")');" ^
  "[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

pushd "%ANDROID_DIR%"
call .\gradlew.bat --stop
call .\gradlew.bat assembleRelease --no-daemon --max-workers=1 -Pkotlin.incremental=false -PreactNativeArchitectures=%REACT_NATIVE_ARCHITECTURES%
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\verify_mobile_apk.ps1" -ApkPath "%ANDROID_DIR%\app\build\outputs\apk\release\app-release.apk" -ExpectedVersion "%VERSION%" -ExpectedVersionCode "%VERSION_CODE%"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%
copy /Y "app\build\outputs\apk\release\app-release.apk" "%APK_PATH%" >nul
set COPY_RESULT=%ERRORLEVEL%
if not "%COPY_RESULT%"=="0" (
  echo [ERROR] Failed to copy the verified APK to %APK_PATH%.
  exit /b %COPY_RESULT%
)
popd

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\verify_mobile_apk.ps1" -ApkPath "%APK_PATH%" -ExpectedVersion "%VERSION%" -ExpectedVersionCode "%VERSION_CODE%"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

powershell -NoProfile -Command ^
  "$stage = [System.IO.Path]::GetFullPath('%KC_STAGE%');" ^
  "if ($stage -notlike 'C:\tmp\k-*') { throw 'Unexpected keyboard-controller stage path: ' + $stage };" ^
  "if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }; " ^
  "$path = [System.IO.Path]::GetFullPath('%SHORT_BUILD_ROOT%');" ^
  "if ($path -notlike 'C:\tmp\a-*') { throw 'Unexpected short build path: ' + $path };" ^
  "if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

if "%RELEASE_EXISTS%"=="1" (
  gh release upload "v%VERSION%" "%APK_PATH%" --clobber --repo %RELEASE_REPO%
  if errorlevel 1 exit /b 1
) else (
  gh release create "v%VERSION%" "%APK_PATH%" --repo %RELEASE_REPO% --target "%RELEASE_TARGET%" --title "v%VERSION%" --notes "v%VERSION% リリース"
  if errorlevel 1 exit /b 1
)

rem 日本語 notes を cmd 経由で渡すと codepage 不一致で文字化けするため、
rem latest.json の生成と更新は UTF-8 (BOM付き) の PowerShell script に委譲する。
rem NOTES_FILE 環境変数に UTF-8 テキストファイルを指定すると notes を差し替えられる。
if defined NOTES_FILE (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\publish_latest_json.ps1" -Version "%VERSION%" -Repo "%RELEASE_REPO%" -ApkName "%APK_NAME%" -Today "%TODAY%" -NotesFile "%NOTES_FILE%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\publish_latest_json.ps1" -Version "%VERSION%" -Repo "%RELEASE_REPO%" -ApkName "%APK_NAME%" -Today "%TODAY%"
)
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

echo Built %APK_PATH% and published v%VERSION%
