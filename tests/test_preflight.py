"""Pure preflight checks: what's missing on PATH before the user hits download."""

from yt_dlp_tui.presets import BUILTIN_PRESETS, Preset
from yt_dlp_tui.probe import Tooling, preflight_warnings


def test_warns_when_ytdlp_missing() -> None:
  warnings = preflight_warnings(Tooling(ytdlp=None, ffmpeg="/usr/bin/ffmpeg"), BUILTIN_PRESETS)
  assert any("yt-dlp" in w for w in warnings)


def test_warns_when_ffmpeg_missing_and_presets_need_it() -> None:
  warnings = preflight_warnings(Tooling(ytdlp="/usr/bin/yt-dlp", ffmpeg=None), BUILTIN_PRESETS)
  assert any("ffmpeg" in w for w in warnings)


def test_no_warnings_when_all_present() -> None:
  tooling = Tooling(ytdlp="/usr/bin/yt-dlp", ffmpeg="/usr/bin/ffmpeg")
  assert preflight_warnings(tooling, BUILTIN_PRESETS) == []


def test_no_ffmpeg_warning_when_no_preset_needs_it() -> None:
  presets = (Preset(id="x", name="x", args=("-f", "b")),)
  warnings = preflight_warnings(Tooling(ytdlp="/usr/bin/yt-dlp", ffmpeg=None), presets)
  assert warnings == []


def test_the_ffmpeg_warning_does_not_claim_the_presets_are_disabled() -> None:
  """The spec asks for "warn *and disable*" the ffmpeg-dependent presets.
  Disabling is a follow-up (controller ruling), so the wording has to describe
  what actually happens: all five presets stay selectable and will run and fail
  mid-download. Calling them "unavailable" told the user the app had taken care
  of it, which sends them looking for a preset that is right there in the list.
  """
  warnings = preflight_warnings(Tooling(ytdlp="/usr/bin/yt-dlp", ffmpeg=None), BUILTIN_PRESETS)
  message = next(w for w in warnings if "ffmpeg" in w)
  assert "unavailable" not in message
  assert "fail" in message
