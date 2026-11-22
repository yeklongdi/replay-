from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from threading import Event, Lock
from typing import Callable

from .audio import (
    AudioCache, AudioMixer, SAMPLE_BYTES, SAMPLE_RATE, SAMPLES_PER_FRAME, find_ffmpeg, voice_as_media_clip,
)
from .model import FPS, Project, ReplayError
from .render import Images, Timeline, render_image

CHUNK_SAMPLES = (512 * 1024) // SAMPLE_BYTES


class ExportCancelled(Exception):
    pass


class ExportJob:
    def __init__(self, project: Project, timeline: Timeline, media: Path, start: int,
                 end: int, output: Path, report: Callable[[int, str], None]):
        if not 0 <= start < end <= timeline.length:
            raise ReplayError("请先选定可播放的导出范围。")
        self.project = project
        self.visuals = timeline.visuals_between(start, end)
        self.clips = tuple(clip for clip in timeline.media.audio if clip.start < end and start < clip.end)
        self.voices = tuple(span for span in timeline.voices if span.start < end and start < span.end)
        self.duck_spans = timeline.duck_spans
        self.duck_tracks = frozenset(
            track.id for track in project.media.tracks if track.kind == "music"
        ) if project.media is not None else frozenset({"music"})
        self.duck_gain = project.voice.duck_level / 100 if project.voice.duck_music else 1.0
        self.media = media
        self.start = start
        self.end = end
        self.output = output
        self.report = report
        self.ffmpeg = find_ffmpeg()
        self._stop = Event()
        self._guard = Lock()
        self._process: subprocess.Popen | None = None
        self._last_percent = -1

    def cancel(self) -> None:
        with self._guard:
            self._stop.set()
            if self._process is not None and self._process.poll() is None:
                self._process.kill()

    def _check(self) -> None:
        if self._stop.is_set():
            raise ExportCancelled()

    def _progress(self, percent: int, message: str) -> None:
        if percent > self._last_percent:
            self._last_percent = percent
            self.report(percent, message)

    def _spawn(self, arguments: list[str], error_file, **options) -> subprocess.Popen:
        with self._guard:
            self._check()
            self._process = subprocess.Popen(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *arguments],
                stderr=error_file, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                **options,
            )
            return self._process

    def _finish(self, process: subprocess.Popen, error_file) -> None:
        code = process.wait()
        with self._guard:
            self._process = None
            self._check()
        if code:
            error_file.seek(0)
            detail = error_file.read().decode("utf-8", errors="replace").strip()
            raise ReplayError(f"FFmpeg 导出失败。\n{detail[-1600:]}" if detail else f"FFmpeg 退出码 {code}。")

    def _render_frames(self, work: Path) -> Path:
        images = Images(self.media)
        lines = ["ffconcat version 1.0"]
        last_filename = ""
        previous_us = 0
        for index, span in enumerate(self.visuals):
            self._check()
            filename = f"frame-{index:07}.png"
            picture = render_image(self.project, span.page, images, span.background)
            if not picture.save(str(work / filename), "PNG", 70):
                raise ReplayError("写入临时画面失败。请检查磁盘空间。")
            end_frame = span.end - self.start
            current_us = round(end_frame * 1_000_000 / FPS)
            seconds = (current_us - previous_us) / 1_000_000
            lines.extend((f"file '{filename}'", f"option framerate {FPS}", f"duration {seconds:.6f}"))
            last_filename, previous_us = filename, current_us
            self._progress(index * 46 // len(self.visuals), f"生成画面 {index + 1:,} / {len(self.visuals):,}")
        lines.extend((f"file '{last_filename}'", f"option framerate {FPS}"))
        playlist = work / "frames.ffconcat"
        playlist.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
        return playlist

    def _decode(self, arguments: list[str]) -> None:
        with tempfile.TemporaryFile() as errors:
            process = self._spawn(arguments, errors, stdout=subprocess.DEVNULL)
            self._finish(process, errors)

    def _render_audio(self, work: Path) -> Path:
        audio = work / "audio.m4a"
        cache = AudioCache(self.project, self.media)
        decoded = {}
        ut_spans = {}
        ut_custom_files = {}
        for reference in dict.fromkeys(clip.asset for clip in self.clips):
            self._check()
            self._progress(46, "读取配乐和音效")
            decoded[reference] = cache.decoded(reference, self._decode)
        for span in self.voices:
            self._check()
            self._progress(46, "准备角色配音")
            if span.mode == "ut":
                ut_spans[span.asset] = span
                if (span.role_voice and span.role_voice.ut_preset == "custom"
                        and span.role_voice.ut_asset in self.project.assets
                        and span.role_voice.ut_asset not in ut_custom_files):
                    ut_custom_files[span.role_voice.ut_asset] = cache.decoded(span.role_voice.ut_asset, self._decode)
            elif span.asset not in decoded:
                decoded[span.asset] = cache.decoded_voice(span, self._decode)
        all_clips = (*self.clips, *(voice_as_media_clip(span) for span in self.voices))
        mixer = AudioMixer(
            all_clips,
            decoded,
            duck_spans=self.duck_spans,
            duck_tracks=self.duck_tracks,
            duck_gain=self.duck_gain,
            ut_spans=ut_spans,
            ut_custom_files=ut_custom_files,
        )
        first, end = self.start * SAMPLES_PER_FRAME, self.end * SAMPLES_PER_FRAME
        with tempfile.TemporaryFile() as errors:
            process = self._spawn([
                "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0",
                "-c:a", "aac", "-b:a", "192k", str(audio),
            ], errors, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            try:
                with process.stdin as sink:
                    for start in range(first, end, CHUNK_SAMPLES):
                        self._check()
                        samples = min(CHUNK_SAMPLES, end - start)
                        sink.write(mixer.read(start, samples))
                        self._progress(47 + (start + samples - first) * 15 // (end - first), "混合音轨")
            except Exception:
                if process.poll() is None:
                    process.kill()
                self._finish(process, errors)
                raise
            self._finish(process, errors)
        return audio

    def _encode(self, playlist: Path, audio: Path, destination: Path) -> None:
        frames = self.end - self.start
        self._progress(63, "编码 MP4")
        with tempfile.TemporaryFile() as errors:
            process = self._spawn([
                "-nostdin", "-nostats", "-stats_period", "0.25", "-progress", "pipe:1",
                "-f", "concat", "-safe", "0", "-i", str(playlist), "-i", str(audio),
                "-map", "0:v:0", "-map", "1:a:0", "-vf", f"fps={FPS}:start_time=0",
                "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "4", "-c:a", "copy",
                "-movflags", "+faststart", str(destination),
            ], errors, stdout=subprocess.PIPE)
            with process.stdout as status:
                for line in status:
                    if not self._stop.is_set() and line.startswith(b"frame="):
                        current = int(line.split(b"=", 1)[1])
                        self._progress(min(99, 63 + current * 36 // frames), "编码 MP4")
            self._finish(process, errors)

    def run(self) -> None:
        self._check()
        with tempfile.NamedTemporaryFile(
            dir=self.output.parent, prefix=".replay-", suffix=".mp4", delete=False,
        ) as output:
            staged = Path(output.name)
        try:
            with tempfile.TemporaryDirectory(prefix="replay-export-") as directory:
                work = Path(directory)
                playlist = self._render_frames(work)
                audio = self._render_audio(work)
                self._encode(playlist, audio, staged)
            with self._guard:
                self._check()
                staged.replace(self.output)
            self._progress(100, "导出完成")
        finally:
            staged.unlink(missing_ok=True)
