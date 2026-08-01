"""Pure argv assembly. No I/O, no subprocess, no globals.

All yt-dlp flag knowledge lives here so it can be tested exhaustively offline.
"""

from dataclasses import dataclass, field
from pathlib import Path

from yt_dlp_tui import config
from yt_dlp_tui.presets import Preset

PROGRESS_PREFIX = "PROG:"
POSTPROCESS_PREFIX = "PP:"

# Every field carries a |default: without one, missing values render as bare
# `NA`, which is invalid JSON and throws on the final progress line.
#
# String fields need a *quoted* default (|"" not |): yt-dlp's create_key()
# substitutes the default as a raw literal and skips the json ('j') conversion
# entirely when the value is None (see YoutubeDL.py, `if value is None:
# value, fmt = default, 's'` runs before the `elif fmt[-1] == 'j'` branch). A
# bare `|` default renders as `"title":` with nothing after the colon, which
# is invalid JSON. Numeric `|0` defaults are safe only because a bare `0` is
# itself valid JSON.
PROGRESS_TEMPLATE = (
  PROGRESS_PREFIX + "{"
  '"b":%(progress.downloaded_bytes|0)j,'
  '"t":%(progress.total_bytes,progress.total_bytes_estimate|0)j,'
  '"s":%(progress.speed|0)j,'
  '"e":%(progress.eta|0)j,'
  '"i":%(info.playlist_index|0)j,'
  '"n":%(info.n_entries|0)j,'
  '"title":%(info.title|"")j'
  "}"
)
POSTPROCESS_TEMPLATE = (
  "postprocess:" + POSTPROCESS_PREFIX + "{"
  '"st":%(progress.status|"")j,'
  '"pp":%(progress.postprocessor|"")j'
  "}"
)


@dataclass(frozen=True)
class Overrides:
  output_dir: Path | None = None
  height_cap: int | None = None
  audio_format: str | None = None
  extra_args: tuple[str, ...] = field(default_factory=tuple)


def _replace_flag(args: list[str], flag: str, value: str) -> list[str]:
  """Replace the value of `flag` in place. Returns args unchanged if absent."""
  out = list(args)
  if flag in out:
    out[out.index(flag) + 1] = value
  return out


def build_command(
  url: str,
  preset: Preset,
  overrides: Overrides | None = None,
  *,
  ytdlp: str = "yt-dlp",
  download_dir: Path | None = None,
  archive: Path | None = None,
) -> list[str]:
  overrides = overrides or Overrides()
  dest = overrides.output_dir or download_dir or config.default_download_dir()

  args = list(preset.args)
  is_audio = "-x" in args

  if overrides.height_cap is not None and not is_audio:
    selector = f"bv*[height<={overrides.height_cap}]+ba/b[height<={overrides.height_cap}]"
    args = _replace_flag(args, "-f", selector)
  if overrides.audio_format is not None and is_audio:
    args = _replace_flag(args, "--audio-format", overrides.audio_format)

  cmd: list[str] = [ytdlp, "--newline", "--no-colors"]
  cmd += ["--progress-template", PROGRESS_TEMPLATE]
  cmd += ["--progress-template", POSTPROCESS_TEMPLATE]
  cmd += args
  cmd += ["-o", str(dest / preset.output_template)]

  if preset.is_playlist:
    cmd += ["--download-archive", str(archive or config.archive_path())]

  cmd += list(overrides.extra_args)
  cmd += ["--", url]  # `--` so a URL beginning with `-` is never parsed as a flag
  return cmd


# The pure-plumbing flags `build_command` always adds, named exactly as the
# spec fixes them: `--newline`, `--no-colors`, and `--progress-template`
# (which appears twice, once per template above). Four flags on the default
# preset. This is a name-based allowlist, not a pattern over flag *values* --
# a `--progress-template` payload is packed with punctuation that could look
# like almost anything, and matching on its contents instead of the flag
# name in front of it is exactly how a filter goes from "hides four known
# flags" to "hides whatever happens to look machine-generated", silently
# swallowing legitimate `extra_args` a user adds through the advanced drawer.
MACHINERY_FLAGS = frozenset({"--newline", "--no-colors", "--progress-template"})
_FLAGS_TAKING_VALUE = frozenset({"--progress-template"})


def elide_machinery(cmd: list[str]) -> tuple[list[str], int]:
  """Split a real argv (as returned by `build_command`) into the flags worth
  showing and a count of how many machinery flags were dropped.

  This is a filter over the exact argv passed in -- it never re-derives or
  reassembles a command of its own, so the elided view can never drift from
  what `build_command` actually produced.
  """
  filtered: list[str] = []
  hidden = 0
  skip_next = False
  for token in cmd:
    if skip_next:
      skip_next = False
      continue
    if token in MACHINERY_FLAGS:
      hidden += 1
      skip_next = token in _FLAGS_TAKING_VALUE
      continue
    filtered.append(token)
  return filtered, hidden
