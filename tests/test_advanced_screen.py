"""Coverage for the advanced overrides drawer: prefill from the current
`Overrides`, save/cancel semantics, blank/junk-input handling, and that its
`ctrl+s`/`escape` bindings actually survive being pushed on top of a screen
full of `Input` widgets.

`app.push_screen(AdvancedScreen())` is awaited throughout: reading
`textual/app.py` confirms `push_screen(..., wait_for_dismiss=False)` (the
default) returns an `AwaitMount`, which awaits the screen's mount -- it is
not the `wait_for_dismiss=True` form (an `asyncio.Future` for the dismiss
result, only usable from a worker), so the brief's `await
app.push_screen(...)` is correct as written.

Like `tests/test_main_screen.py`, every test here goes through a full
`YtDlpTuiApp`, which always mounts a real `MainScreen` underneath (its
`on_mount` unconditionally pushes one). No test below types into
`#url-input`, so `MainScreen`'s real subprocess-probe path can't fire -- but
the same autouse guard from that file is carried here anyway, so a future
edit that adds a keystroke into `#url-input` can't silently regress into a
real `yt-dlp` invocation (that file's docstring documents exactly that
incident from an earlier round of this project).

`AdvancedScreen.AUTO_FOCUS` is not set, so it inherits `"*"` from `App`
(`Screen.AUTO_FOCUS` defaults to `None`, which falls back to
`self.app.AUTO_FOCUS`) -- the first `Input` (`#opt-dir`) is focused at mount,
same situation `MainScreen` is in with `#url-input`. `escape` and `ctrl+s`
are both non-printable, so `Input.check_consume_key` (which only claims
`character.isprintable()`) never claims them, and `Input` has no `BINDINGS`
entry for either key -- so `Screen._binding_chain` never finds an earlier
namespace to stop at. This is verified below with real `pilot.press(...)`
keypresses, not merely reasoned about.
"""

from pathlib import Path

import pytest
from textual.widgets import Input

from yt_dlp_tui.app import YtDlpTuiApp
from yt_dlp_tui.command import Overrides
from yt_dlp_tui.presets import BUILTIN_PRESETS
from yt_dlp_tui.probe import ProbeResult, Tooling
from yt_dlp_tui.screens import main as main_screen_module
from yt_dlp_tui.screens.advanced import AdvancedScreen
from yt_dlp_tui.screens.main import MainScreen

OFFLINE_TOOLING = Tooling(ytdlp=None, ffmpeg=None)


@pytest.fixture(autouse=True)
def _stub_probe(monkeypatch: pytest.MonkeyPatch) -> None:
  """Same structural guarantee as test_main_screen.py's fixture: no test in
  this file can reach a real `yt-dlp` subprocess through `MainScreen`'s
  `_probe`, regardless of what a future edit types into `#url-input`."""

  async def fake_probe(url: str, *, ytdlp: str) -> ProbeResult:
    return ProbeResult(title="Stubbed Title", duration=90, extractor="Fake")

  monkeypatch.setattr(main_screen_module, "probe", fake_probe)


def _make_app() -> YtDlpTuiApp:
  return YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)


async def test_saving_overrides_updates_the_app() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    screen = app.screen
    screen.query_one("#opt-height", Input).value = "480"
    screen.query_one("#opt-extra", Input).value = "--sleep-requests 2"
    screen.action_save()
    await pilot.pause()
    assert app.overrides.height_cap == 480
    assert app.overrides.extra_args == ("--sleep-requests", "2")


async def test_blank_fields_clear_overrides() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    app.screen.action_save()
    await pilot.pause()
    assert app.overrides.height_cap is None
    assert app.overrides.extra_args == ()


async def test_previously_set_overrides_are_actually_cleared_by_blank_fields() -> None:
  """The brief's own `test_blank_fields_clear_overrides` only proves that
  saving an already-blank drawer stays blank -- `app.overrides` starts as
  `Overrides()` by default, so that test would pass even if `action_save`
  never read the fields at all. This starts from a populated `Overrides`,
  blanks every field by hand, and checks the save actually overwrites the
  previous non-default state rather than leaving it untouched."""
  app = _make_app()
  app.overrides = Overrides(
    output_dir=Path("/tmp/previous"),
    height_cap=720,
    audio_format="mp3",
    extra_args=("--verbose",),
  )
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    screen = app.screen
    for widget_id in ("#opt-dir", "#opt-height", "#opt-audio", "#opt-extra"):
      screen.query_one(widget_id, Input).value = ""
    screen.action_save()
    await pilot.pause()
    assert app.overrides == Overrides()


async def test_non_numeric_height_is_ignored_not_fatal() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    app.screen.query_one("#opt-height", Input).value = "abc"
    app.screen.action_save()
    await pilot.pause()
    assert app.overrides.height_cap is None


async def test_extra_args_are_shell_split() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    app.screen.query_one("#opt-extra", Input).value = '--user-agent "my agent"'
    app.screen.action_save()
    await pilot.pause()
    assert app.overrides.extra_args == ("--user-agent", "my agent")


async def test_unbalanced_quotes_in_extra_args_do_not_crash() -> None:
  """`shlex.split` raises `ValueError` on an unterminated quote; `action_save`
  must degrade to an empty tuple rather than let the exception propagate and
  crash the app -- this is the branch the brief's `except ValueError` exists
  for, and it had no test before this one."""
  app = _make_app()
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    app.screen.query_one("#opt-extra", Input).value = '--user-agent "unterminated'
    app.screen.action_save()
    await pilot.pause()
    assert app.overrides.extra_args == ()


async def test_output_dir_is_stored_as_path() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    app.screen.query_one("#opt-dir", Input).value = "/tmp/out"
    app.screen.action_save()
    await pilot.pause()
    assert app.overrides.output_dir == Path("/tmp/out")


async def test_audio_format_is_saved() -> None:
  """Not one of the brief's pinned tests, but `#opt-audio` is one of the four
  named fields and had zero coverage otherwise."""
  app = _make_app()
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    app.screen.query_one("#opt-audio", Input).value = "opus"
    app.screen.action_save()
    await pilot.pause()
    assert app.overrides.audio_format == "opus"


async def test_fields_are_prefilled_from_existing_overrides() -> None:
  """`compose` reads `self.app.overrides` at push time to seed every Input.
  Height 0 is deliberately included: `str(0 or "")` renders as `""` because
  `0` is falsy in Python, which would silently drop a real (if unusual)
  height cap on prefill -- `AdvancedScreen.compose` must check `is None`
  rather than truthiness."""
  app = _make_app()
  app.overrides = Overrides(
    output_dir=Path("/tmp/previous"),
    height_cap=0,
    audio_format="mp3",
    extra_args=("--verbose", "--no-part"),
  )
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    screen = app.screen
    assert screen.query_one("#opt-dir", Input).value == "/tmp/previous"
    assert screen.query_one("#opt-height", Input).value == "0"
    assert screen.query_one("#opt-audio", Input).value == "mp3"
    assert screen.query_one("#opt-extra", Input).value == "--verbose --no-part"


async def test_cancel_does_not_modify_overrides_and_pops_screen() -> None:
  app = _make_app()
  app.overrides = Overrides(height_cap=720)
  async with app.run_test() as pilot:
    main_screen = app.screen
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    app.screen.query_one("#opt-height", Input).value = "999"
    app.screen.action_cancel()
    await pilot.pause()
    assert app.overrides == Overrides(height_cap=720)
    assert app.screen is main_screen


async def test_ctrl_s_saves_via_real_keypress_despite_input_focus() -> None:
  """`#opt-dir` holds focus at mount (AUTO_FOCUS). `ctrl+s` is not printable,
  so `Input.check_consume_key` must not claim it -- proven here with a real
  keypress rather than calling `action_save()` directly."""
  app = _make_app()
  async with app.run_test() as pilot:
    main_screen = app.screen
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    input_widget = app.screen.query_one("#opt-dir", Input)
    assert app.focused is input_widget

    app.screen.query_one("#opt-height", Input).value = "480"
    await pilot.press("ctrl+s")
    await pilot.pause()

    assert app.overrides.height_cap == 480
    assert app.screen is main_screen  # save also pops back to MainScreen


async def test_escape_cancels_via_real_keypress_despite_input_focus() -> None:
  app = _make_app()
  app.overrides = Overrides(height_cap=720)
  async with app.run_test() as pilot:
    main_screen = app.screen
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    input_widget = app.screen.query_one("#opt-dir", Input)
    assert app.focused is input_widget

    app.screen.query_one("#opt-height", Input).value = "999"
    await pilot.press("escape")
    await pilot.pause()

    assert app.overrides == Overrides(height_cap=720)  # unchanged: cancel, not save
    assert app.screen is main_screen


async def test_escape_on_drawer_does_not_trigger_main_screens_blur_url(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """`MainScreen` also binds `escape` (to `action_blur_url`). Once
  `AdvancedScreen` is pushed on top, `Screen._binding_chain` only walks from
  the focused widget up through the *active* screen and the App -- it never
  considers a screen sitting underneath in `screen_stack` -- so `escape`
  must resolve to `AdvancedScreen.action_cancel` only. Checked with a spy
  rather than assumed from the stack-ordering argument alone."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    main_screen = app.screen
    assert isinstance(main_screen, MainScreen)
    blur_calls = []
    monkeypatch.setattr(main_screen, "action_blur_url", lambda: blur_calls.append(True))

    await app.push_screen(AdvancedScreen())
    await pilot.pause()

    await pilot.press("escape")
    await pilot.pause()

    assert blur_calls == []
    assert app.screen is main_screen
