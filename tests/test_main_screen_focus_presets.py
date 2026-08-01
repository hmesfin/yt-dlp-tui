"""Focus routing after `escape`, and preset-selection stability across the
per-keystroke probe rebuilds (final whole-branch review, findings C1 and C2).

Split out of `test_main_screen.py` (already near the 500-line ceiling) rather
than appended to it.

C1: `action_blur_url` used to call `set_focus(None)`, so after `escape`
nothing held focus at all -- while the legend for exactly that state promised
`↑↓ preset · ⏎ download`. Both were dead: arrows went nowhere, and `enter`
had no handler once the `Input` (whose `Submitted` message is the only thing
wired to download) had lost focus. The only route to a non-default preset was
two undocumented `tab` presses.

C2: `refresh_presets()` set `listing.index = 0` on *every* rebuild, and
`watch_probe_result` rebuilds on every probe result -- which fires once per
keystroke, with no debounce. Choosing a preset while a probe was in flight got
silently undone when it resolved.

Carries the same autouse `_stub_probe` guard as the other screen test modules:
several tests here type into `#url-input`, `yt-dlp` is on PATH in this
environment, and an unguarded keystroke would shell out for real.
"""

import asyncio

import pytest
from textual.widgets import Input, ListView, Static

from yt_dlp_tui.app import YtDlpTuiApp
from yt_dlp_tui.presets import BUILTIN_PRESETS
from yt_dlp_tui.probe import ProbeResult, Tooling
from yt_dlp_tui.screens import main as main_screen_module

OFFLINE_TOOLING = Tooling(ytdlp=None, ffmpeg=None)
AUDIO_INDEX = next(i for i, p in enumerate(BUILTIN_PRESETS) if p.id == "audio-m4a")


@pytest.fixture(autouse=True)
def _stub_probe(monkeypatch: pytest.MonkeyPatch) -> None:
  async def default_fake_probe(url: str, *, ytdlp: str) -> ProbeResult:
    return ProbeResult(title="Stubbed Title", duration=90, extractor="Fake")

  monkeypatch.setattr(main_screen_module, "probe", default_fake_probe)


def _make_app() -> YtDlpTuiApp:
  return YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)


# C1 -- what `escape` actually leaves live


async def test_escape_focuses_the_preset_list_so_the_arrow_keys_work() -> None:
  """The legend shown after `escape` promises `↑↓ preset`. With focus dropped
  to `None` the arrows reached nothing at all -- verified by pressing them for
  real rather than calling `action_cursor_down`."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    listing = app.screen.query_one("#preset-list", ListView)

    await pilot.press("escape")
    assert app.focused is listing

    await pilot.press("down")
    assert listing.index == 1
    assert app.selected_preset == BUILTIN_PRESETS[1]

    await pilot.press("up")
    assert listing.index == 0
    assert app.selected_preset == BUILTIN_PRESETS[0]


async def test_enter_after_escape_starts_the_download(monkeypatch: pytest.MonkeyPatch) -> None:
  """The other half of the same legend: `⏎ download`. While the `Input` has
  focus that runs through `Input.Submitted`; once `escape` has moved focus to
  the preset list, `ListView` owns `enter` for its own `select_cursor`, so the
  key needs an explicit route back to the screen's download action or the
  legend is lying in the state it describes."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    downloads: list[bool] = []
    monkeypatch.setattr(app.screen, "action_download", lambda: downloads.append(True))

    await pilot.press("escape")
    await pilot.press("enter")

    assert downloads == [True]


async def test_letter_keys_and_q_still_fire_with_the_preset_list_focused(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Moving focus to a widget instead of dropping it must not re-break what
  `escape` exists to unblock: `ListView` does not override
  `check_consume_key`, so the printable keys still reach the Screen and App
  bindings."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    advanced: list[bool] = []
    monkeypatch.setattr(app.screen, "action_advanced", lambda: advanced.append(True))
    preview = app.screen.query_one("#command-preview", Static)

    await pilot.press("escape")
    await pilot.press("a")
    assert advanced == [True]

    await pilot.press("v")
    await pilot.pause()
    assert "--progress-template" in str(preview.content)  # `v` toggled the full command

    await pilot.press("q")
    assert app.is_running is False


async def test_clicking_a_preset_row_selects_it_without_starting_a_download(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """`ListView` posts `Selected` from a mouse click as well as from `enter`,
  so wiring `on_list_view_selected` to the download would mean a mouse user
  could not change preset at all without kicking off a network download.
  `enter` is routed through a binding instead, precisely so a click stays a
  selection."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    downloads: list[bool] = []
    monkeypatch.setattr(app.screen, "action_download", lambda: downloads.append(True))

    await pilot.click(f"#preset-{BUILTIN_PRESETS[AUDIO_INDEX].id}")
    await pilot.pause()

    assert app.selected_preset == BUILTIN_PRESETS[AUDIO_INDEX]
    assert downloads == []


# C2 -- a resolving probe must not revert the user's preset choice


async def test_a_resolving_probe_does_not_revert_the_users_preset_choice(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """The live race: picking a preset takes about as long as a probe. The probe
  is gated on an `asyncio.Event` so the choice provably lands *while it is in
  flight*, which is the window the unconditional `index = 0` destroyed."""
  probe_may_finish = asyncio.Event()

  async def gated_fake_probe(url: str, *, ytdlp: str) -> ProbeResult:
    await probe_may_finish.wait()
    return ProbeResult(title="Stubbed Title", duration=90, extractor="Fake")

  monkeypatch.setattr(main_screen_module, "probe", gated_fake_probe)
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    app.screen.query_one("#url-input", Input).value = "https://example.com/v"
    await pilot.pause()

    await pilot.press("escape")
    for _ in range(AUDIO_INDEX):
      await pilot.press("down")
    assert app.selected_preset.id == "audio-m4a"

    probe_may_finish.set()
    await pilot.pause()
    await pilot.pause()

    assert app.selected_preset.id == "audio-m4a"
    assert app.screen.query_one("#preset-list", ListView).index == AUDIO_INDEX
    assert "--embed-thumbnail" in str(app.screen.query_one("#command-preview", Static).content)


async def test_a_playlist_probe_still_preselects_a_playlist_preset() -> None:
  """The behaviour the C2 fix must not break: a probe that *reorders* the list
  still preselects the top (playlist) row, even though the user had already
  moved the highlight off row 0. Preserving the choice is scoped to rebuilds
  that leave the ordering alone."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    await pilot.press("escape")
    await pilot.press("down")
    assert app.selected_preset == BUILTIN_PRESETS[1]

    app.probe_result = ProbeResult(title="A list", is_playlist=True, playlist_count=20)
    await pilot.pause()

    listing = app.screen.query_one("#preset-list", ListView)
    assert listing.index == 0
    assert app.selected_preset.is_playlist


async def test_repeated_identical_probe_results_do_not_move_the_selection() -> None:
  """`watch_probe_result` fires per keystroke with no debounce (an accepted
  limitation), so a 40-character URL rebuilds the list ~40 times. Every one of
  those rebuilds used to snap the highlight back to row 0."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    await pilot.press("escape")
    for _ in range(AUDIO_INDEX):
      await pilot.press("down")
    assert app.selected_preset.id == "audio-m4a"

    for n in range(5):
      app.probe_result = ProbeResult(title=f"Title {n}", duration=n, extractor="Fake")
      await pilot.pause()

    assert app.screen.query_one("#preset-list", ListView).index == AUDIO_INDEX
    assert app.selected_preset.id == "audio-m4a"
