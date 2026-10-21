$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Installing build dependencies..."
python -m pip install --disable-pip-version-check -r requirements-build.txt

Write-Host "Building GUI executable..."
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name ArchiveDedup `
  --icon assets/app.ico `
  --add-data "assets;assets" `
  --version-file version_info.txt `
  archive_dedup.py

Write-Host "Building CLI executable..."
python -m PyInstaller --noconfirm --clean --onefile --console `
  --name ArchiveDedupCLI `
  archive_dedup_cli.py

Write-Host "Packing..."
$zip = Join-Path $PSScriptRoot "dist\ArchiveDedup-windows.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path "dist\ArchiveDedup.exe", "dist\ArchiveDedupCLI.exe" `
  -DestinationPath $zip -Force

Write-Host "Done: $zip"
