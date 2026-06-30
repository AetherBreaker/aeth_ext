$target = Join-Path $env:USERPROFILE ".vscode\extensions\local.drekker-add-to-runtime-base-0.0.1"
$source = Join-Path $PSScriptRoot ""

if (Test-Path $target) {
    Remove-Item $target -Recurse -Force
}

New-Item -ItemType Junction -Path $target -Target $source | Out-Null
Write-Host "Junction created at: $target"
Write-Host "Source: $source"
Write-Host ""
Write-Host "Reload VS Code now: Ctrl+Shift+P -> Developer: Reload Window"