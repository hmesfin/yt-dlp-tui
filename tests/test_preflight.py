"""Pure preflight checks: what's missing on PATH before the user hits download."""

from yt_dlp_tui.presets import BUILTIN_PRESETS
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
  from yt_dlp_tui.presets import Preset

  presets = (Preset(id="x", name="x", args=("-f", "b")),)
  warnings = preflight_warnings(Tooling(ytdlp="/usr/bin/yt-dlp", ffmpeg=None), presets)
  assert warnings == []
