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

import re
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


def _toast_text(app: YtDlpTuiApp) -> str:
  """Every rendered line of every live `Toast`, whitespace-collapsed.

  Queried by CSS type selector rather than by importing `textual.widgets._toast`
  (private), and read off `render_line` rather than the stored notification
  message -- the message is the raw string whether or not markup is on, so only
  the rendered line can show markup having eaten part of it.
  """
  lines = [
    toast.render_line(y).text
    for toast in app.screen.query("Toast")
    for y in range(toast.size.height)
  ]
  return re.sub(r"\s+", " ", " ".join(lines)).strip()


async def _save_with(app: YtDlpTuiApp, field: str, value: str) -> None:
  await app.push_screen(AdvancedScreen())
  screen = app.screen
  screen.query_one(f"#{field}", Input).value = value
  screen.action_save()


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
    blur_calls: list[bool] = []
    monkeypatch.setattr(main_screen, "action_blur_url", lambda: blur_calls.append(True))

    await app.push_screen(AdvancedScreen())
    await pilot.pause()

    await pilot.press("escape")
    await pilot.pause()

    assert blur_calls == []
    assert app.screen is main_screen


async def test_pressing_a_after_escape_pushes_the_advanced_screen() -> None:
  """`MainScreen.action_advanced` (`screens/main.py`) is the only production
  line this task changes in that file, and it had zero coverage that doesn't
  replace the real body with a spy: `test_letter_keys_type_into_focused_url_input`
  and `test_escape_blurs_input_and_frees_the_letter_keys` in
  `test_main_screen.py` both monkeypatch `action_advanced` away before
  pressing "a", so they'd keep passing even if the real body were reverted to
  a `notify()` stub, deleted, or made to push the wrong screen. This drives
  the actual key sequence a user takes from the main screen -- escape to
  blur the URL input (freeing the letter keys, per Task 7's ruling), then
  "a" -- with the real `action_advanced` in place, and asserts a real
  `AdvancedScreen` ends up on top of the stack."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    await pilot.press("escape")
    await pilot.press("a")
    await pilot.pause()
    assert isinstance(app.screen, AdvancedScreen)


async def test_output_dir_expands_tilde() -> None:
  """`Path("~/Videos")` is not expanded on its own -- yt-dlp runs as a
  subprocess with no shell, so nothing else would expand it either, and
  `build_command` would emit a literal `~` directory under the process cwd.
  `~/...` is the single most likely thing a user types into a directory box,
  so `action_save` must call `.expanduser()`."""
  app = _make_app()
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    app.screen.query_one("#opt-dir", Input).value = "~/Videos"
    app.screen.action_save()
    await pilot.pause()
    assert app.overrides.output_dir == Path("~/Videos").expanduser()
    assert "~" not in str(app.overrides.output_dir)


async def test_non_positive_height_is_rejected_like_non_numeric() -> None:
  """`int("-5")`/`int("0")` both parse cleanly, but neither is a usable
  height cap: `build_command` would emit `-f 'bv*[height<=-5]+ba/b[height<=-5]'`,
  which no format satisfies, turning a silently-accepted typo into a
  confusing yt-dlp error at download time instead of at entry. Reject
  non-positive heights the same way non-numeric ones are rejected."""
  app = _make_app()
  async with app.run_test() as pilot:
    for junk in ("0", "-5"):
      # `action_save` pops the screen on every call, so each case needs its
      # own push -- reusing one `AdvancedScreen` across the loop would query
      # a widget that's no longer on the (popped) stack for the second case.
      await app.push_screen(AdvancedScreen())
      await pilot.pause()
      app.screen.query_one("#opt-height", Input).value = junk
      app.screen.action_save()
      await pilot.pause()
      assert app.overrides.height_cap is None


async def test_non_numeric_height_notifies_the_user(monkeypatch: pytest.MonkeyPatch) -> None:
  """The brief pins "ignored rather than fatal" for a junk height -- ignoring
  is correct, but silently dropping the value with no feedback is a
  different failure: the user has no way to know their input vanished."""
  app = _make_app()
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    screen = app.screen
    notifications: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
      screen, "notify", lambda *args, **kwargs: notifications.append((args, kwargs))
    )
    screen.query_one("#opt-height", Input).value = "abc"
    screen.action_save()
    await pilot.pause()
    assert app.overrides.height_cap is None
    assert notifications, "expected a notify() call warning about the dropped height"
    assert notifications[0][1].get("severity") == "warning"


async def test_unterminated_quote_in_extra_args_notifies_the_user(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Worse than the height case: one unterminated quote discards *every*
  flag the user typed into extra args, and the screen pops on save, so the
  text is gone and the field reopens empty. The save must still notify what
  was dropped rather than silently losing the user's input -- the drawer
  still pops (unauthorized to change save semantics), but the user is told."""
  app = _make_app()
  async with app.run_test() as pilot:
    await app.push_screen(AdvancedScreen())
    await pilot.pause()
    screen = app.screen
    notifications: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
      screen, "notify", lambda *args, **kwargs: notifications.append((args, kwargs))
    )
    screen.query_one("#opt-extra", Input).value = '--user-agent "unterminated'
    screen.action_save()
    await pilot.pause()
    assert app.overrides.extra_args == ()
    assert notifications, "expected a notify() call warning about the dropped extra args"
    assert notifications[0][1].get("severity") == "warning"
    assert app.screen is not screen  # save still pops -- not authorized to keep it open


# `notify()` markup (final whole-branch review, finding C4)
#
# Every notify test above monkeypatches `screen.notify`, so none of them ever
# renders a `Toast` -- and neither does anything else in this suite:
# `App.run_test()` sets `_disable_notifications = not notifications` and
# `notifications` defaults to `False`, so zero `Toast` widgets mount under the
# default harness. The two tests below pass `notifications=True` on purpose;
# they are the only place in the project where a notification is actually
# drawn.


async def test_notify_echoes_a_bracketed_value_verbatim() -> None:
  """`App.notify` defaults `markup=True` and `Toast.render` runs the message
  through `Content.from_markup`, so a bracketed run in the user's own input is
  eaten before it reaches the screen. That is precisely the silent-loss failure
  these two notifies exist to prevent: the toast would report a *different*
  value than the one that was dropped."""
  app = _make_app()
  async with app.run_test(notifications=True) as pilot:
    await _save_with(app, "opt-extra", '[bold]x "oops')
    await pilot.pause()
    assert '[bold]x "oops' in _toast_text(app)

    await _save_with(app, "opt-height", "[dim]9x")
    await pilot.pause()
    assert "[dim]9x" in _toast_text(app)


async def test_notify_with_an_unbalanced_bracket_does_not_kill_the_app() -> None:
  """Worse than a mangled echo: `Content.from_markup` raises `MarkupError` on
  an unbalanced tag, out of `Toast.render`, and the app goes down -- while
  reporting a *validation* problem, i.e. exactly when the user already typed
  something malformed."""
  app = _make_app()
  async with app.run_test(notifications=True) as pilot:
    await _save_with(app, "opt-height", "[/b]7x")
    await pilot.pause()
    assert app.is_running is True
    assert "[/b]7x" in _toast_text(app)
