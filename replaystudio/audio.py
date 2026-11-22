from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from array import array
from bisect import bisect_left, bisect_right
from itertools import accumulate
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices, QtAudio

from .model import FPS, MediaClip, Project, ReplayError
from .storage import asset_path
from .voice import render_ut_pcm

SAMPLE_RATE = 48_000
SAMPLES_PER_FRAME = SAMPLE_RATE // FPS
SAMPLE_BYTES = 4
DUCK_RAMP_SAMPLES = SAMPLE_RATE * 12 // 100
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class PreparationCancelled(Exception):
    pass


def find_ffmpeg() -> str:
    candidates = []
    if override := os.environ.get("REPLAY_FFMPEG"):
        candidates.append(Path(override))
    if bundled := getattr(sys, "_MEIPASS", None):
        candidates.append(Path(bundled) / "bin" / "ffmpeg.exe")
    for path in candidates:
        if path.is_file():
            return str(path)
    if executable := shutil.which("ffmpeg"):
        return executable
    raise ReplayError("找不到 FFmpeg。请使用自带编码器的便携版，或将 FFmpeg 加入 PATH。")


class AudioCache:
    def __init__(self, project: Project, directory: Path):
        self.project = project
        self.directory = directory
        self.cache = directory / "audio-cache"

    def cached(self, reference: str) -> Path | None:
        output = self.cache / (reference + ".pcm")
        return output if output.is_file() and output.stat().st_size >= SAMPLE_BYTES else None

    def decoded(self, reference: str, run=None, stop=None) -> Path:
        if stop is not None and stop.is_set():
            raise PreparationCancelled()
        if ready := self.cached(reference):
            return ready
        asset = self.project.assets[reference]
        output = self.cache / (reference + ".pcm")
        self.cache.mkdir(exist_ok=True)
        source = asset_path(asset, self.directory)
        with tempfile.NamedTemporaryFile(dir=self.cache, suffix=".pcm", delete=False) as temporary:
            staged = Path(temporary.name)
        try:
            args = ["-nostdin", "-i", str(source), "-map", "0:a:0", "-vn",
                    "-ar", str(SAMPLE_RATE), "-ac", "2",
                    "-c:a", "pcm_s16le", "-f", "s16le", str(staged)]
            if run is None:
                with subprocess.Popen(
                    [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
                ) as process:
                    while process.poll() is None:
                        if stop is not None and stop.is_set():
                            process.kill()
                            raise PreparationCancelled()
                        try:
                            process.wait(timeout=.15)
                        except subprocess.TimeoutExpired:
                            continue
                    if process.returncode:
                        raise ReplayError(f"无法解码「{asset.name}」。")
            else:
                run(args)
            if staged.stat().st_size < SAMPLE_BYTES:
                raise ReplayError(f"「{asset.name}」没有可播放的音频。")
            try:
                staged.replace(output)
            except PermissionError:
                if not output.is_file():
                    raise
        finally:
            staged.unlink(missing_ok=True)
        return output

    def frame_length(self, reference: str) -> int:
        samples = self.decoded(reference).stat().st_size // SAMPLE_BYTES
        return (samples + SAMPLES_PER_FRAME - 1) // SAMPLES_PER_FRAME

    def cached_voice(self, span) -> Path | None:
        if span.mode == "ut":
            output = self.cache / f"ut-{span.ut_key}.pcm"
            return output if output.is_file() and output.stat().st_size >= SAMPLE_BYTES else None
        return self.cached(span.asset)

    def decoded_voice(self, span, run=None, stop=None) -> Path:
        if stop is not None and stop.is_set():
            raise PreparationCancelled()
        if span.mode != "ut":
            return self.decoded(span.asset, run=run, stop=stop)
        if ready := self.cached_voice(span):
            return ready
        custom_pcm = None
        if span.role_voice and span.role_voice.ut_preset == "custom" and span.role_voice.ut_asset in self.project.assets:
            custom_pcm = self.decoded(span.role_voice.ut_asset, run=run, stop=stop)
        pcm = render_ut_pcm(span.text, span.role_voice, span.length, custom_pcm)
        self.cache.mkdir(exist_ok=True)
        output = self.cache / f"ut-{span.ut_key}.pcm"
        with tempfile.NamedTemporaryFile(dir=self.cache, suffix=".pcm", delete=False) as temporary:
            staged = Path(temporary.name)
            temporary.write(pcm)
        try:
            try:
                staged.replace(output)
            except PermissionError:
                if not output.is_file():
                    raise
        finally:
            staged.unlink(missing_ok=True)
        return output


def voice_as_media_clip(span) -> MediaClip:
    return MediaClip(
        id=span.id,
        track="__voice__",
        asset=span.asset,
        start=span.start,
        length=span.length,
        volume=span.volume,
    )


class AudioMixer:
    def __init__(
        self,
        clips: tuple[MediaClip, ...],
        files: dict[str, Path],
        duck_spans: tuple[tuple[int, int], ...] = (),
        duck_tracks: frozenset[str] = frozenset(),
        duck_gain: float = 1.0,
        ut_spans: dict[str, object] | None = None,
        ut_custom_files: dict[str, Path] | None = None,
    ):
        self.clips = sorted(clips, key=lambda clip: clip.start)
        self.starts = [clip.start * SAMPLES_PER_FRAME for clip in self.clips]
        self.latest_ends = list(accumulate((clip.end * SAMPLES_PER_FRAME for clip in self.clips), max))
        self.files = files
        self.source_sizes = {key: file.stat().st_size // SAMPLE_BYTES for key, file in files.items()}
        self.duck_spans = tuple((s * SAMPLES_PER_FRAME, e * SAMPLES_PER_FRAME) for s, e in duck_spans)
        self.duck_tracks = duck_tracks
        self.duck_gain = max(0.0, min(1.0, duck_gain))
        self.ut_spans = ut_spans or {}
        self.ut_custom_files = ut_custom_files or {}
        self._ut_mem_cache: dict[str, bytes] = {}

    def _read_ut_clip(self, clip: MediaClip, offset: int, samples: int) -> bytes:
        span = self.ut_spans.get(clip.asset)
        if span is None:
            return b""
        pcm = self._ut_mem_cache.get(clip.asset)
        if pcm is None:
            custom_path = self.ut_custom_files.get(span.role_voice.ut_asset) if span.role_voice else None
            pcm = render_ut_pcm(span.text, span.role_voice, span.length, custom_path)
            if len(self._ut_mem_cache) >= 32:
                self._ut_mem_cache.clear()
            self._ut_mem_cache[clip.asset] = pcm
        start_b = offset * SAMPLE_BYTES
        if start_b >= len(pcm):
            return b""
        return pcm[start_b:start_b + samples * SAMPLE_BYTES]

    def _duck_factor(self, sample_pos: int) -> float:
        factor = 1.0
        for left, right in self.duck_spans:
            if sample_pos < left - DUCK_RAMP_SAMPLES:
                break
            if sample_pos > right + DUCK_RAMP_SAMPLES:
                continue
            if left <= sample_pos <= right:
                return self.duck_gain
            if sample_pos < left:
                t = (sample_pos - (left - DUCK_RAMP_SAMPLES)) / DUCK_RAMP_SAMPLES
                factor = min(factor, 1.0 - (1.0 - self.duck_gain) * t)
            else:
                t = (sample_pos - right) / DUCK_RAMP_SAMPLES
                factor = min(factor, self.duck_gain + (1.0 - self.duck_gain) * t)
        return factor

    def _overlaps_duck(self, left: int, right: int) -> bool:
        if not self.duck_spans or self.duck_gain >= 0.999:
            return False
        for d_left, d_right in self.duck_spans:
            if d_left - DUCK_RAMP_SAMPLES < right and left < d_right + DUCK_RAMP_SAMPLES:
                return True
        return False

    def _read_clip(self, clip: MediaClip, offset: int, samples: int) -> bytes:
        if clip.asset in self.ut_spans:
            return self._read_ut_clip(clip, offset, samples)
        source_size = self.source_sizes[clip.asset]
        if not source_size:
            return b""
        if clip.loop:
            offset %= source_size
        elif offset >= source_size:
            return b""
        with self.files[clip.asset].open("rb") as stream:
            stream.seek(offset * SAMPLE_BYTES)
            initial = stream.read(samples * SAMPLE_BYTES)
            remaining = samples * SAMPLE_BYTES - len(initial)
            if remaining and clip.loop:
                stream.seek(0)
                loop = stream.read(min(remaining, source_size * SAMPLE_BYTES))
                copies, rest = divmod(remaining, len(loop))
                return initial + loop * copies + loop[:rest]
            return initial

    def read(self, start: int, samples: int) -> bytes:
        stop = start + samples
        first = bisect_right(self.latest_ends, start)
        last = bisect_left(self.starts, stop)
        if first >= last:
            return bytes(samples * SAMPLE_BYTES)
        mixed = array("i", [0]) * (samples * 2)
        for clip in self.clips[first:last]:
            start_sample = clip.start * SAMPLES_PER_FRAME
            left = max(start, start_sample)
            right = min(stop, clip.end * SAMPLES_PER_FRAME)
            if left >= right:
                continue
            source_offset = clip.source_in * SAMPLES_PER_FRAME + left - start_sample
            raw = self._read_clip(clip, source_offset, right - left)
            needs_duck = clip.track in self.duck_tracks and self._overlaps_duck(left, right)
            if first + 1 == last and clip.volume == 100 and not needs_duck:
                return (bytes((left - start) * SAMPLE_BYTES) + raw
                        + bytes((stop - left) * SAMPLE_BYTES - len(raw)))
            sound = array("h")
            sound.frombytes(raw)
            gain = clip.volume / 100
            base_idx = (left - start) * 2
            if not needs_duck:
                for i, value in enumerate(sound, base_idx):
                    mixed[i] += round(value * gain)
            else:
                step = 64
                frame_count = len(sound) // 2
                for pos in range(0, frame_count, step):
                    block_end = min(frame_count, pos + step)
                    d = self._duck_factor(left + (pos + block_end) // 2)
                    eff = gain * d
                    for k in range(pos * 2, block_end * 2):
                        mixed[base_idx + k] += round(sound[k] * eff)
        return array("h", (max(-32768, min(32767, value)) for value in mixed)).tobytes()


class PreviewAudio(QObject):
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cache: AudioCache | None = None
        self.sink: QAudioSink | None = None
        self.device = None
        self.mixer: AudioMixer | None = None
        self.first = self.cursor = self.end = 0
        self.pending = b""
        self._audition_sink: QAudioSink | None = None
        self._audition_buffer: QBuffer | None = None
        self._audition_timer = QTimer(self)
        self._audition_timer.setSingleShot(True)
        self._audition_timer.timeout.connect(self.stop_audition)

    def bind(self, cache: AudioCache | None) -> None:
        self.stop()
        self.stop_audition()
        self.cache = cache

    def stop(self) -> None:
        if self.sink:
            self.sink.reset()
            self.sink.deleteLater()
        self.sink = self.device = self.mixer = None
        self.pending = b""

    def stop_audition(self) -> None:
        self._audition_timer.stop()
        if self._audition_sink:
            self._audition_sink.reset()
            self._audition_sink.deleteLater()
        if self._audition_buffer:
            self._audition_buffer.close()
            self._audition_buffer.deleteLater()
        self._audition_sink = None
        self._audition_buffer = None

    def play_pcm(self, pcm_bytes: bytes) -> None:
        self.stop_audition()
        if not pcm_bytes:
            return
        output = QMediaDevices.defaultAudioOutput()
        if output.isNull():
            self.error.emit("找不到可用的音频输出设备。")
            return
        pcm = QAudioFormat()
        pcm.setSampleRate(SAMPLE_RATE)
        pcm.setChannelCount(2)
        pcm.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self._audition_sink = QAudioSink(output, pcm, self)
        self._audition_buffer = QBuffer(self)
        self._audition_buffer.setData(pcm_bytes)
        self._audition_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self._audition_sink.start(self._audition_buffer)
        duration_ms = max(250, int(len(pcm_bytes) / (SAMPLE_RATE * SAMPLE_BYTES) * 1000) + 250)
        self._audition_timer.start(duration_ms)

    def play(
        self,
        clips: tuple[MediaClip, ...],
        start: int,
        end: int,
        extra_files: dict[str, Path] | None = None,
        duck_spans: tuple[tuple[int, int], ...] = (),
        duck_tracks: frozenset[str] = frozenset(),
        duck_gain: float = 1.0,
        ut_spans: dict[str, object] | None = None,
        ut_custom_files: dict[str, Path] | None = None,
    ) -> None:
        self.stop()
        self.stop_audition()
        audible = tuple(clip for clip in clips if clip.start < end and start < clip.end)
        if not audible or self.cache is None:
            return
        output = QMediaDevices.defaultAudioOutput()
        if output.isNull():
            self.error.emit("找不到可用的音频输出设备。")
            return
        try:
            files = dict(extra_files or {})
            ut_map = ut_spans or {}
            for reference in dict.fromkeys(clip.asset for clip in audible):
                if reference in files or reference in ut_map:
                    continue
                path = self.cache.cached(reference)
                if path is None:
                    raise ReplayError(f"「{self.cache.project.assets[reference].name}」的预览音频尚未就绪。")
                files[reference] = path
            self.mixer = AudioMixer(
                audible, files,
                duck_spans=duck_spans, duck_tracks=duck_tracks, duck_gain=duck_gain,
                ut_spans=ut_map, ut_custom_files=ut_custom_files,
            )
        except (OSError, ReplayError) as error:
            self.error.emit(str(error))
            return
        pcm = QAudioFormat()
        pcm.setSampleRate(SAMPLE_RATE)
        pcm.setChannelCount(2)
        pcm.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self.sink = QAudioSink(output, pcm, self)
        self.sink.setBufferSize(SAMPLE_RATE * SAMPLE_BYTES // 5)
        self.first = start
        self.cursor = start * SAMPLES_PER_FRAME
        self.end = end * SAMPLES_PER_FRAME
        self.device = self.sink.start()
        self.step()

    def step(self) -> int | None:
        if self.sink is None:
            return None
        if self.device is None or self.sink.state() == QtAudio.State.StoppedState:
            self.stop()
            self.error.emit("音频设备暂停服务，请检查 Windows 声音输出。")
            return None
        try:
            free = self.sink.bytesFree()
            while free >= SAMPLE_BYTES and (self.pending or self.cursor < self.end):
                if not self.pending:
                    samples = min(free // SAMPLE_BYTES, 8192, self.end - self.cursor)
                    self.pending = self.mixer.read(self.cursor, samples)
                    self.cursor += samples
                written = self.device.write(self.pending[:free])
                if written < 0:
                    raise ReplayError("无法向音频设备写入声音。")
                if not written:
                    break
                self.pending = self.pending[written:]
                free -= written
        except (OSError, ReplayError) as error:
            self.stop()
            self.error.emit(str(error))
            return None
        if self.cursor == self.end and not self.pending and self.sink.state() == QtAudio.State.IdleState:
            return self.end // SAMPLES_PER_FRAME
        return self.first + self.sink.processedUSecs() * FPS // 1_000_000
