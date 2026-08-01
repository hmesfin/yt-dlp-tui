"""A malformed `~/.config/yt-dlp-tui/config.toml` must not crash the app
(final whole-branch review, finding I8).

The README documents that file and tells people to write presets into it, but
`YtDlpTuiApp.__init__` called `load_presets()` unguarded, so bad TOML or a
missing key propagated out of the constructor as a raw traceback -- before the
app had drawn anything, with no message naming the file at fault.

The `id` field was worse: it reached `id=f"preset-{preset.id}"` on a Textual
widget, and the README states no constraint on it, so `id = "flac hq"` raised
`BadIdentifier` at startup. Rows are keyed by position now, and the preset id
is free-form.

Every test here writes a real config file under a tmp `XDG_CONFIG_HOME` (see
`conftest.py`) and starts a real app with `presets=None`, i.e. the production
path -- injecting presets, which every other screen test does, is precisely
what kept this untested.
"""

from pathlib import Path

import pytest
from textual.widgets import ListView, Static

from yt_dlp_tui import config
from yt_dlp_tui.app import YtDlpTuiApp
from yt_dlp_tui.presets import BUILTIN_PRESETS
from yt_dlp_tui.probe import ProbeResult, Tooling
from yt_dlp_tui.screens import main as main_screen_module

OFFLINE_TOOLING = Tooling(ytdlp=None, ffmpeg=None)

GOOD_CONFIG = """
[[preset]]
id = "flac hq"
name = "Lossless"
args = ["-x", "--audio-format", "flac"]
"""

BAD_TOML = """
[[preset]
id = "flac"
"""

MISSING_KEY = """
[[preset]]
id = "flac"
args = ["-x"]
"""


@pytest.fixture(autouse=True)
def _stub_probe(monkeypatch: pytest.MonkeyPatch) -> None:
  async def fake_probe(url: str, *, ytdlp: str) -> ProbeResult:
    return ProbeResult(title="Stubbed Title", duration=90, extractor="Fake")

  monkeypatch.setattr(main_screen_module, "probe", fake_probe)


def _write_config(text: str) -> None:
  path = config.config_path()
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(text)


def _real_config_app() -> YtDlpTuiApp:
  """`presets=None`: the production path that reads the user's config file."""
  return YtDlpTuiApp(tooling=OFFLINE_TOOLING)


@pytest.mark.parametrize("text", [BAD_TOML, MISSING_KEY], ids=["bad-toml", "missing-name-key"])
async def test_a_broken_config_falls_back_to_builtins_with_a_visible_warning(text: str) -> None:
  _write_config(text)
  app = _real_config_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    assert app.is_running is True
    assert app.presets == BUILTIN_PRESETS
    warning = str(app.screen.query_one("#tool-warning", Static).content)
    assert "config.toml" in warning


async def test_a_preset_id_that_is_not_an_identifier_still_starts_and_selects() -> None:
  """`id = "flac hq"` (or a dot, or a leading digit) raised
  `BadIdentifier: 'preset-flac hq' is an invalid id` out of `on_mount`. The
  README places no constraint on `id`, so the widget keying had to stop
  depending on it -- and the preset lookup that reads the key back has to
  still land on the right preset."""
  _write_config(GOOD_CONFIG)
  app = _real_config_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    assert app.is_running is True
    assert app.presets[-1].id == "flac hq"

    listing = app.screen.query_one("#preset-list", ListView)
    assert len(listing.children) == len(BUILTIN_PRESETS) + 1

    await pilot.press("escape")
    for _ in range(len(BUILTIN_PRESETS)):
      await pilot.press("down")
    assert app.selected_preset.id == "flac hq"
    assert "--audio-format flac" in str(app.screen.query_one("#command-preview", Static).content)


async def test_no_warning_when_there_is_no_config_file(tmp_path: Path) -> None:
  """The overwhelmingly common case: no config file at all is not a problem
  and must not put a scary banner above the URL box."""
  app = _real_config_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    assert app.presets == BUILTIN_PRESETS
    warning = str(app.screen.query_one("#tool-warning", Static).content)
    assert "config.toml" not in warning
