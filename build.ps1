$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Installing build dependencies..."
python -m pip install --disable-pip-version-check -r requirements-build.txt

Write-Host "Building GUI executable (onedir)..."
python -m PyInstaller --noconfirm --clean --onedir --windowed `
  --name ArchiveDedup `
  --icon assets/app.ico `
  --add-data "assets;assets" `
  --version-file version_info.txt `
  archive_dedup.py

Write-Host "Building CLI executable (onedir)..."
python -m PyInstaller --noconfirm --clean --onedir --console `
  --name ArchiveDedupCLI `
  --icon assets/app.ico `
  archive_dedup_cli.py

Write-Host "Packing onedir release..."
$stage = Join-Path $PSScriptRoot "dist\package"
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item -LiteralPath "dist\ArchiveDedup" -Destination (Join-Path $stage "ArchiveDedup") -Recurse
Copy-Item -LiteralPath "dist\ArchiveDedupCLI" -Destination (Join-Path $stage "ArchiveDedupCLI") -Recurse
Copy-Item -LiteralPath "packaging\启动说明.txt" -Destination (Join-Path $stage "启动说明.txt")

$zip = Join-Path $PSScriptRoot "dist\ArchiveDedup-windows.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force

Write-Host "Done: $zip"
