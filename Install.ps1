$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$source = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.py' -File)
if ($source.Count -ne 1) {
    throw 'Keep Install.ps1 in the same folder as the Resolve Python script.'
}

$bin = Join-Path $env:APPDATA 'ResolveNetEaseMusic\bin'
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

$menu = Join-Path $env:APPDATA 'Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility'
New-Item -ItemType Directory -Path $menu -Force | Out-Null
Copy-Item -LiteralPath $source[0].FullName -Destination $menu -Force
Write-Host 'Installed. Restart Resolve if the menu entry is not visible.'
