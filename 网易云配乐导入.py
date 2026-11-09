# -*- coding: utf-8 -*-
import base64
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import traceback
import wave
import winreg
import zlib
from array import array
from concurrent.futures import CancelledError
from ctypes import wintypes
from datetime import datetime
from itertools import cycle
from pathlib import Path
from queue import Empty, Queue
from threading import Condition, Event, Thread, current_thread
from uuid import uuid4


AUDIO_FORMATS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".aif", ".aiff"}
MUSIC_BIN_NAME = "网易云音乐"
DEFAULT_MP3_FOLDER = "网易云配乐_MP3"
PLAYHEAD_WIDTH = 30
CONTENT_EDGE = 6
WAVEFORM_ROW_HEIGHT = 88
WAVEFORM_GROOVE_HEIGHT = 66
# Rasterized “准备中…” alpha mask, 62 × 17. No extra runtime font or image library.
PREPARING_MASK = (
    "eNqdkl1EQ3EYxl91oqJI2U1Ml8fMjBLRxWIRKytF3SW66MNsEqXdRER0ebYuOrFyRuqiETX6IJok0uyi"
    "LrpYF0UpdvSxqO3pnJ1TO62jk56L1//j/fk/z+tPJGt5p7lyxUuqTJYK+q4GwEo6shzYiEpE2ByZTL"
    "tyVH2BxT/S6zhlqAvnRNOYz52UHeFLVgO6Lo1J2v5qHyfzMZAISdoE0mYDWnryyQmcRSJPiEUibuc"
    "9Etk3fxnTf4u7JqJSfFMhXbrviAJDRJdwSltzzFfU+4ibK0AwkSFN1Ke57ZH25UNJeSkOFCv0IMt2"
    "Ax0sO6pHu7NJHPK8iC2er2daFx6A+MRSFkjONFdIdNtn7k4deqrEH807D+J+w8tKi8aNjGRgzoCux"
    "Rqjoe2v2pjXtQa5RwB7VJtbGhcXAAKcIDcb0LuIUzSfW6a1zT+m1hF2qcUddpne4SGNc5me9Y0BY"
    "77Z/FOa35JCSi0iUh6kqwpplhiAIVaXDr5wauGeuT2EifK5Q4XOf/+pNUKLTJ+sKhpWppaTYEznl"
    "HP+H30A/QWPOg=="
)
DATE_COLORS = (
    (0.49, 0.87, 1.00),
    (1.00, 0.81, 0.48),
    (0.68, 0.95, 0.63),
    (0.82, 0.73, 1.00),
    (1.00, 0.68, 0.75),
    (0.57, 0.93, 0.84),
)
SETTINGS = Path(os.environ["APPDATA"]) / "ResolveNetEaseMusic" / "download-folder.txt"
MP3_SETTINGS = SETTINGS.with_name("mp3-folder.txt")
LOCAL_FFMPEG = SETTINGS.parent / "bin" / "ffmpeg.exe"
CODEX_FFMPEG = (
    Path.home() / ".codex" / "tools" / "whisper-venv" / "Lib" / "site-packages"
    / "imageio_ffmpeg" / "binaries" / "ffmpeg-win-x86_64-v7.1.exe"
)


def read_folder_setting(path, default=""):
    try:
        return path.read_text(encoding="utf-8").strip() or default
    except (OSError, UnicodeError):
        return default


def cloud_music_executable():
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"orpheus\shell\open\command") as key:
            command, _ = winreg.QueryValueEx(key, "")
        # The installed handler puts --webcmd immediately after the quoted path.
        executable = Path(os.path.expandvars(command.split('"')[1]))
        if executable.name.lower() == "cloudmusic.exe" and executable.is_file():
            return executable
    except (OSError, IndexError, TypeError):
        pass
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(variable)
        if root:
            executable = Path(root) / "NetEase" / "CloudMusic" / "cloudmusic.exe"
            if executable.is_file():
                return executable
    raise RuntimeError("未找到网易云音乐客户端。请先安装 Windows 版网易云音乐。")


def scan_audio(folder):
    files = []
    def report_scan_error(error):
        if error.filename == os.fspath(folder):
            raise error

    for directory, subfolders, filenames in folder.walk(on_error=report_scan_error):
        subfolders[:] = [
            name for name in subfolders
            if name.casefold() != DEFAULT_MP3_FOLDER.casefold()
            and not (directory / name).is_junction()
        ]
        for name in filenames:
            path = directory / name
            suffix = path.suffix.lower()
            if suffix not in AUDIO_FORMATS and suffix != ".ncm":
                continue
            try:
                info = path.stat()
                if not path.is_file() or not info.st_size:
                    continue
            except OSError:
                continue
            files.append((info.st_mtime_ns, path))
    files.sort(key=lambda entry: (-entry[0], entry[1].name.casefold(), str(entry[1]).casefold()))
    return files


def decrypt_ncm_key(encrypted, secret=b"hzHRAmso5kInbaxW", prefix=b"neteasecloudmusic"):
    cng = ctypes.WinDLL("bcrypt.dll")
    algorithm = ctypes.c_void_p()
    key = ctypes.c_void_p()
    size = ctypes.c_ulong()
    plaintext = ctypes.create_string_buffer(len(encrypted))
    mode = ctypes.create_unicode_buffer("ChainingModeECB")

    def check(status):
        if status:
            raise RuntimeError("NCM 密钥无效或无法解密（0x{:08X}）。".format(status & 0xFFFFFFFF))

    check(cng.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), ctypes.c_wchar_p("AES"), None, 0))
    try:
        check(cng.BCryptSetProperty(
            algorithm, ctypes.c_wchar_p("ChainingMode"), mode, ctypes.sizeof(mode), 0
        ))
        check(cng.BCryptGenerateSymmetricKey(
            algorithm, ctypes.byref(key), None, 0, secret, len(secret), 0
        ))
        check(cng.BCryptDecrypt(
            key, encrypted, len(encrypted), None, None, 0,
            plaintext, len(plaintext), ctypes.byref(size), 1
        ))
    finally:
        if key.value:
            cng.BCryptDestroyKey(key)
        cng.BCryptCloseAlgorithmProvider(algorithm, 0)
    decoded = plaintext.raw[:size.value]
    if not decoded.startswith(prefix) or len(decoded) == len(prefix):
        raise RuntimeError("NCM 密钥格式不正确。")
    return decoded[len(prefix):]


def extract_ncm_audio(source, work, file_size, cancelled=None):
    # Container layout reference: taurusxin/ncmdump, src/ncmcrypt.cpp.
    with source.open("rb") as stream:
        def read_exact(length):
            data = stream.read(length)
            if len(data) != length:
                raise RuntimeError("NCM 文件不完整，请等下载完成后重试。")
            return data

        def read_length():
            return int.from_bytes(read_exact(4), "little")

        if read_exact(8) != b"CTENFDAM":
            raise RuntimeError("无法识别 NCM 文件头。")
        read_exact(2)
        key_size = read_length()
        if not 16 <= key_size <= 4096 or key_size % 16:
            raise RuntimeError("NCM 密钥长度不正确。")
        key = decrypt_ncm_key(bytes(byte ^ 0x64 for byte in read_exact(key_size)))

        metadata_size = read_length()
        stream.seek(metadata_size, os.SEEK_CUR)
        read_exact(5)
        cover_size = read_length()
        image_size = read_length()
        if image_size > cover_size or stream.tell() + cover_size + 4 > file_size:
            raise RuntimeError("NCM 封面区或音频区不完整。")
        stream.seek(cover_size, os.SEEK_CUR)

        box = list(range(256))
        cursor = 0
        for index in range(256):
            cursor = (cursor + box[index] + key[index % len(key)]) % 256
            box[index], box[cursor] = box[cursor], box[index]
        mask = bytes(box[(box[index] + box[(index + box[index]) % 256]) % 256]
                     for index in (*range(1, 256), 0))
        block_mask = mask * 4096

        block = stream.read(len(block_mask))
        if len(block) < 4:
            raise RuntimeError("NCM 音频区不完整。")
        audio = (int.from_bytes(block, "little") ^ int.from_bytes(
            block_mask[:len(block)], "little"
        )).to_bytes(len(block), "little")
        if audio.startswith(b"fLaC"):
            output = work / "audio.flac"
        elif audio.startswith(b"ID3") or (audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0):
            output = work / "audio.mp3"
        else:
            raise RuntimeError("NCM 内的音频格式不受支持。")
        mask_value = int.from_bytes(block_mask, "little")
        with output.open("wb") as handle:
            handle.write(audio)
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise CancelledError()
                block = stream.read(len(block_mask))
                if not block:
                    break
                xor_value = mask_value if len(block) == len(block_mask) else int.from_bytes(
                    block_mask[:len(block)], "little"
                )
                handle.write((int.from_bytes(block, "little") ^ xor_value).to_bytes(len(block), "little"))
    return output


def find_ffmpeg():
    if LOCAL_FFMPEG.is_file():
        return str(LOCAL_FFMPEG)
    return shutil.which("ffmpeg.exe") or (str(CODEX_FFMPEG) if CODEX_FFMPEG.is_file() else None)


def audio_duration(source, ffmpeg):
    if source.suffix.lower() == ".ncm":
        with source.open("rb") as stream:
            if stream.read(8) != b"CTENFDAM":
                return None
            stream.read(2)
            stream.seek(int.from_bytes(stream.read(4), "little"), os.SEEK_CUR)
            size = int.from_bytes(stream.read(4), "little")
            if not 22 < size <= 2 * 1024 * 1024:
                return None
            metadata = bytes(byte ^ 0x63 for byte in stream.read(size))
        encrypted = base64.b64decode(metadata[22:], validate=True)
        if not encrypted or len(encrypted) % 16:
            return None
        tags = json.loads(decrypt_ncm_key(encrypted, b"#14ljk_!\\]&0U<'(", b"music:"))
        return int(tags["duration"]) if tags.get("duration") else None

    if source.suffix.lower() == ".wav":
        try:
            with wave.open(str(source), "rb") as stream:
                return round(stream.getnframes() * 1000 / stream.getframerate())
        except (wave.Error, EOFError):
            pass
    if ffmpeg:
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-i", str(source)],
            capture_output=True, timeout=6, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        match = re.search(rb"Duration: (\d+):(\d+):(\d+)\.(\d+)", result.stderr)
        if match:
            hours, minutes, seconds = (int(part) for part in match.groups()[:3])
            return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(
                match.group(4)[:3].ljust(3, b"0")
            )
    return None


def format_time(milliseconds):
    if milliseconds is None:
        return "—"
    minutes, seconds = divmod(milliseconds // 1000, 60)
    return "{}:{:02d}".format(minutes, seconds)


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def imported_sources(resolve_app, files):
    project = resolve_app.GetProjectManager().GetCurrentProject()
    if not project:
        return set()
    root = project.GetMediaPool().GetRootFolder()
    folder = next((f for f in root.GetSubFolderList() or [] if f.GetName() == MUSIC_BIN_NAME), None)
    if folder is None:
        return set()
    clips = [clip.GetClipProperty("File Path") for clip in folder.GetClipList() or []]
    targets = {Path(path) for path in clips if path}
    by_name = {}
    for path in targets:
        by_name.setdefault(path.name.casefold(), []).append(path)
    marked = set()
    for _, source in files:
        if source in targets:
            marked.add(source)
            continue
        if source.suffix.lower() not in {".mp3", ".ncm"}:
            continue
        prefix = source.stem[:100].casefold() + " - "
        candidates = by_name.get(source.name.casefold(), ())
        hashed = any(name.startswith(prefix) and len(name) == len(prefix) + 20 for name in by_name)
        if not candidates and not hashed:
            continue
        try:
            digest = file_digest(source)
            if prefix + digest[:16] + ".mp3" in by_name or any(
                target.is_file() and file_digest(target) == digest for target in candidates
            ):
                marked.add(source)
        except OSError:
            continue
    return marked


def convert_ncm(source, output=None):
    before = source.stat()
    with source.open("rb") as stream:
        if stream.read(8) != b"CTENFDAM":
            raise RuntimeError("这不是有效的 NCM 文件，或下载尚未完成。")
        stream.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    if output is None:
        output = source.parent / DEFAULT_MP3_FOLDER
    output.mkdir(exist_ok=True)
    target = output / ("{} - {}.mp3".format(source.stem[:100], digest.hexdigest()[:16]))
    if target.is_file() and target.stat().st_size:
        return target

    with tempfile.TemporaryDirectory(prefix=".ncm-", dir=str(output)) as temp:
        work = Path(temp)
        payload = extract_ncm_audio(source, work, before.st_size)
        ready = payload
        if payload.suffix == ".flac":
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                raise RuntimeError("这首 NCM 内含 FLAC。请安装 FFmpeg 并将 ffmpeg.exe 加入 PATH。")
            ready = work / "ready.mp3"
            try:
                subprocess.run(
                    [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", str(payload),
                     "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "320k", str(ready)],
                    capture_output=True, timeout=120, check=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                raise RuntimeError("FFmpeg 无法将 FLAC 转成 MP3。{}".format(exc)) from exc

        after = source.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise RuntimeError("源文件仍在写入，请等下载完成后重试。")
        if not ready.stat().st_size:
            raise RuntimeError("转换结果为空。")
        try:
            ready.rename(target)
        except FileExistsError:
            if not target.is_file() or not target.stat().st_size:
                raise RuntimeError("同名输出无法使用，请检查转换目录。")
    return target


def copy_mp3(source, output):
    if source.parent.resolve() == output:
        return source
    with tempfile.TemporaryDirectory(prefix=".mp3-", dir=str(output)) as temp:
        staged = Path(temp) / "audio.mp3"
        before = source.stat()
        shutil.copyfile(source, staged)
        after = source.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise RuntimeError("源文件仍在写入，请等下载完成后重试。")

        target = output / source.name
        try:
            staged.rename(target)
            return target
        except FileExistsError:
            pass

        with staged.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if target.is_file():
            with target.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == digest:
                    return target

        target = output / "{} - {}.mp3".format(source.stem[:100], digest[:16])
        try:
            staged.rename(target)
        except FileExistsError:
            if target.is_file():
                with target.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() == digest:
                        return target
            raise RuntimeError("MP3 保存目录中有同名文件，无法保存此曲目。")
        return target


def music_destination(resolve_app):
    project = resolve_app.GetProjectManager().GetCurrentProject()
    if not project:
        raise RuntimeError("请先打开一个达芬奇项目。")
    pool = project.GetMediaPool()
    root = pool.GetRootFolder()
    for folder in root.GetSubFolderList() or []:
        if folder.GetName() == MUSIC_BIN_NAME:
            return pool, folder

    folder = pool.AddSubFolder(root, MUSIC_BIN_NAME)
    if not folder:
        raise RuntimeError("无法创建媒体池素材箱「{}」。".format(MUSIC_BIN_NAME))
    return pool, folder


def import_audio(pool, folder, path):
    if not pool.SetCurrentFolder(folder):
        raise RuntimeError("无法打开媒体池素材箱「{}」。".format(MUSIC_BIN_NAME))
    target = os.path.normcase(os.path.normpath(str(path)))
    for clip in folder.GetClipList() or []:
        existing = clip.GetClipProperty("File Path")
        if existing and os.path.normcase(os.path.normpath(existing)) == target:
            return False
    if not pool.ImportMedia([str(path)]):
        raise RuntimeError("达芬奇未能导入。请确认下载已完成，并检查音频格式。")
    return True


def waveform_peaks(audio):
    width = 800
    with wave.open(str(audio), "rb") as stream:
        if stream.getsampwidth() != 2:
            raise RuntimeError("生成波形需要 16 位 WAV。请安装 FFmpeg 以解码试听。")
        frames = stream.getnframes()
        if not frames:
            raise RuntimeError("音频没有可试听的内容。")
        peaks = []
        previous = 0
        for column in range(width):
            end = (column + 1) * frames // width
            samples = array("h", stream.readframes(end - previous))
            peaks.append(max(-min(samples), max(samples)) if samples else 0)
            previous = end
    return peaks


def draw_waveform(peaks, image, markers=(None, None), duration=0, width=None, preparing=False):
    width, height = width or len(peaks), WAVEFORM_GROOVE_HEIGHT
    gutter = PLAYHEAD_WIDTH // 2
    span = width - 2 * gutter
    if peaks:
        scale = max(max(peaks), 1)
        bars = [round((height // 2 - 4) * peaks[column * len(peaks) // span] / scale)
                for column in range(span)]
        bars = [0] * gutter + bars + [0] * gutter
    else:
        bars = [0] * width
    start, end = (
        None if point is None else gutter + min(span, round(point * span / max(duration, 1)))
        for point in markers
    )
    radius, stroke = 9, 2
    edge, top = CONTENT_EDGE, 2
    frame_height = height - 2 * top

    def corner_inset(y, box_height, r):
        distance = r - min(y, box_height - 1 - y) - 0.5
        return max(0, math.ceil(r - math.sqrt(r * r - max(0, distance) ** 2) - 0.5))

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        if y < top or y >= height - top:
            rows.extend(bytes(width * 4))
            continue
        distance = abs(y - height // 2)
        frame_y = y - top
        outer = edge + corner_inset(frame_y, frame_height, radius)
        inner = width if frame_y < stroke or frame_y >= frame_height - stroke else (
            edge + stroke + corner_inset(frame_y - stroke, frame_height - 2 * stroke, radius - stroke)
        )
        for column, bar in enumerate(bars):
            if column < outer or column >= width - outer:
                rows.extend(b"\x00\x00\x00\x00")
                continue
            if column < inner or column >= width - inner:
                color = b"\x78\xa8\xd6"
            elif column in (start, end):
                color = b"\xff\xcc\x72"
            elif 0 < distance <= bar:
                color = b"\x63\xcd\xf2"
            elif peaks and distance == 0 and gutter <= column < width - gutter:
                color = b"\x39\x5c\x70"
            elif start is not None and end is not None and start < column < end:
                color = b"\x36\x35\x2e"
            else:
                color = b"\x19\x21\x2d"
            rows.extend(color)
            rows.append(255)

    if preparing:
        mask_width, mask_height = 62, 17
        mask = zlib.decompress(base64.b64decode(PREPARING_MASK))
        left, start_y = (width - mask_width) // 2, (height - mask_height) // 2
        for index, alpha in enumerate(mask):
            if alpha:
                y, x = divmod(index, mask_width)
                offset = (start_y + y) * (1 + width * 4) + 1 + (left + x) * 4
                for channel, color in enumerate((255, 213, 141)):
                    position = offset + channel
                    rows[position] = (rows[position] * (255 - alpha) + color * alpha + 127) // 255

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    )


def run_preview_ffmpeg(command, timeout, cancelled=None):
    if cancelled is None:
        subprocess.run(command, capture_output=True, timeout=timeout, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return
    if cancelled.is_set():
        raise CancelledError()
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          creationflags=subprocess.CREATE_NO_WINDOW) as process:
        deadline = time.monotonic() + timeout
        try:
            while not cancelled.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    _, errors = process.communicate(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, command, stderr=errors)
                return
            raise CancelledError()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


class AudioPreview:
    def __init__(self):
        self.alias = "netease_" + uuid4().hex
        self.player = None
        self.opened = False
        self.duration = 0
        self.playing = False
        self.temp = None
        self.source = None
        self.source_signature = None
        self.base_audio = None
        self.active_audio = None
        self.peaks = None
        self.volume = 100
        self.loop = None

    def command(self, action):
        if self.player is None:
            self.player = ctypes.WinDLL("winmm.dll")
        response = ctypes.create_unicode_buffer(256)
        code = self.player.mciSendStringW(ctypes.c_wchar_p(action), response, len(response), None)
        if code:
            self.player.mciGetErrorStringW(code, response, len(response))
            raise RuntimeError(response.value or "Windows 播放器错误 {}".format(code))
        return response.value

    def workspace(self):
        if self.temp is None:
            # The Windows waveaudio driver rejects long filenames.
            self.temp = tempfile.TemporaryDirectory(prefix="ncm-")
        return Path(self.temp.name)

    def prepare(self, source, cancelled):
        try:
            if cancelled.is_set():
                raise CancelledError()
            info = source.stat()
            audio = source
            if source.suffix.lower() == ".ncm":
                audio = extract_ncm_audio(source, self.workspace(), info.st_size, cancelled)
            ffmpeg = find_ffmpeg()
            wav = self.workspace() / "p.wav"
            if ffmpeg:
                try:
                    run_preview_ffmpeg(
                        [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", str(audio),
                         "-map", "0:a:0", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(wav)],
                        90, cancelled,
                    )
                except (subprocess.SubprocessError, OSError) as exc:
                    raise RuntimeError("无法解码试听音频。{}".format(exc)) from exc
            elif audio.suffix.lower() != ".wav":
                raise RuntimeError("试听这种格式需要 FFmpeg，请将 ffmpeg.exe 加入 PATH。")
            else:
                shutil.copyfile(audio, wav)

            after = source.stat()
            if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError("源文件仍在写入，请等下载完成后试听。")
            self.source = source
            self.source_signature = (after.st_size, after.st_mtime_ns)
            self.base_audio = wav
            self.active_audio = self.volume_audio(self.volume, cancelled)
        except Exception:
            self.stop()
            raise

    def start_prepared(self):
        self.open_wav(self.active_audio)
        self.start_at(0)

    def position(self):
        return int(self.command("status {} position".format(self.alias)))

    def open_wav(self, audio):
        self.command('open "{}" type waveaudio alias {}'.format(audio, self.alias))
        self.opened = True
        self.active_audio = audio
        self.command("set {} time format milliseconds".format(self.alias))
        self.duration = int(self.command("status {} length".format(self.alias)))

    def waveform(self, markers=(None, None), width=None):
        image = self.workspace() / "w-{}-{}-{}.png".format(*markers, width)
        if not image.exists():
            if self.peaks is None:
                self.peaks = waveform_peaks(self.base_audio)
            draw_waveform(self.peaks, image, markers, self.duration, width)
        return image

    def volume_audio(self, volume, cancelled=None):
        if volume == 100:
            return self.base_audio
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("调整试听音量需要 FFmpeg。")
        wav = self.workspace() / ("v2.wav" if self.active_audio == self.workspace() / "v1.wav" else "v1.wav")
        run_preview_ffmpeg(
            [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(self.base_audio),
             "-af", "volume={}".format(volume / 100), "-c:a", "pcm_s16le", str(wav)],
            30, cancelled,
        )
        return wav

    def set_volume(self, volume):
        if volume == self.volume:
            return
        if self.opened:
            audio = self.volume_audio(volume)
            position = self.position()
            resume = self.playing and position < self.duration
            old_audio = self.active_audio
            self.command("close " + self.alias)
            self.opened = self.playing = False
            try:
                self.open_wav(audio)
            except (OSError, RuntimeError):
                self.open_wav(old_audio)
                raise
            finally:
                if self.opened:
                    self.seek(position)
                    if resume:
                        self.start_at(position)
        self.volume = volume

    def start_at(self, position):
        if self.loop:
            start, end = self.loop
            if not start <= position < end:
                position = start
            self.command("play {} from {} to {}".format(self.alias, position, end))
        else:
            self.command("play {} from {}".format(self.alias, position))
        self.playing = True
        return position

    def set_loop(self, markers):
        self.loop = markers
        if self.playing:
            self.start_at(self.position())

    def toggle(self):
        if self.playing:
            self.command("pause " + self.alias)
            self.playing = False
        else:
            position = self.position()
            self.start_at(0 if position >= self.duration else position)

    def seek(self, position):
        position = max(0, min(position, self.duration))
        if self.playing and position < self.duration:
            return self.start_at(position)
        target = "end" if position == self.duration else str(position)
        self.command("seek {} to {}".format(self.alias, target))
        if position == self.duration:
            self.playing = False
        return position

    def stop(self):
        if self.opened:
            self.command("close " + self.alias)
            self.opened = False
        self.playing = False
        self.duration = 0
        self.loop = None
        self.source = self.base_audio = self.active_audio = None
        self.source_signature = None
        self.peaks = None
        if self.temp is not None:
            self.temp.cleanup()
            self.temp = None


class PreviewLoader:
    def __init__(self):
        self.condition = Condition()
        self.pending = self.result = self.latest = None
        self.closed = False
        self.worker = Thread(target=self.run, name="NetEasePreviewLoader", daemon=True)

    def request(self, source, volume):
        self.cancel()
        with self.condition:
            self.latest = Event()
            self.pending = (source, volume, self.latest)
            self.condition.notify()

    def cancel(self):
        with self.condition:
            if self.latest is not None:
                self.latest.set()
            self.pending = None
            result = self.result
            self.result = None
        if result is not None:
            result[0].stop()

    def poll(self):
        with self.condition:
            result = self.result
            self.result = None
            return result

    def close(self):
        try:
            self.cancel()
        finally:
            with self.condition:
                self.closed = True
                self.condition.notify()

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                source, volume, cancelled = self.pending
                self.pending = None

            prepared = AudioPreview()
            prepared.volume = volume
            error = None
            try:
                prepared.prepare(source, cancelled)
            except Exception as exc:
                error = str(exc)
            with self.condition:
                accepted = not self.closed and not cancelled.is_set()
                if accepted:
                    self.result = (prepared, error)
            if not accepted:
                prepared.stop()


def save_preview_segment(preview, markers, output):
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("导出片段需要 FFmpeg。")
    source = preview.source
    info = source.stat()
    if (info.st_size, info.st_mtime_ns) != preview.source_signature:
        raise RuntimeError("源文件在试听后有变化，请重新试听再导出。")
    start, end = markers
    output = output or source.parent / DEFAULT_MP3_FOLDER
    output.mkdir(exist_ok=True)
    target = output / "{} - {}-{} - {}.mp3".format(
        source.stem[:84], start, end, file_digest(source)[:16]
    )
    if target.is_file() and target.stat().st_size:
        return target
    with tempfile.TemporaryDirectory(prefix=".segment-", dir=str(output)) as temp:
        staged = Path(temp) / "segment.mp3"
        subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-ss", str(start / 1000),
             "-i", str(preview.base_audio), "-t", str((end - start) / 1000),
             "-c:a", "libmp3lame", "-b:a", "320k", str(staged)],
            capture_output=True, check=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if not staged.is_file() or not staged.stat().st_size:
            raise RuntimeError("片段导出结果为空。")
        try:
            staged.rename(target)
        except FileExistsError:
            if not target.is_file() or not target.stat().st_size:
                raise RuntimeError("同名片段无法使用。")
    return target


def preview_owns_keyboard(user32, panel):
    foreground = user32.GetForegroundWindow()
    if not panel or not foreground:
        return False
    if user32.GetAncestor(foreground, 2) == panel:  # GA_ROOT
        return True

    # Resolve can keep its main window active while the pointer is over the
    # floating panel. Qt may give that panel an owner or a separate top-level HWND.
    pointer = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pointer)):
        return False
    hovered = user32.WindowFromPoint(pointer)
    if not hovered or user32.GetAncestor(hovered, 2) != panel:
        return False
    if user32.GetAncestor(foreground, 3) == user32.GetAncestor(panel, 3):  # GA_ROOTOWNER
        return True
    foreground_process = wintypes.DWORD()
    panel_process = wintypes.DWORD()
    user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_process))
    user32.GetWindowThreadProcessId(panel, ctypes.byref(panel_process))
    return bool(panel_process.value and foreground_process.value == panel_process.value)


def install_preview_shortcut(user32, panel_handle, allowed, requests):
    # Give the hook its own Win32 message loop. Resolve's script dispatcher
    # receives the playback request separately on its UI thread.
    hook_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, hook_type, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    message_ptr = ctypes.POINTER(wintypes.MSG)
    user32.PeekMessageW.argtypes = [message_ptr, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.argtypes = [message_ptr, wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    kernel32 = ctypes.WinDLL("kernel32.dll")
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    ready = Event()
    installed = Event()
    stopping = Event()
    space_down = captured = False

    @hook_type
    def on_keyboard(code, message, data):
        nonlocal space_down, captured
        if code == 0 and data and ctypes.c_uint.from_address(data).value == 0x20:
            if message in (0x0101, 0x0105):  # WM_KEYUP / WM_SYSKEYUP
                space_down = False
                if captured:
                    captured = False
                    return 1
            elif message == 0x0100:  # WM_KEYDOWN
                if not space_down and allowed.is_set() and not stopping.is_set():
                    if (preview_owns_keyboard(user32, panel_handle())
                            and not any(user32.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12))):
                        captured = True
                        requests.put(None)
                space_down = True
                if captured:
                    return 1
        return user32.CallNextHookEx(None, code, message, data)

    def run_hook():
        message = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        hook = user32.SetWindowsHookExW(13, on_keyboard, kernel32.GetModuleHandleW(None), 0)
        if hook:
            installed.set()
        ready.set()
        if hook:
            try:
                while not stopping.is_set() and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                    pass
            finally:
                user32.UnhookWindowsHookEx(hook)

    worker = Thread(target=run_hook, name="NetEasePreviewShortcut", daemon=True)
    worker.start()
    if not ready.wait(0.5) or not installed.is_set():
        stopping.set()
        user32.PostThreadMessageW(worker.native_id, 0x0012, 0, 0)  # WM_QUIT
        return None

    def uninstall():
        stopping.set()
        user32.PostThreadMessageW(worker.native_id, 0x0012, 0, 0)
        worker.join(0.5)

    return uninstall


def show_panel(resolve_app, fusion_app, bmd_app):
    ui = fusion_app.UIManager
    dispatcher = bmd_app.UIDispatcher(ui)
    window_id = "ResolveNetEaseMusic"
    window_title = "网易云配乐导入"
    import_style = """
        QPushButton {
            background-color: #175ac2; color: #ffffff;
            border: 1px solid #5394f5; border-radius: 7px;
            font-weight: bold; padding: 5px 14px;
        }
        QPushButton:hover { background-color: #2066cf; }
        QPushButton:pressed { background-color: #104b9e; }
        QPushButton:disabled { background-color: #3b4455; color: #bdc6d6; border-color: #59657b; }
    """
    refresh_style = """
        QPushButton {
            background-color: #164641; color: #e6fffb;
            border: 1px solid #44988c; border-radius: 7px; padding: 4px 12px;
        }
        QPushButton:hover { background-color: #205c54; }
        QPushButton:pressed { background-color: #103a36; }
    """
    artwork = tempfile.TemporaryDirectory(prefix="ncm-ui-")
    empty_wave = Path(artwork.name) / "wave-frame.png"
    draw_waveform([], empty_wave, width=800)

    def border_image(image):
        return 'border-image: url("' + image.as_posix() + '") 0 0 0 0 stretch stretch;'

    seek_style = f"""
        QSlider {{
            min-height: 84px; padding: 0; background: transparent; border: none;
        }}
        QSlider::groove:horizontal {{
            height: {WAVEFORM_GROOVE_HEIGHT}px; background: transparent; border: none; subcontrol-origin: content;
        }}
        QSlider::sub-page:horizontal, QSlider::add-page:horizontal {{
            background: transparent; border: none;
        }}
        QSlider::handle:horizontal {{
            width: {PLAYHEAD_WIDTH}px; margin: -4px 0; border: none; border-radius: 5px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(224, 237, 255, 48), stop:0.45 rgba(224, 237, 255, 48),
                stop:0.46 #ff5263, stop:0.54 #ff5263,
                stop:0.55 rgba(224, 237, 255, 48), stop:1 rgba(224, 237, 255, 48));
        }}
        QSlider::handle:hover, QSlider::handle:pressed {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(224, 237, 255, 87), stop:0.45 rgba(224, 237, 255, 87),
                stop:0.46 #ff5263, stop:0.54 #ff5263,
                stop:0.55 rgba(224, 237, 255, 87), stop:1 rgba(224, 237, 255, 87));
        }}
        QSlider::handle:disabled {{ background: transparent; }}
    """
    empty_seek_style = seek_style + "QSlider::groove:horizontal { " + border_image(empty_wave) + " }"
    transport_style = """
        QPushButton {
            background: #2c3442; color: #eef4ff; border: 1px solid #4c596c;
            border-radius: 7px; padding: 3px 8px;
        }
        QPushButton:hover { background: #394a61; }
        QPushButton:pressed { background: #1e2a3d; }
        QPushButton:disabled { background: #272c35; color: #7e8a9c; border-color: #363e4b; }
    """
    tree_style = """
        QTreeView {
            border: 2px solid #78a8d6; border-radius: 8px;
            background-color: #242832; alternate-background-color: #2b303b;
        }
    """

    def aligned(widget, weight=0):
        return ui.HGroup({"Weight": weight, "Spacing": 0, "Margin": 0}, [
            ui.HGap(CONTENT_EDGE, 0), widget, ui.HGap(CONTENT_EDGE, 0),
        ])

    window = dispatcher.AddWindow(
        {
            "ID": window_id, "WindowTitle": window_title,
            "Geometry": [260, 48, 940, 850], "MinimumSize": [790, 686],
            "Events": {"Close": True, "Hide": True, "KeyPress": True, "Resize": True},
        },
        ui.HGroup({"ID": "LayoutRoot", "Spacing": 0, "Margin": 0}, [
            ui.Label({
                "ID": "LeftGutter", "Text": "", "MinimumSize": [4, 1],
                "MaximumSize": [4, 16777215], "Weight": 0,
            }),
            ui.VGroup({"ID": "ContentColumn", "Spacing": 8, "Margin": 0, "Weight": 1}, [
                ui.VGap(18, 0),
                aligned(ui.HGroup({"Spacing": 12}, [
                    ui.Button({"ID": "OpenClient", "Text": "打开网易云音乐客户端", "MinimumSize": [224, 36], "Weight": 0}),
                    ui.Label({"Text": "在客户端搜索、试听并下载配乐"}),
                ])),
                aligned(ui.HGroup({"Spacing": 12}, [
                    ui.Button({"ID": "Browse", "Text": "网易云下载目录", "MinimumSize": [156, 36], "Weight": 0}),
                    ui.Button({
                        "ID": "Refresh", "Text": "刷新列表", "FixedSize": [130, 36],
                        "Weight": 0, "StyleSheet": refresh_style,
                    }),
                    ui.LineEdit({
                        "ID": "Folder", "MinimumSize": [280, 36],
                        "Text": read_folder_setting(SETTINGS, str(Path.home() / "Downloads")),
                    }),
                ])),
                aligned(ui.HGroup({"Spacing": 12}, [
                    ui.Button({"ID": "BrowseMP3", "Text": "MP3 保存目录", "FixedSize": [156, 36], "Weight": 0}),
                    ui.Button({"ID": "ResetMP3", "Text": "恢复默认", "FixedSize": [130, 36], "Weight": 0}),
                    ui.LineEdit({
                        "ID": "MP3Folder", "ReadOnly": True, "Text": read_folder_setting(MP3_SETTINGS),
                        "PlaceholderText": "默认：NCM 转到下载目录下，已有 MP3 原地导入",
                        "MinimumSize": [280, 36],
                    }),
                ])),
                aligned(ui.Label({
                    "Text": "导入到媒体池「{}」。按住 Ctrl 点选，按住 Shift 连选。".format(MUSIC_BIN_NAME),
                })),
                aligned(ui.HGroup({"Spacing": 12}, [
                    ui.LineEdit({
                        "ID": "Search", "PlaceholderText": "按文件名搜索，支持多个关键词",
                        "MinimumSize": [300, 32], "Events": {"TextChanged": True},
                    }),
                    ui.CheckBox({"ID": "NewOnly", "Text": "仅看本次新下载", "Weight": 0, "Events": {"Toggled": True}}),
                ])),
                aligned(ui.Label({"ID": "Status", "Text": "", "WordWrap": True, "MinimumSize": [0, 44]})),
                aligned(ui.Tree({
                    "ID": "Files", "ColumnCount": 5, "SelectionMode": "ExtendedSelection",
                    "SortingEnabled": False, "RootIsDecorated": False,
                    "AlternatingRowColors": True, "MinimumSize": [460, 96], "StyleSheet": tree_style,
                    "Events": {"ItemClicked": True, "ItemDoubleClicked": True},
                }), weight=1),
                aligned(ui.HGroup({"Spacing": 12}, [
                    ui.Label({
                        "ID": "PreviewTrack", "Text": "试听播放器  ·  点击播放条或双击歌曲",
                        "MinimumSize": [0, 30], "MaximumSize": [16777215, 30],
                        "StyleSheet": "QLabel { color: #eef4ff; font-size: 15px; font-weight: bold; }",
                    }),
                    ui.Button({
                        "ID": "LoadWaveform", "Text": "加载波形", "FixedSize": [100, 30],
                        "Weight": 0, "Enabled": False, "StyleSheet": transport_style,
                    }),
                    ui.Label({
                        "ID": "PreviewState", "Text": "未播放", "Alignment": 0x84,
                        "FixedSize": [88, 25], "Weight": 0,
                    }),
                    ui.Label({
                        "ID": "PreviewTime", "Text": "0:00 / 0:00", "Alignment": 0x82,
                        "FixedSize": [132, 26], "Weight": 0,
                        "StyleSheet": "QLabel { color: #d5e4f6; font-family: Consolas; font-size: 14px; }",
                    }),
                ])),
                ui.HGroup({"Weight": 0, "Spacing": 0, "MinimumSize": [480, WAVEFORM_ROW_HEIGHT]}, [
                    ui.Slider({
                        "ID": "Seek", "Orientation": "Horizontal", "Minimum": 0, "Maximum": 1,
                        "Value": 0, "SingleStep": 1000, "PageStep": 0, "Tracking": False,
                        "StyleSheet": empty_seek_style, "MinimumSize": [480, WAVEFORM_ROW_HEIGHT],
                        "MaximumSize": [16777215, WAVEFORM_ROW_HEIGHT], "Weight": 1,
                        "ToolTip": "未播放时点这里开始试听。播放后点击定位，也可以拖动播放头。",
                        "Events": {"ValueChanged": True, "SliderPressed": True,
                                   "SliderMoved": True, "SliderReleased": True, "ActionTriggered": True},
                    }),
                ]),
                ui.VGap(10, 0),
                aligned(ui.HGroup({"Spacing": 8}, [
                    ui.Button({"ID": "Preview", "Text": "试听所选", "FixedSize": [105, 34], "Weight": 0, "StyleSheet": transport_style}),
                    ui.Button({"ID": "Previous", "Text": "上一首", "FixedSize": [78, 34], "Weight": 0, "StyleSheet": transport_style}),
                    ui.Button({"ID": "Back5", "Text": "−5 秒", "FixedSize": [69, 34], "Weight": 0, "Enabled": False, "StyleSheet": transport_style}),
                    ui.Button({
                        "ID": "PausePreview", "Text": "播放 / 暂停", "FixedSize": [112, 34],
                        "Weight": 0, "Enabled": False, "ToolTip": "空格：暂停或继续试听",
                        "StyleSheet": transport_style,
                    }),
                    ui.Button({"ID": "Ahead5", "Text": "+5 秒", "FixedSize": [69, 34], "Weight": 0, "Enabled": False, "StyleSheet": transport_style}),
                    ui.Button({"ID": "Next", "Text": "下一首", "FixedSize": [78, 34], "Weight": 0, "StyleSheet": transport_style}),
                    ui.HGap(18, 1),
                    ui.Button({"ID": "StopPreview", "Text": "停止", "FixedSize": [78, 34], "Weight": 0, "Enabled": False, "StyleSheet": transport_style}),
                ])),
                aligned(ui.HGroup({"Spacing": 10}, [
                    ui.Button({"ID": "SetA", "Text": "标记 A", "FixedSize": [96, 32], "Weight": 0}),
                    ui.Button({"ID": "SetB", "Text": "标记 B", "FixedSize": [96, 32], "Weight": 0}),
                    ui.CheckBox({
                        "ID": "LoopAB", "Text": "循环 A–B", "Weight": 0, "Enabled": False,
                        "Events": {"Toggled": True},
                    }),
                    ui.Button({"ID": "ClearAB", "Text": "清除选段", "FixedSize": [104, 32], "Weight": 0}),
                    ui.Label({"ID": "Range", "Text": "A —  /  B —", "MinimumSize": [130, 26]}),
                    ui.Button({"ID": "ImportAB", "Text": "只导入选段", "FixedSize": [140, 32], "Weight": 0, "Enabled": False}),
                ])),
                aligned(ui.HGroup({"Spacing": 14, "MinimumSize": [0, 50]}, [
                    ui.Label({"ID": "VolumeLabel", "Text": "试听音量 100%", "MinimumSize": [110, 26], "Weight": 0}),
                    ui.Slider({
                        "ID": "Volume", "Orientation": "Horizontal", "Minimum": 0, "Maximum": 100,
                        "Value": 100, "SingleStep": 5, "Tracking": False, "FixedSize": [126, 26],
                        "Weight": 0, "Events": {"ValueChanged": True},
                    }),
                    ui.HGap(16, 1),
                    ui.Button({
                        "ID": "Import", "Text": "导入所选音频", "FixedSize": [204, 44],
                        "Weight": 0, "StyleSheet": import_style,
                    }),
                ])),
                ui.VGap(12, 0),
                ui.Button({"ID": "ClockPulse", "Hidden": True, "Events": {"Clicked": True}}),
            ]),
            ui.Label({
                "ID": "RightGutter", "Text": "", "MinimumSize": [44, 1],
                "MaximumSize": [44, 16777215], "Weight": 0,
            }),
        ]),
    )
    items = window.GetItems()
    tree = items["Files"]
    header = tree.NewItem()
    header.Text[0] = "排名"
    header.Text[1] = "修改时间（最新在前）"
    header.Text[2] = "时长"
    header.Text[3] = "状态"
    header.Text[4] = "文件名"
    tree.SetHeaderItem(header)
    tree.ColumnWidth[0] = 62
    tree.ColumnWidth[1] = 172
    tree.ColumnWidth[2] = 62
    tree.ColumnWidth[3] = 106
    rows = []
    listed_folder = None
    found = []
    baseline = {}
    new_paths = set()
    imported_paths = set()
    stamps = {}
    durations = {}
    pending = set()
    duration_jobs = Queue()
    duration_results = Queue()
    end_worker = Event()
    duration_ffmpeg = find_ffmpeg()
    markers = [None, None]
    preview = AudioPreview()
    loader = PreviewLoader()
    loading_source = None
    seek = items["Seek"]
    displayed_position = 0
    displayed_time = ""
    displayed_wave = None
    waveform_loaded = False
    waveform_needs_resize = False
    layout_gutters = (4, 44)
    dragging = False
    closing = False
    remove_shortcut = None
    panel_hwnd = None
    space_allowed = Event()
    space_requests = Queue()
    last_clock = 0.0
    clock_pending = Event()

    def status(message):
        items["Status"].Text = message

    def display_time(position, scrubbing=False):
        nonlocal displayed_time
        elapsed = format_time(position)
        if scrubbing:
            elapsed += ".{}".format(position % 1000 // 100)
        text = "{} / {}".format(elapsed, format_time(preview.duration))
        if text != displayed_time:
            displayed_time = text
            items["PreviewTime"].Text = text

    def set_player_state(label, foreground, background):
        badge = items["PreviewState"]
        if badge.Text != label or not badge.StyleSheet:
            badge.Text = label
            badge.StyleSheet = (
                "QLabel {{ qproperty-alignment: AlignCenter; color: {}; background: {}; border-radius: 6px; }}".format(
                    foreground, background
                )
            )

    def update_player_state():
        if loading_source is not None:
            set_player_state("准备中", "#ffe4a6", "#493b24")
        elif not preview.opened:
            set_player_state("未播放", "#c3cfde", "#323b48")
        elif preview.playing and preview.loop:
            set_player_state("循环中", "#e6ddff", "#493963")
        elif preview.playing:
            set_player_state("播放中", "#d0f7e7", "#1b4a42")
        elif displayed_position >= preview.duration:
            set_player_state("已播完", "#c3cfde", "#323b48")
        else:
            set_player_state("已暂停", "#ffe4a6", "#493b24")

    def probe_durations():
        while not end_worker.is_set():
            job = duration_jobs.get()
            if job is None or end_worker.is_set():
                return
            source, stamp = job
            try:
                duration = audio_duration(source, duration_ffmpeg)
            except Exception:
                duration = None
            duration_results.put((source, stamp, duration))

    duration_worker = Thread(target=probe_durations, name="NetEaseMusicDuration", daemon=True)

    def collect_durations():
        changed = set()
        for _ in range(16):
            try:
                source, stamp, duration = duration_results.get_nowait()
            except Empty:
                break
            pending.discard((source, stamp))
            key = (source, stamp)
            durations.setdefault(key, duration)
            if stamps.get(source) == stamp:
                changed.add(source)
        if changed:
            for row, path in rows:
                if path in changed:
                    row.Text[2] = format_time(durations[(path, stamps[path])])

    def display_position(position):
        nonlocal displayed_position
        displayed_position = position
        if int(seek.Value) != position:
            seek.Value = position
        display_time(position)

    def reset_preview_display():
        nonlocal dragging, displayed_wave, waveform_needs_resize, waveform_loaded
        dragging = False
        displayed_wave = None
        waveform_needs_resize = False
        waveform_loaded = False
        display_position(0)
        seek.Maximum = 1
        seek.Enabled = True
        items["LoadWaveform"].Enabled = False
        items["PausePreview"].Enabled = False
        items["StopPreview"].Enabled = False
        items["Back5"].Enabled = False
        items["Ahead5"].Enabled = False
        markers[:] = [None, None]
        items["Range"].Text = "A —  /  B —"
        items["LoopAB"].Checked = False
        items["LoopAB"].Enabled = False
        items["ImportAB"].Enabled = False
        show_idle_frame()
        items["PreviewTrack"].Text = "试听播放器  ·  点击播放条或双击歌曲"
        items["PreviewTrack"].ToolTip = ""

    def stop_playback():
        nonlocal loading_source
        loading_source = None
        loader.cancel()
        reset_preview_display()
        preview.stop()
        display_time(0)
        update_player_state()

    def tick():
        if dragging:
            return
        result = loader.poll()
        if result is not None:
            finish_preview(*result)
        if waveform_needs_resize:
            if preview.opened:
                show_waveform()
            else:
                show_idle_frame()
        collect_durations()
        if not preview.playing:
            return
        position = min(preview.position(), preview.duration)
        if preview.loop and position >= preview.loop[1] - 2:
            position = preview.loop[0]
            preview.start_at(position)
        elif position >= preview.duration:
            preview.playing = False
        display_position(position)
        if not preview.playing:
            update_player_state()

    def jump_to(position):
        if not preview.opened:
            return
        try:
            if preview.loop and not preview.loop[0] <= position < preview.loop[1]:
                preview.loop = None
                items["LoopAB"].Checked = False
            position = preview.seek(position)
            display_position(position)
            update_player_state()
        except (OSError, RuntimeError) as exc:
            status("无法跳转试听位置 · {}".format(exc))

    def seek_changed(_event):
        position = int(seek.Value)
        if not dragging and position != displayed_position:
            jump_to(position)

    def begin_drag(_event):
        nonlocal dragging
        if not preview.opened:
            play_from_track()
            return
        dragging = True

    def move_drag(_event):
        if dragging:
            display_time(int(seek.SliderPosition), scrubbing=True)

    def end_drag(_event):
        nonlocal dragging
        if not dragging:
            return
        position = int(seek.SliderPosition)
        dragging = False
        jump_to(position)

    def skip_preview(offset):
        if preview.opened:
            try:
                jump_to(max(0, min(preview.position() + offset, preview.duration)))
            except (OSError, RuntimeError) as exc:
                status("无法读取当前试听位置 · {}".format(exc))

    def toggle_preview(_event):
        try:
            if preview.opened:
                preview.toggle()
                display_position(preview.position())
                update_player_state()
        except (OSError, RuntimeError) as exc:
            status("无法暂停或继续试听 · {}".format(exc))

    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetKeyState.argtypes = [ctypes.c_int]
    user32.GetKeyState.restype = ctypes.c_short

    def focus_preview_panel(_event=None):
        nonlocal panel_hwnd
        panel_hwnd = user32.FindWindowW(None, window_title)
        if panel_hwnd and user32.GetForegroundWindow() != panel_hwnd:
            user32.SetForegroundWindow(panel_hwnd)
        tree.SetFocus("MouseFocusReason")
        update_space_availability()

    def first_coordinate(value, name):
        if isinstance(value, dict):
            return next(value[key] for key in (name, name.lower(), 0, "0", 1, "1") if key in value)
        return value[0]

    def waveform_action(_event):
        # Qt emits ActionTriggered before applying the value of a groove click.
        # Substitute the position under the pointer for its default page step.
        if dragging or loading_source is not None:
            return
        if not (user32.GetKeyState(1) | user32.GetAsyncKeyState(1)) & 0x8000:
            return
        hwnd = user32.GetForegroundWindow()
        title = ctypes.create_unicode_buffer(120)
        if not hwnd or not user32.GetWindowTextW(hwnd, title, len(title)) or title.value != window_title:
            return
        cursor = wintypes.POINT()
        client = wintypes.RECT()
        if not (user32.GetCursorPos(ctypes.byref(cursor)) and user32.ScreenToClient(hwnd, ctypes.byref(cursor))
                and user32.GetClientRect(hwnd, ctypes.byref(client)) and client.right > 0):
            return

        try:
            logical_width = float(first_coordinate(window.Size(), "Width"))
            window_origin = first_coordinate(window.MapToGlobal([0, 0]), "X")
            seek_origin = first_coordinate(seek.MapToGlobal([0, 0]), "X")
            left = float(seek_origin) - float(window_origin)
            width = float(first_coordinate(seek.Size(), "Width"))
            x = cursor.x * logical_width / client.right - left
        except (AttributeError, IndexError, StopIteration, TypeError, ValueError):
            return
        inset = PLAYHEAD_WIDTH / 2
        if logical_width > 0 and width > 2 * inset and 0 <= x <= width:
            if not preview.opened:
                play_from_track()
                return
            if waveform_needs_resize:
                show_waveform()
            fraction = max(0.0, min(1.0, (x - inset) / (width - 2 * inset)))
            position = round(fraction * preview.duration)
            seek.SliderPosition = position
            if position != displayed_position:
                jump_to(position)

    def queue_clock():
        # Resolve's script loop may not dispatch Win32 thread timer messages.
        # Post one event through its UI dispatcher and wait for acknowledgement.
        while not end_worker.wait(0.09):
            if not clock_pending.is_set():
                clock_pending.set()
                try:
                    ui.QueueEvent(items["ClockPulse"], "Clicked", {})
                except Exception:
                    return

    clock_worker = Thread(target=queue_clock, name="NetEasePreviewClock", daemon=True)

    def preview_clock(_event):
        nonlocal last_clock
        if closing or current_thread() is clock_worker:
            return
        clock_pending.clear()
        last_clock = time.monotonic()
        try:
            update_space_availability()
            try:
                space_requests.get_nowait()
            except Empty:
                pass
            else:
                if space_allowed.is_set():
                    play_or_toggle_with_space()
            tick()
        except Exception as exc:
            preview.playing = False
            status("无法读取试听进度 · {}".format(exc))

    def selected_folder():
        entered = items["Folder"].Text.strip().strip('"')
        if not entered:
            raise ValueError("请选择网易云客户端设置中的下载目录。")
        folder = Path(os.path.expandvars(entered)).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise ValueError("下载目录不存在。请在网易云客户端设置中核对目录。")
        return folder.resolve()

    def render_rows(_event=None):
        selected = {path for row, path in rows if row.Selected}
        rows.clear()
        tree.Clear()
        keywords = items["Search"].Text.casefold().split()
        new_only = items["NewOnly"].Checked
        palette = cycle(DATE_COLORS)
        previous_date = None
        for rank, (modified_ns, path) in enumerate(found, start=1):
            modified = datetime.fromtimestamp(modified_ns // 1_000_000_000)
            date = modified.date()
            if date != previous_date:
                red, green, blue = next(palette)
                date_color = {"R": red, "G": green, "B": blue, "A": 1.0}
                previous_date = date
            if (new_only and path not in new_paths) or any(word not in path.name.casefold() for word in keywords):
                continue
            key = (path, modified_ns)
            row = tree.NewItem()
            row.Text[0] = str(rank)
            row.Text[1] = modified.strftime("%Y-%m-%d %H:%M:%S")
            row.Text[2] = format_time(durations.get(key))
            row.Text[3] = " · ".join(
                label for present, label in ((path in new_paths, "新下载"), (path in imported_paths, "已导入"))
                if present
            )
            row.Text[4] = path.name
            row.TextColor[4] = date_color
            tree.AddTopLevelItem(row)
            if path in selected:
                row.Selected = True
            rows.append((row, path))
            if key not in pending and key not in durations:
                pending.add(key)
                duration_jobs.put(key)
        items["Import"].Enabled = bool(rows)
        status("显示 {} / {} 首，本次新下载 {} 首。".format(len(rows), len(found), len(new_paths)))

    def refresh(_event=None):
        nonlocal listed_folder, found, baseline, new_paths, imported_paths, stamps
        try:
            folder = selected_folder()
            found = scan_audio(folder)
        except (OSError, ValueError) as exc:
            found = []
            render_rows()
            status(str(exc))
            return
        stamps = {path: stamp for stamp, path in found}
        if folder != listed_folder:
            if listed_folder is not None:
                stop_playback()
            baseline = stamps.copy()
            listed_folder = folder
        new_paths = {path for path, stamp in stamps.items() if baseline.get(path) != stamp}
        try:
            imported_paths = imported_sources(resolve_app, found)
        except (AttributeError, RuntimeError, TypeError):
            imported_paths = set()
        render_rows()
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(str(listed_folder), encoding="utf-8")
        except OSError:
            status(items["Status"].Text + " 下载目录未能保存。")

    def browse(_event):
        selected = fusion_app.RequestDir(items["Folder"].Text)
        if selected:
            items["Folder"].Text = selected
            refresh()

    def set_mp3_folder(path):
        try:
            MP3_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            MP3_SETTINGS.write_text(path, encoding="utf-8")
        except OSError as exc:
            status("无法保存 MP3 目录设置。{}".format(exc))
            return
        items["MP3Folder"].Text = path
        status("MP3 将保存到 {}".format(path) if path else "已恢复默认的 MP3 保存方式。")

    def browse_mp3(_event):
        current = items["MP3Folder"].Text or items["Folder"].Text
        selected = fusion_app.RequestDir(current)
        if selected:
            set_mp3_folder(selected)

    def open_client(_event):
        try:
            executable = cloud_music_executable()
            os.startfile(str(executable))
        except (OSError, RuntimeError) as exc:
            status("无法打开客户端。{}".format(exc))
            return
        status("请在网易云音乐客户端搜索。下载完成后回来点「刷新」。")

    def apply_wave_image(image):
        nonlocal displayed_wave, waveform_needs_resize
        waveform_needs_resize = False
        if image == displayed_wave:
            return
        displayed_wave = image
        seek.StyleSheet = seek_style + "QSlider::groove:horizontal { " + border_image(image) + " }"

    def waveform_width():
        try:
            width = int(first_coordinate(seek.Size(), "Width"))
        except (AttributeError, IndexError, StopIteration, TypeError, ValueError):
            return 0
        return min(width, 4096)

    def show_waveform():
        if not waveform_loaded:
            show_idle_frame()
            return
        width = waveform_width()
        image = preview.waveform(markers, width=width) if width > PLAYHEAD_WIDTH else (
            preview.waveform(markers)
        )
        apply_wave_image(image)

    def show_idle_frame(preparing=None):
        width = waveform_width()
        if width <= PLAYHEAD_WIDTH:
            width = 800
        if preparing is None:
            preparing = loading_source is not None
        bounds = tuple(markers)
        duration = preview.duration if any(point is not None for point in bounds) else 0
        image = Path(artwork.name) / "frame-{}-{}-{}-{}-{}.png".format(
            width, int(preparing), duration, *bounds
        )
        if not image.is_file():
            draw_waveform([], image, bounds, duration, width, preparing)
        apply_wave_image(image)

    def load_waveform(_event):
        nonlocal waveform_loaded
        if not preview.opened or waveform_loaded:
            return
        try:
            show_idle_frame(preparing=True)
            seek.Repaint()
            waveform_loaded = True
            show_waveform()
            items["LoadWaveform"].Enabled = False
            status("波形已加载。")
        except Exception as exc:
            waveform_loaded = False
            show_idle_frame()
            status("无法加载波形 · {}".format(exc))

    def center_content():
        nonlocal layout_gutters
        try:
            root = items["LayoutRoot"]
            panel_width = int(first_coordinate(window.Size(), "Width"))
            root_width = int(first_coordinate(root.Size(), "Width"))
            panel_x = first_coordinate(window.MapToGlobal([0, 0]), "X")
            root_x = first_coordinate(root.MapToGlobal([0, 0]), "X")
            left = int(root_x) - int(panel_x)
            right = panel_width - left - root_width
            if panel_width <= 0 or root_width < panel_width * 0.75:
                return
            margin = max(24, left, right) + 4
            gutters = (margin - left, margin - right)
            if gutters != layout_gutters:
                for name, width in zip(("LeftGutter", "RightGutter"), gutters):
                    items[name].MaximumSize = [width, 16777215]
                    items[name].MinimumSize = [width, 1]
                layout_gutters = gutters
                window.RecalcLayout()
        except (AttributeError, IndexError, KeyError, StopIteration, TypeError, ValueError):
            return

    def player_resized(_event):
        nonlocal waveform_needs_resize
        if not closing:
            center_content()
            waveform_needs_resize = True

    def preview_source(source):
        nonlocal loading_source
        try:
            if selected_folder() != listed_folder:
                status("下载目录已更改，请先点「刷新列表」。")
                return
            if not source.is_file() or not source.stat().st_size:
                raise RuntimeError("音频文件不可用，请刷新列表。")
            stop_playback()
            loading_source = source
            set_track_title(source)
            items["StopPreview"].Enabled = True
            seek.Enabled = False
            show_idle_frame()
            update_player_state()
            volume = int(items["Volume"].Value)
            if time.monotonic() - last_clock < 0.8:
                loader.request(source, volume)
            else:
                # The panel can still play a track when its event pump is unavailable.
                seek.Repaint()
                prepared = AudioPreview()
                prepared.volume = volume
                prepared.prepare(source, Event())
                finish_preview(prepared, None)
        except Exception as exc:
            stop_playback()
            set_player_state("无法播放", "#ffd6d6", "#5c3339")
            status("试听失败 · {}".format(exc))

    def set_track_title(source):
        track = source.name if len(source.name) <= 75 else source.name[:72] + "…"
        items["PreviewTrack"].Text = track
        items["PreviewTrack"].ToolTip = source.name

    def finish_preview(prepared, error):
        nonlocal loading_source, preview
        loading_source = None
        preview = prepared
        try:
            if error is not None:
                raise RuntimeError(error)
            preview.start_prepared()
            show_idle_frame()
            player_resized(None)
            source = preview.source
            stamp = stamps.get(source)
            if stamp is not None:
                durations[(source, stamp)] = preview.duration
            for row, path in rows:
                if path == source:
                    row.Text[2] = format_time(preview.duration)
                    break
            seek.Maximum = preview.duration
            seek.Enabled = True
            items["LoadWaveform"].Enabled = True
            items["PausePreview"].Enabled = True
            items["StopPreview"].Enabled = True
            items["Back5"].Enabled = True
            items["Ahead5"].Enabled = True
            set_track_title(source)
            display_position(0)
            update_player_state()
            status("可直接点击或拖动定位。需要看波形时点「加载波形」。")
        except Exception as exc:
            stop_playback()
            set_player_state("无法播放", "#ffd6d6", "#5c3339")
            status("试听失败 · {}".format(exc))

    def preview_selected(_event):
        selected = [path for row, path in rows if row.Selected]
        if len(selected) == 1:
            focus_preview_panel()
            preview_source(selected[0])
        else:
            status("试听时请选择一首音频。")

    def play_from_track():
        if loading_source is not None:
            return
        if not rows:
            status("当前列表没有可试听的曲目。")
            return
        row, source = next(((row, path) for row, path in rows if row.Selected), rows[0])
        row.Selected = True
        preview_source(source)

    def preview_double_clicked(event):
        rank = event["item"].Text[0]
        for row, path in rows:
            if row.Text[0] == rank:
                preview_source(path)
                return

    def move_track(direction):
        if not rows:
            status("当前列表没有可试听的曲目。")
            return
        current = loading_source or preview.source
        if current is None:
            current = next((path for row, path in rows if row.Selected), None)
        index = next((i for i, (_, path) in enumerate(rows) if path == current), None)
        target = (0 if direction > 0 else len(rows) - 1) if index is None else index + direction
        if not 0 <= target < len(rows):
            status("已到当前列表的最后一首。" if direction > 0 else "已到当前列表的第一首。")
            return
        selected, source = rows[target]
        for row, _ in rows:
            if row.Selected:
                row.Selected = False
        selected.Selected = True
        tree.ScrollToItem(selected)
        preview_source(source)

    def can_toggle_with_space():
        return not closing and (preview.opened or loading_source is not None or bool(rows)) and not any(
            items[name].HasFocus() for name in ("Search", "Folder", "MP3Folder")
        )

    def update_space_availability():
        if can_toggle_with_space():
            space_allowed.set()
        else:
            space_allowed.clear()

    def play_or_toggle_with_space():
        if loading_source is not None:
            return
        if preview.opened:
            toggle_preview(None)
        else:
            play_from_track()

    def key_pressed(event):
        if event.get("IsAutoRepeat"):
            return
        if event.get("Key") in (27, 0x01000000, "Escape", "Esc", "Key_Escape") or event.get("Text") == "\x1b":
            close(event)
            return
        if event.get("Key") not in (32, "Space", "Key_Space") and event.get("Text") != " ":
            return
        if can_toggle_with_space():
            play_or_toggle_with_space()

    def volume_changed(_event):
        volume = int(items["Volume"].Value)
        try:
            if loading_source is not None:
                preview_source(loading_source)
            else:
                preview.set_volume(volume)
            items["VolumeLabel"].Text = "试听音量 {}%".format(volume)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            items["Volume"].Value = preview.volume
            status("无法调整试听音量 · {}".format(exc))

    def selected_range():
        start, end = markers
        return (start, end) if start is not None and end is not None and end > start else None

    def update_markers():
        def label(point):
            return "—" if point is None else "{}.{:03d}".format(format_time(point), point % 1000)

        limits = selected_range()
        items["Range"].Text = "A {}  /  B {}".format(*(label(point) for point in markers))
        items["LoopAB"].Enabled = limits is not None
        items["ImportAB"].Enabled = limits is not None
        if limits is None:
            items["LoopAB"].Checked = False
        loop = limits if items["LoopAB"].Checked else None
        if loop != preview.loop:
            preview.set_loop(loop)
        update_player_state()
        show_waveform()

    def set_marker(index):
        if not preview.opened:
            status("请先试听一首歌，再标记片段。")
            return
        try:
            point = min(preview.position(), preview.duration)
            other = markers[1 - index]
            if other is not None and (point >= other if index == 0 else point <= other):
                status("A 点必须早于 B 点。")
                return
            markers[index] = point
            update_markers()
        except (OSError, RuntimeError) as exc:
            status("无法标记片段 · {}".format(exc))

    def clear_markers(_event):
        if preview.opened:
            markers[:] = [None, None]
            update_markers()

    def loop_toggled(_event):
        limits = selected_range() if items["LoopAB"].Checked else None
        if preview.opened and limits != preview.loop:
            try:
                preview.set_loop(limits)
                display_position(preview.position())
                update_player_state()
            except (OSError, RuntimeError) as exc:
                status("无法循环试听 · {}".format(exc))

    def mp3_output():
        output_path = items["MP3Folder"].Text
        output = Path(output_path).resolve() if output_path else None
        if output is not None and not output.is_dir():
            raise RuntimeError("MP3 保存目录不存在，请重新选择或恢复默认。")
        return output

    def import_segment(_event):
        limits = selected_range()
        if not preview.opened or limits is None:
            status("请先标记 A 和 B。")
            return
        try:
            path = save_preview_segment(preview, limits, mp3_output())
            pool, folder = music_destination(resolve_app)
            added = import_audio(pool, folder, path)
            status(("已导入片段 · " if added else "片段已在媒体池 · ") + path.name)
        except Exception as exc:
            status("片段导入失败 · {}".format(exc))
            record_errors([traceback.format_exc()])

    def stop_preview(_event):
        try:
            stop_playback()
            status("试听已停止。")
        except (OSError, RuntimeError) as exc:
            status("无法停止试听 · {}".format(exc))

    def import_selected(_event):
        try:
            selected = [path for row, path in rows if row.Selected]
            if not selected:
                status("请先在列表中选中要导入的配乐。")
                return
            if selected_folder() != listed_folder:
                status("下载目录已更改，请先点「刷新」。")
                return
            output = mp3_output()
            stop_playback()
        except Exception as exc:
            status("导入失败 · {}".format(exc))
            record_errors([traceback.format_exc()])
            return

        imported = skipped = 0
        first_failure = None
        failures = []
        destination = None
        for source in selected:
            try:
                if not source.is_file() or not source.stat().st_size:
                    raise RuntimeError("音频文件不可用，请刷新列表。")
                path = source
                if source.suffix.lower() == ".ncm":
                    path = convert_ncm(source, output)
                elif source.suffix.lower() == ".mp3" and output is not None:
                    path = copy_mp3(source, output)
                if destination is None:
                    destination = music_destination(resolve_app)
                pool, folder = destination
                added = import_audio(pool, folder, path)
                imported_paths.add(source)
                imported += int(added)
                skipped += int(not added)
            except Exception as exc:
                if first_failure is None:
                    first_failure = "{} · {}".format(source.name, exc)
                failures.append("{}\n{}".format(source, traceback.format_exc()))

        message = "导入 {} 首，已存在 {} 首，失败 {} 首。".format(imported, skipped, len(failures))
        if destination is not None:
            message = "素材箱「{}」  {}".format(MUSIC_BIN_NAME, message)
        if failures:
            message += "\n首个错误 · " + first_failure
            record_errors(failures)
        render_rows()
        status(message)

    def record_errors(errors):
        try:
            SETTINGS.with_name("last-import-error.txt").write_text(
                "\n\n".join(errors), encoding="utf-8"
            )
        except OSError:
            pass

    def close(_event):
        nonlocal closing
        if closing:
            return
        closing = True
        try:
            window.Hide()
        finally:
            dispatcher.ExitLoop()

    window.On[window_id].Close = close
    window.On[window_id].Hide = close
    window.On[window_id].KeyPress = key_pressed
    window.On[window_id].Resize = player_resized
    window.On["Browse"].Clicked = browse
    window.On["BrowseMP3"].Clicked = browse_mp3
    window.On["ResetMP3"].Clicked = lambda _event: set_mp3_folder("")
    window.On["Refresh"].Clicked = refresh
    window.On["Import"].Clicked = import_selected
    window.On["Preview"].Clicked = preview_selected
    window.On["LoadWaveform"].Clicked = load_waveform
    window.On["Files"].ItemClicked = focus_preview_panel
    window.On["Files"].ItemDoubleClicked = preview_double_clicked
    window.On["PausePreview"].Clicked = toggle_preview
    window.On["StopPreview"].Clicked = stop_preview
    window.On["Seek"].ValueChanged = seek_changed
    window.On["Seek"].SliderPressed = begin_drag
    window.On["Seek"].SliderMoved = move_drag
    window.On["Seek"].SliderReleased = end_drag
    window.On["Seek"].ActionTriggered = waveform_action
    window.On["Back5"].Clicked = lambda _event: skip_preview(-5000)
    window.On["Ahead5"].Clicked = lambda _event: skip_preview(5000)
    window.On["Previous"].Clicked = lambda _event: move_track(-1)
    window.On["Next"].Clicked = lambda _event: move_track(1)
    window.On["Search"].TextChanged = render_rows
    window.On["NewOnly"].Toggled = render_rows
    window.On["Volume"].ValueChanged = volume_changed
    window.On["SetA"].Clicked = lambda _event: set_marker(0)
    window.On["SetB"].Clicked = lambda _event: set_marker(1)
    window.On["ClearAB"].Clicked = clear_markers
    window.On["LoopAB"].Toggled = loop_toggled
    window.On["ImportAB"].Clicked = import_segment
    window.On["OpenClient"].Clicked = open_client
    window.On["ClockPulse"].Clicked = preview_clock
    items["ClockPulse"].Hide()
    refresh()
    update_player_state()
    try:
        duration_worker.start()
        loader.worker.start()
        window.Show()
        center_content()
        show_idle_frame()
        focus_preview_panel()
        remove_shortcut = install_preview_shortcut(user32, lambda: panel_hwnd, space_allowed, space_requests)
        clock_worker.start()
        dispatcher.RunLoop()
    finally:
        closing = True
        if remove_shortcut is not None:
            remove_shortcut()
        end_worker.set()
        duration_jobs.put(None)
        try:
            window.Hide()
        finally:
            try:
                loader.close()
            finally:
                try:
                    preview.stop()
                finally:
                    artwork.cleanup()


if __name__ == "__main__" or globals().get("bmd"):
    show_panel(resolve, fusion, bmd)
