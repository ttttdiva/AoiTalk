@echo off
setlocal

set PROJECT_DIR=%~dp0..
set MOBILE_DIR=%PROJECT_DIR%\mobile
set ANDROID_DIR=%MOBILE_DIR%\android
set APK_NAME=aoitalk-mobile.apk
set RELEASE_REPO=ttttdiva/AoiTalk

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
set RELEASE_EXISTS=0

for /f "delims=" %%V in ('node -e "console.log(require('./mobile/app.json').expo.version)"') do set VERSION=%%V
for /f "delims=" %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%D

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
echo [INFO] Running local Expo prebuild for Android...
pushd "%MOBILE_DIR%"
call npx expo prebuild --platform android --clean
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%
popd

echo [INFO] Applying stable Gradle settings...
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
call gradlew.bat assembleRelease --no-daemon -PreactNativeArchitectures=arm64-v8a,x86_64
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%
copy /Y "app\build\outputs\apk\release\app-release.apk" "%PROJECT_DIR%\%APK_NAME%" >nul
popd

if "%RELEASE_EXISTS%"=="1" (
  gh release upload "v%VERSION%" "%APK_NAME%" --clobber --repo %RELEASE_REPO%
) else (
  gh release create "v%VERSION%" "%APK_NAME%" --repo %RELEASE_REPO% --title "v%VERSION%" --notes "v%VERSION% リリース"
)
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

for /f "delims=" %%S in ('gh api repos/%RELEASE_REPO%/contents/latest.json --jq ".sha" 2^>nul') do set FILE_SHA=%%S
for /f "delims=" %%B in ('powershell -NoProfile -Command "$json = @{mobile=@{version='%VERSION%';url='https://github.com/%RELEASE_REPO%/releases/download/v%VERSION%/%APK_NAME%';notes='v%VERSION% リリース';date='%TODAY%'}} | ConvertTo-Json -Depth 3; [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($json))"') do set B64=%%B

if "%FILE_SHA%"=="" (
  gh api repos/%RELEASE_REPO%/contents/latest.json --method PUT --field message="latest.json を v%VERSION% に更新" --field "content=%B64%"
) else (
  gh api repos/%RELEASE_REPO%/contents/latest.json --method PUT --field message="latest.json を v%VERSION% に更新" --field "content=%B64%" --field "sha=%FILE_SHA%"
)
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

echo Built local %APK_NAME% and published v%VERSION%
