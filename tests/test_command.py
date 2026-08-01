from pathlib import Path

import pytest

from yt_dlp_tui.command import (
  MACHINERY_FLAGS,
  POSTPROCESS_PREFIX,
  PROGRESS_PREFIX,
  Overrides,
  build_command,
  elide_machinery,
)
from yt_dlp_tui.presets import BUILTIN_PRESETS, Preset

VIDEO = next(p for p in BUILTIN_PRESETS if p.id == "video-mp4")
AUDIO = next(p for p in BUILTIN_PRESETS if p.id == "audio-m4a")
PL_VIDEO = next(p for p in BUILTIN_PRESETS if p.id == "playlist-video")
URL = "https://youtube.com/watch?v=abc"
DEST = Path("/tmp/dl")


def test_url_is_last_and_preceded_by_double_dash():
  cmd = build_command(URL, VIDEO, download_dir=DEST)
  assert cmd[-1] == URL
  assert cmd[-2] == "--"


def test_url_starting_with_dash_cannot_be_read_as_a_flag():
  cmd = build_command("-rf", VIDEO, download_dir=DEST)
  assert cmd[-2:] == ["--", "-rf"]


def test_preset_args_are_present():
  cmd = build_command(URL, VIDEO, download_dir=DEST)
  assert "--merge-output-format" in cmd
  assert "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b" in cmd


def test_progress_templates_are_attached_with_defaults():
  cmd = build_command(URL, VIDEO, download_dir=DEST)
  templates = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--progress-template"]
  assert len(templates) == 2
  download_tpl = next(t for t in templates if t.startswith(PROGRESS_PREFIX))
  pp_tpl = next(t for t in templates if t.startswith("postprocess:"))
  assert POSTPROCESS_PREFIX in pp_tpl
  # Every interpolated field in BOTH templates must carry a non-empty default.
  # A bare `|` (no default value) still contains "|" but renders as nothing
  # after the colon when the value is None -- yt-dlp substitutes the default
  # as a raw literal and skips the json ('j') conversion in that case, so an
  # empty default breaks json.loads just like a missing one would.
  for tpl in (download_tpl, pp_tpl):
    for field in tpl.split("%(")[1:]:
      key_and_default = field.split(")")[0]
      assert "|" in key_and_default, f"missing default in {key_and_default!r}"
      default_value = key_and_default.split("|", 1)[1]
      assert default_value != "", f"empty default in {key_and_default!r}"


def test_newline_and_no_colors_are_set():
  cmd = build_command(URL, VIDEO, download_dir=DEST)
  assert "--newline" in cmd
  assert "--no-colors" in cmd


def test_output_path_joins_dir_and_template():
  cmd = build_command(URL, VIDEO, download_dir=DEST)
  assert cmd[cmd.index("-o") + 1] == "/tmp/dl/%(title)s.%(ext)s"


def test_playlist_preset_gets_archive_and_indexed_template():
  cmd = build_command(URL, PL_VIDEO, download_dir=DEST, archive=Path("/tmp/a.txt"))
  assert cmd[cmd.index("--download-archive") + 1] == "/tmp/a.txt"
  assert "%(playlist_index)03d" in cmd[cmd.index("-o") + 1]


def test_non_playlist_preset_gets_no_archive():
  cmd = build_command(URL, VIDEO, download_dir=DEST, archive=Path("/tmp/a.txt"))
  assert "--download-archive" not in cmd


def test_height_cap_replaces_the_format_selector():
  cmd = build_command(URL, VIDEO, Overrides(height_cap=480), download_dir=DEST)
  assert cmd.count("-f") == 1
  assert cmd[cmd.index("-f") + 1] == "bv*[height<=480]+ba/b[height<=480]"
  assert "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b" not in cmd


def test_height_cap_is_ignored_for_audio_presets():
  cmd = build_command(URL, AUDIO, Overrides(height_cap=480), download_dir=DEST)
  assert "-f" not in cmd
  assert "-x" in cmd


def test_audio_format_override_replaces_preset_value():
  cmd = build_command(URL, AUDIO, Overrides(audio_format="flac"), download_dir=DEST)
  assert cmd[cmd.index("--audio-format") + 1] == "flac"
  assert cmd.count("--audio-format") == 1


def test_audio_format_override_ignored_for_video_presets():
  cmd = build_command(URL, VIDEO, Overrides(audio_format="flac"), download_dir=DEST)
  assert "--audio-format" not in cmd


def test_output_dir_override_wins():
  cmd = build_command(URL, VIDEO, Overrides(output_dir=Path("/elsewhere")), download_dir=DEST)
  assert cmd[cmd.index("-o") + 1].startswith("/elsewhere/")


def test_extra_args_are_appended_verbatim_before_the_url():
  cmd = build_command(
    URL, VIDEO, Overrides(extra_args=("--sleep-requests", "2")), download_dir=DEST
  )
  assert cmd[-4:] == ["--sleep-requests", "2", "--", URL]


def test_ytdlp_binary_is_configurable():
  cmd = build_command(URL, VIDEO, download_dir=DEST, ytdlp="/opt/yt-dlp")
  assert cmd[0] == "/opt/yt-dlp"


def test_returns_plain_strings_only():
  cmd = build_command(
    URL, PL_VIDEO, Overrides(height_cap=720), download_dir=DEST, archive=Path("/tmp/a.txt")
  )
  assert all(isinstance(a, str) for a in cmd)


@pytest.mark.parametrize("preset", BUILTIN_PRESETS, ids=lambda p: p.id)
def test_every_preset_builds_a_wellformed_command(preset):
  cmd = build_command(URL, preset, download_dir=DEST, archive=Path("/tmp/a.txt"))
  assert cmd[0] == "yt-dlp"
  assert cmd[-1] == URL
  assert "-o" in cmd


# elide_machinery -- the command-preview filter (Task 11)


def test_elide_machinery_drops_exactly_the_four_named_flags() -> None:
  cmd = build_command(URL, VIDEO, download_dir=DEST)
  visible, hidden = elide_machinery(cmd)
  assert hidden == 4
  for flag in MACHINERY_FLAGS:
    assert flag not in visible
  # Every token removed from `cmd` to get `visible` is accounted for by
  # `MACHINERY_FLAGS` or a value immediately following one of them -- i.e.
  # `visible` isn't just "missing the four flags", it's missing *nothing
  # else*. `list.remove` mutates in place and raises if the flag isn't
  # there, which is the point: this fails loudly if a flag this test expects
  # to be machinery was not actually removed.
  remainder = list(cmd)
  for token in cmd:
    if token in MACHINERY_FLAGS:
      remainder.remove(token)
  # The two progress-template values are still in `remainder` (only the
  # flag names were stripped above) but not in `visible`, so strip them the
  # same way `elide_machinery` does before comparing.
  templates = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--progress-template"]
  for value in templates:
    remainder.remove(value)
  assert visible == remainder


def test_elide_machinery_preserves_preset_args_output_template_and_url_in_order() -> None:
  cmd = build_command(URL, VIDEO, download_dir=DEST)
  visible, _ = elide_machinery(cmd)
  assert visible == [
    "yt-dlp",
    "-f",
    "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
    "--merge-output-format",
    "mp4",
    "-o",
    str(DEST / "%(title)s.%(ext)s"),
    "--",
    URL,
  ]


def test_elide_machinery_never_touches_extra_args_even_when_unfamiliar() -> None:
  """The over-filtering trap named in the brief: a flag `elide_machinery` has
  never heard of must survive untouched, however machine-generated it might
  look."""
  cmd = build_command(
    URL, VIDEO, Overrides(extra_args=("--sleep-requests", "2")), download_dir=DEST
  )
  visible, hidden = elide_machinery(cmd)
  assert hidden == 4  # unchanged: extra_args aren't machinery
  assert visible[-4:] == ["--sleep-requests", "2", "--", URL]


@pytest.mark.parametrize("preset", BUILTIN_PRESETS, ids=lambda p: p.id)
def test_elide_machinery_hidden_count_matches_real_removed_tokens(preset: Preset) -> None:
  """Recomputes the expected count independently of `elide_machinery`'s own
  bookkeeping, across every built-in preset -- not just the default -- so a
  regression that double-counts or drops a flag has nowhere to hide."""
  cmd = build_command(URL, preset, download_dir=DEST, archive=Path("/tmp/a.txt"))
  _, hidden = elide_machinery(cmd)
  assert hidden == sum(1 for token in cmd if token in MACHINERY_FLAGS)
