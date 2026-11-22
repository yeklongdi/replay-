from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field, replace
from heapq import heappop, heappush
from itertools import groupby, pairwise
from typing import TYPE_CHECKING

from .model import DEFAULT_SCENE_NAME, MAX_MEDIA_FRAME, MediaBookmark, MediaClip, MediaEdit, Project, ReplayError

if TYPE_CHECKING:
    from .render import Page


class BackgroundOverlap(ReplayError):
    pass


@dataclass(frozen=True, slots=True)
class BackgroundSpan:
    start: int
    end: int
    asset: str


@dataclass(slots=True)
class MediaSchedule:
    clips: dict[str, MediaClip] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)
    audio: tuple[MediaClip, ...] = ()
    backgrounds: list[BackgroundSpan] = field(default_factory=list)
    conflicts: list[tuple[int, int]] = field(default_factory=list)
    conflicting_clips: set[str] = field(default_factory=set)
    length: int = 0
    _background_starts: list[int] = field(default_factory=list)

    def clip(self, clip_id: str) -> MediaClip:
        return self.clips[clip_id]

    def background_at(self, frame: int) -> str:
        i = bisect_right(self._background_starts, frame) - 1
        if i >= 0 and frame < self.backgrounds[i].end:
            return self.backgrounds[i].asset
        return ""

    def conflicts_between(self, start: int, end: int) -> bool:
        return any(left < end and start < right for left, right in self.conflicts)


def clip_position(clip: MediaClip, cue_starts: dict[str, int]) -> tuple[int, bool]:
    if not clip.anchor:
        return clip.start, False
    if clip.anchor not in cue_starts:
        return clip.start, True
    return max(0, min(MAX_MEDIA_FRAME - clip.length, cue_starts[clip.anchor] + clip.offset)), False


def validate_background(edit: MediaEdit, clip: MediaClip, cue_starts: dict[str, int],
                        schedule: MediaSchedule) -> None:
    track = next(track for track in edit.tracks if track.id == clip.track)
    if track.kind != "background":
        return
    start, pending = clip_position(clip, cue_starts)
    if not pending and any(
        item.track == clip.track and item.id != clip.id and item.id not in schedule.pending
        and item.start < start + clip.length and start < item.end
        for item in schedule.clips.values()
    ):
        raise BackgroundOverlap("这段背景与已有图片重叠，请调整位置或时长。")


def resolve_media(edit: MediaEdit, cue_starts: dict[str, int], text_end: int) -> MediaSchedule:
    schedule = MediaSchedule(length=text_end)
    kinds = {track.id: track.kind for track in edit.tracks}
    audio = []
    starting, ending = {}, {}
    for order, original in enumerate(edit.clips):
        start, pending = clip_position(original, cue_starts)
        clip = replace(original, start=start)
        schedule.clips[clip.id] = clip
        if pending:
            schedule.pending.add(clip.id)
            continue
        schedule.length = max(schedule.length, clip.end)
        if kinds[clip.track] == "background":
            rank = (bool(clip.anchor), order, clip.id)
            starting.setdefault(clip.start, []).append(rank)
            ending.setdefault(clip.end, []).append(clip.id)
        elif clip.volume:
            audio.append(clip)
    schedule.audio = tuple(sorted(audio, key=lambda item: item.start))

    active = set()
    queue = []
    for left, right in pairwise(sorted(starting.keys() | ending.keys())):
        active.difference_update(ending.get(left, ()))
        for rank in starting.get(left, ()):
            active.add(rank[2])
            heappush(queue, rank)
        while queue and queue[0][2] not in active:
            heappop(queue)
        if not queue:
            continue
        if len(active) > 1:
            schedule.conflicts.append((left, right))
            schedule.conflicting_clips.update(active)
        asset = schedule.clips[queue[0][2]].asset
        if schedule.backgrounds and (last := schedule.backgrounds[-1]).end == left and last.asset == asset:
            schedule.backgrounds[-1] = BackgroundSpan(last.start, right, asset)
        else:
            schedule.backgrounds.append(BackgroundSpan(left, right, asset))
    schedule._background_starts = [span.start for span in schedule.backgrounds]
    return schedule


def legacy_media(project: Project, pages: list[Page], cue_frames: dict[str, int]) -> MediaEdit:
    edit = MediaEdit()
    first = project.scenes[project.default_scene]
    if first.name != DEFAULT_SCENE_NAME:
        edit.bookmarks.append(MediaBookmark(f"initial-{first.id}", first.name, 0))
    edit.library.extend(dict.fromkeys(
        reference for scene in project.scenes.values()
        for reference in (scene.background, scene.music) if reference
    ))
    for (scene_id, start), group in groupby(pages, key=lambda page: (page.scene, page.scene_start)):
        scene = project.scenes[scene_id]
        last = list(group)[-1]
        for track, reference in (("background", scene.background), ("music", scene.music)):
            if reference:
                edit.clips.append(MediaClip(
                    f"{track}-{scene_id}-{start}", track, reference, start, last.end - start,
                    loop=track == "music", volume=scene.volume,
                ))
    for cue in project.cues:
        if cue.kind == "scene" and cue.id in cue_frames:
            edit.bookmarks.append(MediaBookmark(cue.id, project.scenes[cue.scene].name, cue_frames[cue.id]))
    return edit
