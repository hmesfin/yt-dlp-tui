"""Preset definitions. Data only — no command assembly, no I/O beyond reading TOML."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from yt_dlp_tui import config

PLAYLIST_TEMPLATE = "%(playlist)s/%(playlist_index)03d - %(title)s.%(ext)s"
SINGLE_TEMPLATE = "%(title)s.%(ext)s"

VIDEO_SELECTOR = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b"
AUDIO_ARGS = ("-x", "--audio-format", "m4a", "--embed-thumbnail", "--embed-metadata")


@dataclass(frozen=True)
class Preset:
  id: str
  name: str
  args: tuple[str, ...]
  output_template: str = SINGLE_TEMPLATE
  is_playlist: bool = False
  needs_ffmpeg: bool = False


BUILTIN_PRESETS: tuple[Preset, ...] = (
  Preset(
    id="video-mp4",
    name="Best video (mp4, plays everywhere)",
    args=("-f", VIDEO_SELECTOR, "--merge-output-format", "mp4"),
    needs_ffmpeg=True,
  ),
  Preset(
    id="audio-m4a",
    name="Audio only  →  m4a + cover + tags",
    args=AUDIO_ARGS,
    needs_ffmpeg=True,
  ),
  Preset(
    id="playlist-video",
    name="Playlist  →  video, archived",
    args=("-f", VIDEO_SELECTOR, "--merge-output-format", "mp4", "--no-overwrites"),
    output_template=PLAYLIST_TEMPLATE,
    is_playlist=True,
    needs_ffmpeg=True,
  ),
  Preset(
    id="playlist-audio",
    name="Playlist  →  audio, archived",
    args=AUDIO_ARGS + ("--no-overwrites",),
    output_template=PLAYLIST_TEMPLATE,
    is_playlist=True,
    needs_ffmpeg=True,
  ),
  Preset(
    id="data-saver",
    name="Data saver  (720p cap)",
    args=("-f", "bv*[height<=720]+ba/b[height<=720]", "--merge-output-format", "mp4"),
    needs_ffmpeg=True,
  ),
)


def _preset_from_table(table: dict) -> Preset:
  return Preset(
    id=str(table["id"]),
    name=str(table["name"]),
    args=tuple(str(a) for a in table["args"]),
    output_template=str(table.get("output_template", SINGLE_TEMPLATE)),
    is_playlist=bool(table.get("is_playlist", False)),
    needs_ffmpeg=bool(table.get("needs_ffmpeg", False)),
  )


def load_presets(path: Path | None = None) -> tuple[Preset, ...]:
  """Built-ins, with same-id user presets replacing them and new ones appended."""
  path = path or config.config_path()
  if not path.exists():
    return BUILTIN_PRESETS
  data = tomllib.loads(path.read_text())
  overrides = {t["id"]: _preset_from_table(t) for t in data.get("preset", [])}
  merged = [overrides.pop(p.id, p) for p in BUILTIN_PRESETS]
  merged.extend(overrides.values())
  return tuple(merged)


def order_for(presets: tuple[Preset, ...], is_playlist: bool) -> tuple[Preset, ...]:
  """Sort playlist-suited presets first when the URL is a playlist. Stable otherwise."""
  if not is_playlist:
    return presets
  return tuple(sorted(presets, key=lambda p: not p.is_playlist))
