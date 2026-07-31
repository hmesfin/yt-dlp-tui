"""End-to-end coverage for the main screen: preset list, live command preview,
playlist reordering, key routing between the URL input and Screen/App
bindings, and the download affordance.

Textual 8.2.8 is installed (the plan targeted >=0.79); a few things differ
from a naive transcription of the plan and are called out inline:

- `Static` no longer exposes `.renderable` -- the public accessor for what
  was set via `.update()` is `.content`.
- `App.query_one`/`.query` do NOT search whatever screen is currently on top
  of the stack. `App._get_dom_base()` returns `default_screen`, which is
  cached once (`_compose_screen`) from `self.screen` at `_on_compose()` time
  -- i.e. the auto-created default screen that exists before `on_mount` ever
  runs `push_screen(MainScreen())` -- and is never refreshed afterwards. So
  `app.query_one("#preset-list")` always misses once a real screen has been
  pushed; querying has to go through `app.screen.query_one(...)` instead.
- `ListView` composed empty and populated later never highlights a row on
  its own: `ListView._on_mount` only sets `index` when `self.children` is
  non-empty *at mount time* (`_list_view.py`), which it never is here. So
  `refresh_presets()` sets `listing.index = 0` itself after every rebuild.
- `Input` claims every printable key for itself
  (`Input.check_consume_key` returns True for any printable character), and
  `Screen._binding_chain` deletes a key an ancestor claims from every
  ancestor's own bindings map before dispatch even runs -- so while the URL
  input has focus (which `App.AUTO_FOCUS = "*"` gives it at mount), bare
  letter bindings like "a" and "q" are unreachable; they type into the box
  instead. `enter` isn't filtered this way (it isn't a printable character),
  but `Input` binds it to its own `action_submit`, and `Screen._check_bindings`
  stops at the *first* namespace with a matching binding while walking the
  chain from the focused widget outward -- `Input` is always first -- so a
  Screen-level `enter` binding would never be reached either way. The design
  (owner-approved after a spec conflict: bare letter bindings vs. an
  auto-focused input can't both fully work) is: typing wins while the URL
  input has focus; `escape` blurs it so `a`/`q` become live; `ctrl+q`
  produces no printable character so `Input` never claims it and it always
  quits; `enter` inside the box triggers download via `Input.Submitted`,
  never via a BINDINGS entry.

Only one test below types a real, non-empty URL into `#url-input` (the only
path to `on_input_changed` -> `_probe`); it monkeypatches
`yt_dlp_tui.screens.main.probe` first, so no real subprocess or network call
happens anywhere in this file. `tooling` and `presets` are also injected on
every app so the suite never reads a real config file or shells out to
detect `yt-dlp`/`ffmpeg`.
"""

import pytest
from textual.widgets import Input, ListView, Static

from yt_dlp_tui.app import YtDlpTuiApp
from yt_dlp_tui.presets import BUILTIN_PRESETS
from yt_dlp_tui.probe import ProbeResult, Tooling
from yt_dlp_tui.screens import main as main_screen_module

OFFLINE_TOOLING = Tooling(ytdlp=None, ffmpeg=None)


def _make_app() -> YtDlpTuiApp:
  return YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)


async def test_app_starts_and_shows_presets() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    listing = app.screen.query_one("#preset-list", ListView)
    assert [item.id for item in listing.children] == [
      f"preset-{preset.id}" for preset in BUILTIN_PRESETS
    ]
    # Not just present -- actually highlighted, so the user can see which
    # preset drives the preview on a screen whose whole premise is that.
    assert listing.index == 0
    assert listing.highlighted_child is not None
    assert listing.highlighted_child.id == f"preset-{BUILTIN_PRESETS[0].id}"
    assert app.selected_preset == BUILTIN_PRESETS[0]


async def test_command_preview_updates_with_url() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    app.url = "https://example.com/v"
    await pilot.pause()
    preview = app.screen.query_one("#command-preview").content
    assert "https://example.com/v" in str(preview)


async def test_command_preview_reflects_selected_preset() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    app.url = "https://example.com/v"
    app.selected_preset = next(p for p in BUILTIN_PRESETS if p.id == "audio-m4a")
    await pilot.pause()
    assert "--embed-thumbnail" in str(app.screen.query_one("#command-preview").content)


async def test_playlist_probe_reorders_presets_and_steers_selection() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    listing = app.screen.query_one("#preset-list", ListView)
    before_order = [item.id for item in listing.children]

    app.probe_result = ProbeResult(title="A list", is_playlist=True, playlist_count=20)
    await pilot.pause()

    # The property pinned-behaviour 4 names:
    assert app.ordered_presets[0].is_playlist
    # The actual widget rebuild that property is supposed to drive -- this is
    # the exact path that raised DuplicateIds when clear()/append() weren't
    # awaited, so assert the row order changed and landed correctly rather
    # than just trusting the rebuild happened.
    after_order = [item.id for item in listing.children]
    assert after_order != before_order
    assert after_order[0] in {"preset-playlist-video", "preset-playlist-audio"}
    assert all(p.is_playlist for p in app.ordered_presets[:2])
    # And the reorder has to steer selection, not just the list contents:
    # nothing else moves `selected_preset` off the pre-reorder preset.
    assert listing.index == 0
    assert app.selected_preset.is_playlist


async def test_empty_url_does_not_crash_preview() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    app.url = ""
    await pilot.pause()
    preview = str(app.screen.query_one("#command-preview", Static).content)
    # `build_command` appends `["--", url]`; shlex.join renders an empty url
    # as `''`, so the preview should visibly still be a real, well-formed
    # command rather than merely "exists".
    assert preview.endswith("-- ''")


async def test_enter_in_url_input_triggers_download(monkeypatch: pytest.MonkeyPatch) -> None:
  """Presses a real "enter" key rather than calling `on_input_submitted`
  directly: the thing worth pinning is that Textual actually routes
  `Input.Submitted` to that handler while the URL input has focus, not that
  a one-line method calls the method it's named after. Also confirms the
  premise that lets every other test in this file skip a real network probe:
  `Input.action_submit` posts `Submitted` without changing `.value`, so this
  can never fire `Input.Changed` / `_probe`."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    screen = app.screen
    called = []
    monkeypatch.setattr(screen, "action_download", lambda: called.append(True))
    input_widget = app.screen.query_one("#url-input", Input)
    assert app.focused is input_widget

    await pilot.press("enter")

    assert called == [True]
    assert app.url == ""  # Input.Changed never fired -- no probe was scheduled


async def test_letter_keys_type_into_focused_url_input(monkeypatch: pytest.MonkeyPatch) -> None:
  """Owner-approved tradeoff: typing wins over the bare `a`/`q` bindings
  while the URL box has focus (which is where `AUTO_FOCUS` puts it at
  mount)."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    screen = app.screen
    advanced_calls = []
    monkeypatch.setattr(screen, "action_advanced", lambda: advanced_calls.append(True))
    input_widget = app.screen.query_one("#url-input", Input)
    assert app.focused is input_widget

    await pilot.press("a")
    await pilot.press("q")

    assert input_widget.value == "aq"
    assert advanced_calls == []
    assert app.is_running is True


async def test_escape_blurs_input_and_frees_the_letter_keys(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    screen = app.screen
    advanced_calls = []
    monkeypatch.setattr(screen, "action_advanced", lambda: advanced_calls.append(True))
    input_widget = app.screen.query_one("#url-input", Input)

    await pilot.press("escape")
    assert app.focused is not input_widget

    await pilot.press("a")

    assert advanced_calls == [True]
    assert input_widget.value == ""  # "a" fired the action, it did not get typed


async def test_ctrl_q_quits_while_url_input_is_focused() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    assert app.focused is app.screen.query_one("#url-input", Input)
    assert app.is_running is True

    await pilot.press("ctrl+q")

    assert app.is_running is False


async def test_ctrl_q_quits_after_escape() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    await pilot.press("escape")
    assert app.is_running is True

    await pilot.press("ctrl+q")

    assert app.is_running is False


async def test_typing_a_url_updates_state_and_replaces_stale_meta(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """The only test that drives `on_input_changed` -> `_probe` for real, so it
  monkeypatches `probe` at the module level `screens.main` imported it under
  -- a real subprocess is never spawned. Covers two things nothing else in
  this file does: that Textual actually routes `Input.Changed` to the
  handler that sets `app.url`, and that `#meta` doesn't keep showing a
  previous URL's title once the user starts editing."""

  async def fake_probe(url: str, *, ytdlp: str) -> ProbeResult:
    return ProbeResult(title="Stubbed Title", duration=90, extractor="Fake")

  monkeypatch.setattr(main_screen_module, "probe", fake_probe)
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    meta = app.screen.query_one("#meta", Static)
    meta.update("stale title from a previous URL")

    await pilot.press("a", "b", "c")
    await pilot.pause()

    assert app.url == "abc"
    assert "stale title" not in str(meta.content)
    assert "Stubbed Title" in str(meta.content)
