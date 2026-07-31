"""End-to-end coverage for the main screen: preset list, live command preview,
playlist reordering, and the URL-input download affordance.

Textual 8.2.8 is installed (the plan targeted >=0.79); a few things differ
from a naive transcription of the plan and are called out inline:

- `Static` no longer exposes `.renderable` -- the public accessor for what
  was set via `.update()` is `.content`.
- `App.AUTO_FOCUS` defaults to "*", so the URL `Input` grabs focus on mount,
  and `Input` itself binds "enter" to its own `action_submit` (which posts
  `Input.Submitted`). A Screen-level `("enter", "download", ...)` binding
  never actually fires while the input is focused -- the real trigger has to
  be `Input.Submitted`. See `test_enter_in_url_input_triggers_download`.
- `App.query_one`/`.query` do NOT search whatever screen is currently on top
  of the stack. `App._get_dom_base()` returns `default_screen`, which is
  cached once (`_compose_screen`) from `self.screen` at `_on_compose()` time
  -- i.e. the auto-created default screen that exists before `on_mount` ever
  runs `push_screen(MainScreen())` -- and is never refreshed afterwards. So
  `app.query_one("#preset-list")` always misses once a real screen has been
  pushed; querying has to go through `app.screen.query_one(...)` instead.

None of these tests type into the URL input (which would fire
`Input.Changed` -> a real probe subprocess) or otherwise touch the network;
presets and tooling are injected so the suite stays fully offline.
"""

from textual.widgets import Input

from yt_dlp_tui.app import YtDlpTuiApp
from yt_dlp_tui.presets import BUILTIN_PRESETS
from yt_dlp_tui.probe import ProbeResult, Tooling

OFFLINE_TOOLING = Tooling(ytdlp=None, ffmpeg=None)


async def test_app_starts_and_shows_presets() -> None:
  app = YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)
  async with app.run_test() as pilot:
    await pilot.pause()
    listing = app.screen.query_one("#preset-list")
    assert listing is not None
    assert app.selected_preset == BUILTIN_PRESETS[0]


async def test_command_preview_updates_with_url() -> None:
  app = YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)
  async with app.run_test() as pilot:
    app.url = "https://example.com/v"
    await pilot.pause()
    preview = app.screen.query_one("#command-preview").content
    assert "https://example.com/v" in str(preview)


async def test_command_preview_reflects_selected_preset() -> None:
  app = YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)
  async with app.run_test() as pilot:
    app.url = "https://example.com/v"
    app.selected_preset = next(p for p in BUILTIN_PRESETS if p.id == "audio-m4a")
    await pilot.pause()
    assert "--embed-thumbnail" in str(app.screen.query_one("#command-preview").content)


async def test_playlist_probe_reorders_presets() -> None:
  app = YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)
  async with app.run_test() as pilot:
    app.probe_result = ProbeResult(title="A list", is_playlist=True, playlist_count=20)
    await pilot.pause()
    assert app.ordered_presets[0].is_playlist


async def test_empty_url_does_not_crash_preview() -> None:
  app = YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)
  async with app.run_test() as pilot:
    app.url = ""
    await pilot.pause()
    assert app.screen.query_one("#command-preview") is not None


async def test_enter_in_url_input_triggers_download(monkeypatch) -> None:
  """`Input` swallows "enter" for its own submit action before a Screen-level
  binding ever sees it, so the download affordance must be wired through
  `Input.Submitted` rather than a Screen BINDINGS entry alone."""
  app = YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)
  async with app.run_test() as pilot:
    await pilot.pause()
    screen = app.screen
    called = []
    monkeypatch.setattr(screen, "action_download", lambda: called.append(True))
    input_widget = app.screen.query_one("#url-input", Input)
    await screen.on_input_submitted(Input.Submitted(input_widget, input_widget.value))
    assert called == [True]
