$ErrorActionPreference = 'Stop'
$moocRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$moocTestRoot = [IO.Path]::GetFullPath((Join-Path $moocRoot '_test_tmp'))
$moocStage = [IO.Path]::GetFullPath((Join-Path $moocTestRoot 'installed-v2'))
if (-not $moocStage.StartsWith($moocTestRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Unexpected installer test target'
}
$moocRegistry = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{A72F26C5-2A3F-47F9-96B8-B282D2C44F75}_is1'
if (Test-Path -LiteralPath $moocRegistry) {
    $moocExisting = Get-ItemProperty -LiteralPath $moocRegistry
    if ($moocExisting.InstallLocation.TrimEnd('\') -ne $moocStage) {
        throw 'A real installation already exists; test will not replace it'
    }
}
$moocInstaller = Join-Path $moocRoot 'release\MOOCDownloader-Setup-2.0.2-x64.exe'
$moocArguments = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS','/TASKS=',('/DIR=' + $moocStage),('/LOG=' + (Join-Path $moocTestRoot 'installer-test.log')))
$moocInstall = Start-Process -FilePath $moocInstaller -ArgumentList $moocArguments -WindowStyle Hidden -PassThru -Wait
if ($moocInstall.ExitCode -ne 0) { throw "Install failed: $($moocInstall.ExitCode)" }
$moocProgram = Join-Path $moocStage 'MOOCDownloader.exe'
$moocHash = (Get-FileHash -LiteralPath $moocProgram -Algorithm SHA256).Hash
$moocExpectedHash = (Get-FileHash -LiteralPath (Join-Path $moocRoot 'dist\MOOCDownloader\MOOCDownloader.exe') -Algorithm SHA256).Hash
if ($moocHash -ne $moocExpectedHash) { throw 'Installed executable differs from packaged executable' }
$env:MOOC_DATA_DIR = Join-Path $moocTestRoot 'installed-user-data'
New-Item -ItemType Directory -Force -Path $env:MOOC_DATA_DIR | Out-Null
Set-Content -LiteralPath (Join-Path $env:MOOC_DATA_DIR 'keep.txt') -Value 'preserve user data'
$moocResult = Join-Path $moocTestRoot 'installed-self-test.json'
$moocRun = Start-Process -FilePath $moocProgram -ArgumentList '--self-test',$moocResult -WindowStyle Hidden -PassThru -Wait
if ($moocRun.ExitCode -ne 0) { throw 'Installed program failed to start' }
$moocCheck = Get-Content -LiteralPath $moocResult -Raw | ConvertFrom-Json
if (-not $moocCheck.ffmpeg -or $moocCheck.version -ne '2.0.2') { throw 'Installed program components are incomplete' }
$moocLicensePaths = @('_internal\LICENSE','_internal\THIRD_PARTY_NOTICES.txt','_internal\licenses\FFmpeg-GPL-3.0.txt','_internal\licenses\Python-LICENSE.txt','_internal\licenses\Tcl-license.terms')
foreach ($moocLicenseRelative in $moocLicensePaths) {
    $moocLicensePath = Join-Path $moocStage $moocLicenseRelative
    if (-not (Test-Path -LiteralPath $moocLicensePath) -or (Get-Item -LiteralPath $moocLicensePath).Length -eq 0) { throw ('Missing installed license: ' + $moocLicenseRelative) }
}
$moocUninstaller = [IO.Path]::GetFullPath((Join-Path $moocStage 'unins000.exe'))
if (-not $moocUninstaller.StartsWith($moocStage + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Uninstall target mismatch' }
$moocUninstall = Start-Process -FilePath $moocUninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/LOG=' + (Join-Path $moocTestRoot 'uninstaller-test.log')) -WindowStyle Hidden -PassThru -Wait
if ($moocUninstall.ExitCode -ne 0) { throw 'Test uninstall failed' }
$moocEvidence = [ordered]@{
    installer_exit = $moocInstall.ExitCode
    installed_start_exit = $moocRun.ExitCode
    installed_version = $moocCheck.version
    ffmpeg_present = $moocCheck.ffmpeg
    binary_hash_matches = ($moocHash -eq $moocExpectedHash)
    license_files_present = $true
    uninstall_exit = $moocUninstall.ExitCode
    program_removed = (-not (Test-Path -LiteralPath $moocProgram))
    user_data_preserved = (Test-Path -LiteralPath (Join-Path $env:MOOC_DATA_DIR 'keep.txt'))
}
$moocEvidence | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $moocTestRoot 'installer-verification.json') -Encoding utf8
$moocEvidence | ConvertTo-Json
if (-not $moocEvidence.program_removed -or -not $moocEvidence.user_data_preserved) { throw 'Uninstall verification failed' }
