param(
    [string]$OutputJar = (Join-Path $PSScriptRoot "short-idle-rebuilt.jar")
)

# Build a separate JAR for review; do not overwrite the verified installed one.
$ErrorActionPreference = "Stop"
$taskRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("pdd-short-build-" + [guid]::NewGuid().ToString("N"))
$classes = Join-Path $taskRoot "classes"
$dex = Join-Path $taskRoot "dex"
$javac = "C:\Program Files\Android\Android Studio\jbr\bin\javac.exe"
$jar = "C:\Program Files\Android\Android Studio\jbr\bin\jar.exe"
$android = Join-Path $env:LOCALAPPDATA "Android\Sdk\platforms\android-36\android.jar"
$d8 = Join-Path $env:LOCALAPPDATA "Android\Sdk\build-tools\35.0.0\d8.bat"
if ([System.IO.Path]::GetFullPath($OutputJar) -eq [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "short-idle-dump.jar"))) {
    throw "Build into a separate file; the installed JAR is checksum-pinned."
}
New-Item -ItemType Directory -Path $classes, $dex -Force | Out-Null
try {
    & $javac --release 8 -cp $android -d $classes (Join-Path $PSScriptRoot "ShortIdleDump.java")
    if ($LASTEXITCODE -ne 0) { throw "javac failed" }
    & $d8 --min-api 26 --lib $android --output $dex (Join-Path $classes "ShortIdleDump.class")
    if ($LASTEXITCODE -ne 0) { throw "d8 failed" }
    & $jar -cf $OutputJar -C $dex classes.dex
    if ($LASTEXITCODE -ne 0) { throw "jar failed" }
    Get-FileHash -Algorithm SHA256 -LiteralPath $OutputJar
    Write-Output "Verify changes and update the pinned checksum only after review and tests; JAR timestamps may vary."
} finally {
    # Delete only explicitly named temporary files and empty directories.
    foreach ($path in @((Join-Path $classes "ShortIdleDump.class"), (Join-Path $dex "classes.dex"))) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
    foreach ($dir in @($classes, $dex, $taskRoot)) {
        if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Force }
    }
}
