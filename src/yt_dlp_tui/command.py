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
  """Replace the value of `flag` in place. Returns args unchanged if absent.

  Also unchanged when `flag` is the last element: a preset read from the
  user's config.toml can end in a bare `-f`, and an unguarded
  `out.index(flag) + 1` raises IndexError out of a function the UI calls on
  every keystroke. No built-in preset has that shape, which is why this went
  untriggered.
  """
  out = list(args)
  if flag not in out:
    return out
  index = out.index(flag)
  if index + 1 == len(out):
    return out
  out[index + 1] = value
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
  """Assemble the argv for one download.

  Two overrides only *edit* a flag the preset already has, and are therefore
  silent no-ops on a preset that does not: `height_cap` needs a `-f` to
  rewrite, and `audio_format` needs an `--audio-format`. Every built-in preset
  carries the one its kind needs, so this only reaches a preset written by
  hand in config.toml -- documented in the README's config section rather than
  warned about at runtime, because this module is pure and has nothing to warn
  through.
  """
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


# The pure-plumbing tokens `build_command` always emits, in the exact order it
# emits them, immediately after the binary. Four flags, two of which carry a
# value.
#
# A *block* and not a set of flag names, because membership is not the
# question: `--no-colors` is machinery when it is the second token of a
# command this module built, and it is the user's own flag when it arrives in
# `extra_args` from the advanced drawer -- the token is identical either way.
# Filtering by name reached into `extra_args` and mangled it: a user's
# `--postprocessor-args --no-colors` rendered as `--postprocessor-args` with
# its value silently gone, and `--progress-template MINE:{}` disappeared
# outright, so the command on screen was not the command being run.
MACHINERY_BLOCK: tuple[str, ...] = (
  "--newline",
  "--no-colors",
  "--progress-template",
  PROGRESS_TEMPLATE,
  "--progress-template",
  POSTPROCESS_TEMPLATE,
)
# Flags, not tokens: the two template payloads are values, not flags, and the
# preview's "+N hidden" label counts flags.
MACHINERY_FLAG_COUNT = 4


def elide_machinery(cmd: list[str]) -> tuple[list[str], int]:
  """Split a real argv (as returned by `build_command`) into the flags worth
  showing and a count of how many machinery flags were dropped.

  This is a filter over the exact argv passed in -- it never re-derives or
  reassembles a command of its own, so the elided view can never drift from
  what `build_command` actually produced.

  An argv that does not carry `MACHINERY_BLOCK` verbatim at the position
  `build_command` puts it is returned untouched, with a count of zero. That is
  the safe direction to fail: showing four flags that could have been hidden
  costs a line of screen, whereas hiding a token that is really running breaks
  the invariant the preview exists for.
  """
  end = 1 + len(MACHINERY_BLOCK)
  if tuple(cmd[1:end]) != MACHINERY_BLOCK:
    return list(cmd), 0
  return [*cmd[:1], *cmd[end:]], MACHINERY_FLAG_COUNT
