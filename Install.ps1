$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$settings = Join-Path $env:APPDATA 'ResolveNetEaseMusic'
$reportPath = Join-Path $settings 'install-report.txt'
$menu = Join-Path $env:APPDATA 'Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility'
$installedScript = $menu
$checkedFfmpeg = 'Not checked'
$ffmpegVersion = 'Not checked'

function Write-InstallReport($result) {
    $report = @(
        'Resolve NetEase Music 1.1.1',
        "Result: $result",
        "Script target: $installedScript",
        "FFmpeg path: $checkedFfmpeg",
        "FFmpeg check: $ffmpegVersion",
        'For Resolve and Python versions, open the panel and click Copy Diagnostics.'
    )
    $report | ForEach-Object { Write-Host $_ }
    try {
        New-Item -ItemType Directory -Path $settings -Force | Out-Null
        Set-Content -LiteralPath $reportPath -Value $report -Encoding UTF8
        Write-Host "Report: $reportPath"
    } catch {
        Write-Host "Could not save the report: $($_.Exception.Message)"
    }
}

trap {
    Write-InstallReport "Failed. $($_.Exception.Message)"
    exit 1
}

$source = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.py' -File)
if ($source.Count -ne 1) {
    throw 'Keep Install.ps1 in the same folder as the Resolve Python script.'
}
$installedScript = Join-Path $menu $source[0].Name

$bin = Join-Path $settings 'bin'
$ffmpeg = Join-Path $bin 'ffmpeg.exe'
if (!(Test-Path -LiteralPath $ffmpeg -PathType Leaf) -and
    !(Get-Command ffmpeg.exe -CommandType Application -ErrorAction SilentlyContinue)) {
    $url = 'https://github.com/GyanD/codexffmpeg/releases/download/8.1.1/ffmpeg-8.1.1-essentials_build.zip'
    $expectedHash = '6F58CE889F59C311410F7D2B18895B33C03456463486F3B1EBC93D97A0F54541'
    $id = [guid]::NewGuid().ToString('N')
    $localArchive = Join-Path $PSScriptRoot 'ffmpeg-8.1.1-essentials_build.zip'
    $downloadPath = Join-Path ([IO.Path]::GetTempPath()) "resolve-netease-ffmpeg-$id.zip"
    $stagedExe = Join-Path $bin "ffmpeg-$id.tmp"

    try {
        if (Test-Path -LiteralPath $localArchive -PathType Leaf) {
            Write-Host 'Using local FFmpeg archive. No download needed.'
            $archivePath = $localArchive
        } else {
            Write-Host 'Downloading FFmpeg 8.1.1 (about 100 MB)...'
            Invoke-WebRequest -Uri $url -UseBasicParsing -OutFile $downloadPath -TimeoutSec 600
            $archivePath = $downloadPath
        }
        $sha256 = [Security.Cryptography.SHA256]::Create()
        $stream = [IO.File]::OpenRead($archivePath)
        try {
            $actualHash = [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace('-', '')
        } finally {
            $stream.Dispose()
            $sha256.Dispose()
        }
        if ($actualHash -ne $expectedHash) {
            throw 'FFmpeg download did not match its expected SHA-256.'
        }

        New-Item -ItemType Directory -Path $bin -Force | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
        try {
            $entry = $archive.GetEntry('ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe')
            if ($null -eq $entry) {
                throw 'The verified FFmpeg archive is missing ffmpeg.exe.'
            }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $stagedExe)
        } finally {
            $archive.Dispose()
        }
        Move-Item -LiteralPath $stagedExe -Destination $ffmpeg -Force
    } catch {
        throw "FFmpeg setup failed. Put the official ffmpeg-8.1.1-essentials_build.zip next to Install.cmd, or put an existing ffmpeg.exe on PATH and try again. $($_.Exception.Message)"
    } finally {
        Remove-Item -LiteralPath $downloadPath, $stagedExe -ErrorAction SilentlyContinue
    }
}

if (Test-Path -LiteralPath $ffmpeg -PathType Leaf) {
    $checkedFfmpeg = $ffmpeg
} else {
    $checkedFfmpeg = (Get-Command ffmpeg.exe -CommandType Application).Source
}
$process = New-Object System.Diagnostics.Process
$ffmpegVersion = 'Failed'
try {
    $process.StartInfo.FileName = $checkedFfmpeg
    $process.StartInfo.Arguments = '-version'
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (!$process.WaitForExit(8000)) {
        $process.Kill()
        throw 'FFmpeg did not respond within 8 seconds.'
    }
    if ($process.ExitCode -ne 0) {
        $reason = ($stderr.GetAwaiter().GetResult() -split '\r?\n')[0]
        throw "FFmpeg could not run (exit $($process.ExitCode)). $reason"
    }
    $ffmpegVersion = ($stdout.GetAwaiter().GetResult() -split '\r?\n')[0]
    if (!$ffmpegVersion) { throw 'FFmpeg returned no version information.' }
} finally {
    $process.Dispose()
}

New-Item -ItemType Directory -Path $menu -Force | Out-Null
Copy-Item -LiteralPath $source[0].FullName -Destination $installedScript -Force
if (!(Test-Path -LiteralPath $installedScript -PathType Leaf)) {
    throw 'The script was not found in the target directory after copying.'
}
Write-InstallReport 'Installed'
Write-Host 'Installed. Restart Resolve if the menu entry is not visible.'
