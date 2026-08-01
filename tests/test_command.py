from pathlib import Path

import pytest
from conftest import DroppedTokens

from yt_dlp_tui.command import (
  POSTPROCESS_PREFIX,
  POSTPROCESS_TEMPLATE,
  PROGRESS_PREFIX,
  PROGRESS_TEMPLATE,
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
#
# The six tokens the spec fixes as pure plumbing, spelled out here rather than
# imported from `command.MACHINERY_BLOCK`: these tests have to pin the expected
# outcome, not the implementation's own notion of it. The two template values
# are imported, but `test_progress_templates_are_attached_with_defaults` above
# pins their contents independently.
EXPECTED_MACHINERY = [
  "--newline",
  "--no-colors",
  "--progress-template",
  PROGRESS_TEMPLATE,
  "--progress-template",
  POSTPROCESS_TEMPLATE,
]


def test_elide_machinery_drops_exactly_the_spec_machinery_block(
  dropped_tokens: DroppedTokens,
) -> None:
  """The oracle names the six tokens that must disappear, rather than
  recomputing them with the implementation's own rule. The version this
  replaces did the latter -- it rebuilt the expected result by filtering on
  `MACHINERY_FLAGS` membership, the exact rule under test -- so it could not
  fail however wrong that rule was."""
  cmd = build_command(URL, VIDEO, download_dir=DEST)
  visible, hidden = elide_machinery(cmd)
  assert dropped_tokens(cmd, visible) == EXPECTED_MACHINERY
  assert hidden == 4


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
def test_elide_machinery_drops_the_same_block_for_every_preset(
  preset: Preset, dropped_tokens: DroppedTokens
) -> None:
  """`build_command` emits the same six-token machinery block for every
  preset, so the elided view must too -- including the playlist presets, whose
  extra `--download-archive` pair sits after it."""
  cmd = build_command(URL, preset, download_dir=DEST, archive=Path("/tmp/a.txt"))
  visible, hidden = elide_machinery(cmd)
  assert dropped_tokens(cmd, visible) == EXPECTED_MACHINERY
  assert hidden == 4


@pytest.mark.parametrize(
  "extra",
  [
    ("--postprocessor-args", "--no-colors"),
    ("--progress-template", "MINE:{}"),
    ("--no-colors",),
    ("--newline",),
  ],
  ids=["machinery-name-as-a-value", "same-flag-again", "bare-flag", "bare-newline"],
)
def test_a_users_own_flag_is_never_elided_for_sharing_a_machinery_name(
  extra: tuple[str, ...], dropped_tokens: DroppedTokens
) -> None:
  """Elision is by position, not by token membership. Matching on the token
  alone reached into `extra_args`: `--postprocessor-args --no-colors` rendered
  as `--postprocessor-args` with its value gone, and `--progress-template
  MINE:{}` vanished entirely -- so the command on screen was not the command
  that ran, which is the one invariant this whole preview exists to hold."""
  cmd = build_command(URL, VIDEO, Overrides(extra_args=extra), download_dir=DEST)
  visible, hidden = elide_machinery(cmd)
  assert visible[-len(extra) - 2 :] == [*extra, "--", URL]
  assert hidden == 4
  # And nothing beyond the block went, whatever the extra args happen to be
  # named -- `visible` losing the right tail is not enough on its own.
  assert dropped_tokens(cmd, visible) == EXPECTED_MACHINERY


def test_elide_machinery_hides_nothing_when_the_block_is_not_where_it_belongs() -> None:
  """Fail safe toward honesty. `elide_machinery` is documented as a filter over
  an argv `build_command` produced; handed anything else it must not guess
  which tokens were plumbing -- showing four extra flags is harmless, hiding a
  flag that is really running is not."""
  handmade = ["yt-dlp", "-f", "best", "--newline", "--no-colors", "--", URL]
  assert elide_machinery(handmade) == (handmade, 0)


# User-preset shapes the built-ins never produce (deferred minor, Task 3)


def test_a_preset_whose_args_end_in_a_bare_f_does_not_crash_the_override() -> None:
  """`_replace_flag` did `out[out.index(flag) + 1] = value` with no bounds
  check. Nothing in `BUILTIN_PRESETS` ends in a valueless flag, but a preset
  read from the user's config.toml can -- and `build_command` runs on every
  keystroke, so the IndexError would land in a reactive watcher."""
  preset = Preset(id="odd", name="Odd", args=("--merge-output-format", "mp4", "-f"))
  cmd = build_command(URL, preset, Overrides(height_cap=480), download_dir=DEST)
  # `-f` is still the last of the preset's own args -- nothing was inserted
  # after it -- and the override simply did not apply.
  assert cmd[cmd.index("-f") + 1] == "-o"
  assert "bv*[height<=480]+ba/b[height<=480]" not in cmd


def test_a_bare_audio_format_at_the_end_of_a_preset_is_left_alone() -> None:
  preset = Preset(id="odd-audio", name="Odd audio", args=("-x", "--audio-format"))
  cmd = build_command(URL, preset, Overrides(audio_format="flac"), download_dir=DEST)
  assert cmd[cmd.index("--audio-format") + 1] == "-o"
  assert "flac" not in cmd
