@echo off
setlocal
set "BUILD_RESULT=1"
set "BUILD_ROOT_READY=0"
set "CLEANUP_FAILED=0"

for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
set MOBILE_DIR=%PROJECT_DIR%\mobile
if /I "%~1"=="__AOITALK_OWNED_BUILD" goto owned_build
for /f "delims=" %%G in ('powershell -NoProfile -Command "[guid]::NewGuid().ToString('N').Substring(0, 8)"') do set "BUILD_ID=%%G"
if not defined BUILD_ID (
  echo [ERROR] Failed to allocate a unique build workspace id.
  set "BUILD_RESULT=1"
  goto cleanup
)
set "ANDROID_DIR="

echo [INFO] Resolving a safe TEMP-derived Android build workspace...
set "AOITALK_TEMP_BASE="
for /f "delims=" %%T in ('powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $p=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()); $root=[IO.Path]::GetPathRoot($p); if ([string]::IsNullOrWhiteSpace($p) -or [string]::IsNullOrWhiteSpace($root) -or $p.TrimEnd('\') -ieq $root.TrimEnd('\')) { throw 'Unsafe TEMP root' }; if (-not (Test-Path -LiteralPath $p -PathType Container)) { throw 'TEMP directory missing' }; $cursor=Get-Item -LiteralPath $p -Force; while ($null -ne $cursor) { if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'TEMP path or parent is a reparse point: ' + $cursor.FullName }; $cursor=$cursor.Parent }; $probe=Join-Path $p ('.aoitalk-build-probe-' + [guid]::NewGuid().ToString('N')); [IO.Directory]::CreateDirectory($probe) | Out-Null; [IO.Directory]::Delete($probe); $p"') do set "AOITALK_TEMP_BASE=%%T"
if not defined AOITALK_TEMP_BASE (
  echo [ERROR] Could not resolve a safe TEMP-derived build root; refusing C-drive fallback.
  set "BUILD_RESULT=1"
  goto cleanup
)
set "AOITALK_BUILD_TMP_ROOT=%AOITALK_TEMP_BASE%\aoi-build-%BUILD_ID%"
set "BUILD_INVOCATION_ROOT=%AOITALK_BUILD_TMP_ROOT%"
set "SHORT_BUILD_ROOT=%AOITALK_TEMP_BASE%\a-%BUILD_ID%"
set "KC_STAGE=%AOITALK_TEMP_BASE%\k-%BUILD_ID%"
set "JDK_TMP=%AOITALK_TEMP_BASE%\j-%BUILD_ID%"
set "ANDROID_DIR=%SHORT_BUILD_ROOT%\android"
set "_JAVA_OPTIONS=-Djdk.net.unixdomain.tmpdir=%JDK_TMP%"
set "GRADLE_OPTS=-Djdk.net.unixdomain.tmpdir=%JDK_TMP%"
set "AOITALK_BUILD_RESERVATION_FILE=%AOITALK_TEMP_BASE%\.aoitalk-build-%BUILD_ID%.lock"
set "AOITALK_BUILD_SCRIPT=%~f0"
powershell -NoProfile -Command ^
  "$ErrorActionPreference='Stop'; $base=[IO.Path]::GetFullPath($env:AOITALK_TEMP_BASE).TrimEnd('\'); $id=$env:BUILD_ID; $root=[IO.Path]::GetFullPath($env:AOITALK_BUILD_TMP_ROOT).TrimEnd('\'); $short=[IO.Path]::GetFullPath($env:SHORT_BUILD_ROOT).TrimEnd('\'); $stage=[IO.Path]::GetFullPath($env:KC_STAGE).TrimEnd('\'); $jdk=[IO.Path]::GetFullPath($env:JDK_TMP).TrimEnd('\'); $reservation=[IO.Path]::GetFullPath($env:AOITALK_BUILD_RESERVATION_FILE); $script=[IO.Path]::GetFullPath($env:AOITALK_BUILD_SCRIPT);" ^
  "$expectedRoot=[IO.Path]::GetFullPath((Join-Path $base ('aoi-build-'+$id))).TrimEnd('\'); $expectedShort=[IO.Path]::GetFullPath((Join-Path $base ('a-'+$id))).TrimEnd('\'); $expectedStage=[IO.Path]::GetFullPath((Join-Path $base ('k-'+$id))).TrimEnd('\'); $expectedJdk=[IO.Path]::GetFullPath((Join-Path $base ('j-'+$id))).TrimEnd('\'); $expectedReservation=[IO.Path]::GetFullPath((Join-Path $base ('.aoitalk-build-'+$id+'.lock')));" ^
  "foreach ($pair in @(@($root,$expectedRoot),@($short,$expectedShort),@($stage,$expectedStage),@($jdk,$expectedJdk),@($reservation,$expectedReservation))) { if (-not $pair[0].Equals($pair[1],[StringComparison]::OrdinalIgnoreCase)) { throw ('Unexpected owned build path: '+$pair[0]) } }; $profile=[IO.Path]::GetFullPath([Environment]::GetFolderPath('UserProfile')).TrimEnd('\'); $repo=[IO.Path]::GetFullPath($env:PROJECT_DIR).TrimEnd('\'); $drive=[IO.Path]::GetPathRoot($base).TrimEnd('\'); foreach ($path in @($root,$short,$stage,$jdk,$reservation)) { if (-not $path.StartsWith($base+'\',[StringComparison]::OrdinalIgnoreCase) -or $path -ieq $base -or $path -ieq $profile -or $path -ieq $repo -or $path -ieq $drive -or $path.IndexOfAny([char[]]'*?[]') -ge 0) { throw ('Unsafe owned build path: '+$path) } };" ^
  "$lock=$null; $reservationOwned=$false; $created=[Collections.Generic.List[string]]::new(); $result=1; $cleanupErrors=[Collections.Generic.List[string]]::new(); try { $lock=[IO.File]::Open($reservation,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None); $reservationOwned=$true; foreach ($path in @($root,$short,$stage,$jdk)) { if (Test-Path -LiteralPath $path) { throw ('Build path collision: '+$path) } }; foreach ($path in @($root,$short,$stage,$jdk)) { New-Item -ItemType Directory -Path $path -ErrorAction Stop | Out-Null; $created.Add($path); $item=Get-Item -LiteralPath $path -Force; if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw ('Owned build path became a reparse point: '+$path) } }; [ordered]@{build_id=$id; creator='AoiTalk-build_apk'; created_at=[DateTimeOffset]::UtcNow.ToString('o'); parent_pid=$PID; repo_path=$repo; temp_base=$base; short_build_root=$short; kc_stage=$stage; jdk_tmp=$jdk; cleanup_policy='exact-owned-build-only'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root '.aoitalk-build-owner.json') -Encoding UTF8 -ErrorAction Stop;" ^
  "$command='call '+[char]34+$script+[char]34+' __AOITALK_OWNED_BUILD'; & $env:ComSpec /d /c $command; $result=$LASTEXITCODE } catch { [Console]::Error.WriteLine('[ERROR] Owned build setup/supervisor failed: '+$_.Exception.Message); $result=1 } finally { foreach ($path in @($short,$stage,$jdk,$root)) { if ($created.Contains($path) -and (Test-Path -LiteralPath $path)) { try { $item=Get-Item -LiteralPath $path -Force; if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw ('Refusing reparse cleanup target: '+$path) }; Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop } catch { $cleanupErrors.Add($path+': '+$_.Exception.Message) } } }; if ($null -ne $lock) { try { $lock.Dispose() } catch { $cleanupErrors.Add($reservation+': '+$_.Exception.Message) }; $lock=$null }; if ($reservationOwned -and (Test-Path -LiteralPath $reservation)) { try { Remove-Item -LiteralPath $reservation -Force -ErrorAction Stop } catch { $cleanupErrors.Add($reservation+': '+$_.Exception.Message) } }; if ($cleanupErrors.Count -gt 0) { [Console]::Error.WriteLine('[ERROR] Owned build supervisor cleanup failed; original build result is preserved: '+($cleanupErrors -join '; ')) } }; exit $result"
set "BUILD_RESULT=%ERRORLEVEL%"
goto cleanup

:owned_build
set "BUILD_ROOT_READY=1"
echo [INFO] Preparing a short-path Android build workspace under %AOITALK_TEMP_BASE%...

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
  set "BUILD_RESULT=1"
  goto cleanup
)
if not defined ANDROID_HOME set "ANDROID_HOME=%LOCALAPPDATA%\Android\Sdk"
if not defined ANDROID_SDK_ROOT set "ANDROID_SDK_ROOT=%ANDROID_HOME%"
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
  if errorlevel 1 (
    set "BUILD_RESULT=1"
    goto cleanup
  )
)

rem Build-only mode produces a verified local APK without publishing an update.
if /I "%AOITALK_BUILD_ONLY%"=="1" goto prepare_android_build

gh auth status -h github.com >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo [ERROR] GitHub CLI authentication is required for release upload and latest.json update.
  echo [ERROR] This is separate from git push over SSH. Run: gh auth login
  set "BUILD_RESULT=1"
  goto cleanup
)

gh release view "v%VERSION%" --repo %RELEASE_REPO% >nul 2>&1
if %ERRORLEVEL% EQU 0 (
  set RELEASE_EXISTS=1
  if /I not "%ALLOW_SAME_VERSION_RELEASE%"=="1" (
    echo [ERROR] Release v%VERSION% already exists.
    echo [ERROR] Mobile auto-update compares semantic versions, so overwriting the same version will not trigger update detection.
    echo [ERROR] Bump mobile/app.json expo.version before publishing, or set ALLOW_SAME_VERSION_RELEASE=1 only if you intentionally want a same-version overwrite.
    set "BUILD_RESULT=1"
    goto cleanup
  )
)

:prepare_android_build
rem Never exclude a directory named "android": it would also remove native Expo modules.
rem Expo prebuild --clean replaces the copied top-level android directory in the next step.
robocopy "%MOBILE_DIR%" "%SHORT_BUILD_ROOT%" /E /XD node_modules .expo "%MOBILE_DIR%\modules\apk-installer\android\build" >nul
set "ROBO_RESULT=%ERRORLEVEL%"
if %ROBO_RESULT% GEQ 8 (
  echo [ERROR] Failed to copy the mobile workspace to %SHORT_BUILD_ROOT%.
  set "BUILD_RESULT=%ROBO_RESULT%"
  goto cleanup
)

if not exist "%SHORT_BUILD_ROOT%\modules\apk-installer\android\src\main\java\expo\modules\apkinstaller\ApkInstallerModule.kt" (
  echo [ERROR] The apk-installer Android source was omitted from the short-path workspace.
  set "BUILD_RESULT=1"
  goto cleanup
)

echo [INFO] Installing dependencies in the short-path workspace...
pushd "%SHORT_BUILD_ROOT%"
call npm ci
set INSTALL_RESULT=%ERRORLEVEL%
popd
if %INSTALL_RESULT% NEQ 0 ( set "BUILD_RESULT=%INSTALL_RESULT%" & goto cleanup )

echo [INFO] Running Expo prebuild for Android...
pushd "%SHORT_BUILD_ROOT%"
call npx expo prebuild --platform android --clean
set PREBUILD_RESULT=%ERRORLEVEL%
popd
if %PREBUILD_RESULT% NEQ 0 ( set "BUILD_RESULT=%PREBUILD_RESULT%" & goto cleanup )

echo [INFO] Applying stable Gradle settings...
powershell -NoProfile -Command ^
  "$stage = [IO.Path]::GetFullPath($env:KC_STAGE); $base = [IO.Path]::GetFullPath($env:AOITALK_TEMP_BASE).TrimEnd('\');" ^
  "if (-not $stage.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Stage escaped validated TEMP base' };" ^
  "if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }"
set "STEP_RESULT=%ERRORLEVEL%"
if not "%STEP_RESULT%"=="0" (
  set "BUILD_RESULT=%STEP_RESULT%"
  goto cleanup
)

powershell -NoProfile -Command ^
  "$path = '%SHORT_BUILD_ROOT%\node_modules\@react-native\gradle-plugin\react-native-gradle-plugin\src\main\kotlin\com\facebook\react\tasks\GenerateAutolinkingNewArchitecturesFileTask.kt';" ^
  "$content = Get-Content -LiteralPath $path -Raw;" ^
  "$content = $content.Replace('${libraryName}_autolinked_build', '${libraryName.take(12)}_autolinked_build');" ^
  "[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))"
set "STEP_RESULT=%ERRORLEVEL%"
if not "%STEP_RESULT%"=="0" ( set "BUILD_RESULT=%STEP_RESULT%" & goto cleanup )

powershell -NoProfile -Command ^
  "$path = '%SHORT_BUILD_ROOT%\node_modules\react-native-keyboard-controller\android\src\main\jni\CMakeLists.txt';" ^
  "$content = Get-Content -LiteralPath $path -Raw;" ^
  "$needle = 'file(GLOB LIB_CODEGEN_SRCS CONFIGURE_DEPENDS ${LIB_ANDROID_GENERATED_JNI_DIR}/*.cpp ${LIB_ANDROID_GENERATED_COMPONENTS_DIR}/*.cpp)';" ^
  "$stage = ([IO.Path]::GetFullPath($env:KC_STAGE)).Replace('\','/'); $custom = $stage + '/custom'; $codegen = $stage + '/codegen'; $q=[char]34; $nl=[Environment]::NewLine;" ^
  "$block = $needle + $nl + $nl + 'file(GLOB LIB_CUSTOM_HEADERS CONFIGURE_DEPENDS ${LIB_COMMON_COMPONENTS_DIR}/*.h)' + $nl + 'file(GLOB LIB_CODEGEN_HEADERS CONFIGURE_DEPENDS ${LIB_ANDROID_GENERATED_JNI_DIR}/*.h ${LIB_ANDROID_GENERATED_COMPONENTS_DIR}/*.h)' + $nl + ('file(MAKE_DIRECTORY '+$q+$custom+$q+' '+$q+$codegen+$q+')') + $nl + ('file(COPY ${LIB_CUSTOM_SRCS} ${LIB_CUSTOM_HEADERS} DESTINATION '+$q+$custom+$q+')') + $nl + ('file(COPY ${LIB_CODEGEN_SRCS} ${LIB_CODEGEN_HEADERS} DESTINATION '+$q+$codegen+$q+')') + $nl + ('file(GLOB LIB_CUSTOM_SRCS CONFIGURE_DEPENDS '+$q+$custom+'/*.cpp'+$q+')') + $nl + ('file(GLOB LIB_CODEGEN_SRCS CONFIGURE_DEPENDS '+$q+$codegen+'/*.cpp'+$q+')');" ^
  "if (-not $content.Contains($needle)) { throw 'Keyboard-controller CMake marker was not found' };" ^
  "$content = $content.Replace($needle, $block);" ^
  "[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))"
set "STEP_RESULT=%ERRORLEVEL%"
if not "%STEP_RESULT%"=="0" ( set "BUILD_RESULT=%STEP_RESULT%" & goto cleanup )

powershell -NoProfile -Command ^
  "$path = '%ANDROID_DIR%\gradle.properties';" ^
  "$content = Get-Content -Path $path -Raw;" ^
  "$jdkTmp = ([IO.Path]::GetFullPath($env:JDK_TMP)).Replace('\','/'); $nl=[Environment]::NewLine; $q=[char]34; $content = [regex]::Replace($content, '(?m)^org\.gradle\.jvmargs=.*$', ('org.gradle.jvmargs=-Xmx4g -Djdk.net.unixdomain.tmpdir='+$q+$jdkTmp+$q));" ^
  "if ($content -notmatch '(?m)^android\.packagingOptions\.pickFirsts=\*\*/libreactnative\.so$') { if ($content -notmatch '\r?\n$') { $content += $nl }; $content += 'android.packagingOptions.pickFirsts=**/libreactnative.so' + $nl };" ^
  "[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))"
set "STEP_RESULT=%ERRORLEVEL%"
if not "%STEP_RESULT%"=="0" ( set "BUILD_RESULT=%STEP_RESULT%" & goto cleanup )

powershell -NoProfile -Command ^
  "$path = '%ANDROID_DIR%\app\build.gradle';" ^
  "$content = Get-Content -Path $path -Raw;" ^
  "$content = [regex]::Replace($content, '(?m)^\s*cliFile = new File\(\[\"node\", \"--print\", \"require\.resolve\(''@expo/cli'', \{ paths: \[require\.resolve\(''expo/package\.json''\)\] \}\)\"\]\.execute\(null, rootDir\)\.text\.trim\(\)\)$', '    cliFile = new File(rootDir, \"../node_modules/expo/node_modules/@expo/cli/build/bin/cli\")');" ^
  "[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))"
set "STEP_RESULT=%ERRORLEVEL%"
if not "%STEP_RESULT%"=="0" ( set "BUILD_RESULT=%STEP_RESULT%" & goto cleanup )

pushd "%ANDROID_DIR%"
call .\gradlew.bat assembleRelease --no-daemon --max-workers=1 -Pkotlin.incremental=false -PreactNativeArchitectures=%REACT_NATIVE_ARCHITECTURES%
set "STEP_RESULT=%ERRORLEVEL%"
if not "%STEP_RESULT%"=="0" (
  set "BUILD_RESULT=%STEP_RESULT%"
  popd
  goto cleanup
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\verify_mobile_apk.ps1" -ApkPath "%ANDROID_DIR%\app\build\outputs\apk\release\app-release.apk" -ExpectedVersion "%VERSION%" -ExpectedVersionCode "%VERSION_CODE%"
set "STEP_RESULT=%ERRORLEVEL%"
if not "%STEP_RESULT%"=="0" (
  set "BUILD_RESULT=%STEP_RESULT%"
  popd
  goto cleanup
)
copy /Y "app\build\outputs\apk\release\app-release.apk" "%APK_PATH%" >nul
set COPY_RESULT=%ERRORLEVEL%
if not "%COPY_RESULT%"=="0" (
  echo [ERROR] Failed to copy the verified APK to %APK_PATH%.
  set "BUILD_RESULT=%COPY_RESULT%"
  popd
  goto cleanup
)
popd

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\verify_mobile_apk.ps1" -ApkPath "%APK_PATH%" -ExpectedVersion "%VERSION%" -ExpectedVersionCode "%VERSION_CODE%"
set "STEP_RESULT=%ERRORLEVEL%"
if not "%STEP_RESULT%"=="0" ( set "BUILD_RESULT=%STEP_RESULT%" & goto cleanup )

if /I "%AOITALK_BUILD_ONLY%"=="1" (
  echo Built and verified local APK: %APK_PATH%
  set "BUILD_RESULT=0"
  goto cleanup
)

if "%RELEASE_EXISTS%"=="1" (
  gh release upload "v%VERSION%" "%APK_PATH%" --clobber --repo %RELEASE_REPO%
) else (
  rem Decode the default Japanese release label inside PowerShell so cmd.exe never parses non-ASCII text.
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$suffix=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('44Oq44Oq44O844K5')); $notes='v'+$env:VERSION+' '+$suffix; & gh release create ('v'+$env:VERSION) $env:APK_PATH --repo $env:RELEASE_REPO --target $env:RELEASE_TARGET --title ('v'+$env:VERSION) --notes $notes; exit $LASTEXITCODE"
)
set "STEP_RESULT=%ERRORLEVEL%"
if not "%STEP_RESULT%"=="0" ( set "BUILD_RESULT=%STEP_RESULT%" & goto cleanup )

rem Keep non-ASCII text out of this batch file because cmd.exe parses batch text through its active code page.
rem publish_latest_json.ps1 is UTF-8 with BOM and owns the UTF-8 metadata update.
rem NOTES_FILE may point to a UTF-8 text file to override notes.
if defined NOTES_FILE (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\publish_latest_json.ps1" -Version "%VERSION%" -Repo "%RELEASE_REPO%" -ApkName "%APK_NAME%" -Today "%TODAY%" -NotesFile "%NOTES_FILE%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\publish_latest_json.ps1" -Version "%VERSION%" -Repo "%RELEASE_REPO%" -ApkName "%APK_NAME%" -Today "%TODAY%"
)
set "STEP_RESULT=%ERRORLEVEL%"
if not "%STEP_RESULT%"=="0" ( set "BUILD_RESULT=%STEP_RESULT%" & goto cleanup )

echo Built %APK_PATH% and published v%VERSION%
set "BUILD_RESULT=0"
goto cleanup

:cleanup
if not defined BUILD_RESULT set "BUILD_RESULT=1"
if "%BUILD_ROOT_READY%"=="1" (
  powershell -NoProfile -Command ^
    "$ErrorActionPreference='Stop'; $base=[IO.Path]::GetFullPath($env:AOITALK_TEMP_BASE).TrimEnd('\'); $root=[IO.Path]::GetFullPath($env:AOITALK_BUILD_TMP_ROOT).TrimEnd('\'); $expected=[IO.Path]::GetFullPath((Join-Path $base ('aoi-build-' + $env:BUILD_ID)));" ^
    "if ($root -cne $expected -or -not $root.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup root escaped validated TEMP base' };" ^
    "$marker=Join-Path $root '.aoitalk-build-owner.json'; if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { throw 'Build ownership marker missing' };" ^
    "$owner=Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json; if ($owner.creator -cne 'AoiTalk-build_apk' -or $owner.build_id -cne $env:BUILD_ID -or $owner.cleanup_policy -cne 'exact-owned-build-only') { throw 'Build ownership marker mismatch' };" ^
    "$profile=[IO.Path]::GetFullPath([Environment]::GetFolderPath('UserProfile')).TrimEnd('\'); $repo=[IO.Path]::GetFullPath($env:PROJECT_DIR).TrimEnd('\'); $drive=[IO.Path]::GetPathRoot($root).TrimEnd('\'); if ($root -ieq $drive -or $root -ieq $profile -or $root -ieq $repo) { throw 'Protected cleanup root' };" ^
    "$expectedShort=[IO.Path]::GetFullPath((Join-Path $base ('a-' + $env:BUILD_ID))).TrimEnd('\'); $expectedStage=[IO.Path]::GetFullPath((Join-Path $base ('k-' + $env:BUILD_ID))).TrimEnd('\'); $expectedJdk=[IO.Path]::GetFullPath((Join-Path $base ('j-' + $env:BUILD_ID))).TrimEnd('\');" ^
    "if (-not ([IO.Path]::GetFullPath($env:SHORT_BUILD_ROOT).TrimEnd('\')).Equals($expectedShort,[StringComparison]::OrdinalIgnoreCase) -or -not ([IO.Path]::GetFullPath($env:KC_STAGE).TrimEnd('\')).Equals($expectedStage,[StringComparison]::OrdinalIgnoreCase) -or -not ([IO.Path]::GetFullPath($env:JDK_TMP).TrimEnd('\')).Equals($expectedJdk,[StringComparison]::OrdinalIgnoreCase)) { throw 'Non-exact cleanup target' };" ^
    "if (-not ([IO.Path]::GetFullPath($owner.temp_base).TrimEnd('\')).Equals($base,[StringComparison]::OrdinalIgnoreCase) -or -not ([IO.Path]::GetFullPath($owner.repo_path).TrimEnd('\')).Equals($repo,[StringComparison]::OrdinalIgnoreCase) -or -not ([IO.Path]::GetFullPath($owner.short_build_root).TrimEnd('\')).Equals($expectedShort,[StringComparison]::OrdinalIgnoreCase) -or -not ([IO.Path]::GetFullPath($owner.kc_stage).TrimEnd('\')).Equals($expectedStage,[StringComparison]::OrdinalIgnoreCase) -or -not ([IO.Path]::GetFullPath($owner.jdk_tmp).TrimEnd('\')).Equals($expectedJdk,[StringComparison]::OrdinalIgnoreCase)) { throw 'Ownership manifest path mismatch' };" ^
    "$rootItem=Get-Item -LiteralPath $root -Force; if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Build root is a reparse point' };" ^
    "foreach ($target in @($env:SHORT_BUILD_ROOT,$env:KC_STAGE,$env:JDK_TMP)) { if ([string]::IsNullOrWhiteSpace($target)) { throw 'Empty cleanup target' }; $full=[IO.Path]::GetFullPath($target); if (-not $full.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup target escaped validated TEMP base' }; if ($full.IndexOfAny([char[]]'*?[]') -ge 0) { throw 'Wildcard cleanup target' }; if (Test-Path -LiteralPath $full) { $item=Get-Item -LiteralPath $full -Force; if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Cleanup target is a reparse point' }; Remove-Item -LiteralPath $full -Recurse -Force } };" ^
    "if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker -Force -ErrorAction Stop };" ^
    "if (Test-Path -LiteralPath $root) { $children=Get-ChildItem -LiteralPath $root -Force; if ($children.Count -eq 0) { Remove-Item -LiteralPath $root -Force } else { throw ('Owned build root not empty after exact cleanup: ' + $root) } }"
  if errorlevel 1 (
    echo [ERROR] Owned build scratch cleanup failed; original build result is preserved.
    set "CLEANUP_FAILED=1"
  )
)
if "%CLEANUP_FAILED%"=="1" echo [ERROR] Cleanup diagnostic: scratch may remain at %AOITALK_BUILD_TMP_ROOT%
exit /b %BUILD_RESULT%
