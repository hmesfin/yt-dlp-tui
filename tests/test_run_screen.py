"""Coverage for the run screen's rendering: how each event type reaches the
widgets, the log caps, and the wiring from the main screen.

Everything here drives `apply_event` (or a keypress) directly and asserts on
widgets. The companion file `test_run_screen_teardown.py` owns the other half
-- whether the child *process* is actually dead -- and is where the real
subprocesses live.

**Nothing here may spawn a real `yt-dlp` or touch the network.** This is the
first screen that runs subprocesses on purpose, so the guarantee is structural
rather than per-test discipline (an earlier round of `test_main_screen.py`
leaked two real `yt-dlp -J` invocations out of one un-guarded keystroke). The
autouse `_guarded_run` fixture replaces the `run` name `screens/run.py`
resolves at call time; it only delegates to the real runner when `argv[0]` is
this interpreter, and records everything else -- so pushing a `RunScreen` with
a genuine `yt-dlp` argv records the argv and spawns nothing. `_stub_probe`
does the same for the `MainScreen` mounted underneath.

Textual 8.2.8, same as the other screen tests: `app.query_one` never searches
a pushed screen (use `app.screen.query_one`), and `Static` exposes what
`.update()` stored as `.content`, not `.renderable`.
"""

import sys
from collections.abc import AsyncIterator

import pytest
from textual.pilot import Pilot
from textual.widgets import ProgressBar, RichLog, Static

from yt_dlp_tui import runner
from yt_dlp_tui.app import YtDlpTuiApp
from yt_dlp_tui.events import DoneEvent, Event, LogEvent, PostProcessEvent, ProgressEvent
from yt_dlp_tui.presets import BUILTIN_PRESETS
from yt_dlp_tui.probe import ProbeResult, Tooling
from yt_dlp_tui.screens import main as main_screen_module
from yt_dlp_tui.screens import run as run_screen_module
from yt_dlp_tui.screens.main import MainScreen
from yt_dlp_tui.screens.run import MAX_LOG_LINE_CHARS, RunScreen

OFFLINE_TOOLING = Tooling(ytdlp=None, ffmpeg=None)

# Only ever rendered into `#title`: every RunScreen here is `autostart=False`,
# so nothing is spawned, and the guard fixture would refuse to spawn it anyway.
ARGV = ["true"]


async def _no_events() -> AsyncIterator[Event]:
  """An empty event stream, used for any argv that is not this interpreter."""
  for event in ():
    yield event


@pytest.fixture(autouse=True)
def _guarded_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
  """Makes "this file cannot spawn yt-dlp" structural. Returns the argvs the
  run screen asked to run, so a test can assert on what *would* have been
  spawned without spawning it."""
  spawned: list[list[str]] = []
  real_run = runner.run

  def guarded_run(argv: list[str]) -> AsyncIterator[Event]:
    spawned.append(list(argv))
    if argv[:1] == [sys.executable]:
      return real_run(argv)
    return _no_events()

  monkeypatch.setattr(run_screen_module, "run", guarded_run)
  return spawned


@pytest.fixture(autouse=True)
def _stub_probe(monkeypatch: pytest.MonkeyPatch) -> None:
  """`MainScreen` is always mounted underneath; keep its probe path offline."""

  async def fake_probe(url: str, *, ytdlp: str) -> ProbeResult:
    return ProbeResult(title="Stubbed Title", duration=90, extractor="Fake")

  monkeypatch.setattr(main_screen_module, "probe", fake_probe)


def _make_app() -> YtDlpTuiApp:
  return YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)


async def _screen(app: YtDlpTuiApp, pilot: Pilot) -> RunScreen:
  await app.push_screen(RunScreen(ARGV, autostart=False))
  await pilot.pause()
  screen = app.screen
  assert isinstance(screen, RunScreen)
  return screen


def _stage(screen: RunScreen) -> str:
  return str(screen.query_one("#stage", Static).content)


# Event rendering (the brief's six, driven through `apply_event` directly)


async def test_progress_event_updates_bar_and_stats() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(ProgressEvent(500, 1000, 2048.0, 5, 3, 10, "Track"))
    await pilot.pause()
    assert screen.query_one("#progress", ProgressBar).progress == 50.0
    counter = str(screen.query_one("#counter", Static).content)
    assert "3/10" in counter
    assert "2.0 KiB/s" in counter
    assert "Track" in str(screen.query_one("#title", Static).content)


async def test_postprocess_event_shows_indeterminate_stage() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(PostProcessEvent(status="started", processor="ExtractAudio"))
    await pilot.pause()
    assert "ExtractAudio" in _stage(screen)


async def test_log_events_accumulate() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(LogEvent("[download] a.mp4"))
    screen.apply_event(LogEvent("ERROR: nope", is_error=True))
    await pilot.pause()
    assert screen.log_lines == ["[download] a.mp4", "ERROR: nope"]
    # The list is what the tests read, but the widget is what the user reads:
    # assert both, or `apply_event` could stop writing to the log entirely and
    # every test here would still pass.
    assert screen.query_one("#log", RichLog).lines


async def test_done_event_success_message() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(DoneEvent(returncode=0))
    await pilot.pause()
    assert screen.finished is True
    assert "done" in _stage(screen).lower()


async def test_done_event_failure_message_mentions_exit_code() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(DoneEvent(returncode=2))
    await pilot.pause()
    assert "2" in _stage(screen)
    assert "failed" in _stage(screen).lower()


async def test_a_title_full_of_brackets_is_shown_literally() -> None:
  """Textual 8 parses content markup in `Static.update`, and yt-dlp titles are
  full of square brackets. With markup on, "[MV] Song" renders as " Song" (the
  bracket is eaten as a style tag) and an unbalanced one raises `MarkupError`
  from inside the driving worker, which kills the app. `.content` reads back
  the raw string either way, so this has to assert on the rendered line."""
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(ProgressEvent(1, 2, 0.0, 0, 1, 2, "[MV] Song [official]"))
    await pilot.pause()
    assert "[MV] Song [official]" in screen.query_one("#title", Static).render_line(0).text
    # An unbalanced tag must not raise out of apply_event either.
    screen.apply_event(ProgressEvent(1, 2, 0.0, 0, 1, 2, "[/close] tail"))
    await pilot.pause()
    assert "[/close] tail" in screen.query_one("#title", Static).render_line(0).text


async def test_zero_total_does_not_divide_by_zero() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(ProgressEvent(0, 0, 0.0, 0, 0, 0, ""))
    await pilot.pause()
    assert screen.query_one("#progress", ProgressBar).progress == 0.0


# A signalled child is not a failed download


async def test_signalled_exit_is_not_reported_as_a_yt_dlp_failure() -> None:
  """A negative returncode means the child was signalled (-15 == SIGTERM), not
  that yt-dlp exited -15. "failed (exit -15)" would tell the user their
  download broke when something merely stopped it."""
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(DoneEvent(returncode=-15))
    await pilot.pause()
    stage = _stage(screen)
    assert "stopped" in stage.lower()
    assert "15" in stage
    assert "-15" not in stage
    assert "failed" not in stage.lower()


async def test_a_done_event_after_a_user_cancel_reads_as_cancelled() -> None:
  """The cancel path normally ends the stream with no DoneEvent at all, but
  the child can lose the race and exit on its own SIGTERM first. Either way
  the user pressed `c`: cancelled, not broken."""
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.action_cancel()
    await pilot.pause()
    screen.apply_event(DoneEvent(returncode=-15))
    await pilot.pause()
    assert "cancel" in _stage(screen).lower()


# Log caps -- LogEvent.text can be ~1 MiB (runner does not truncate)


async def test_cancelling_a_finished_run_does_not_relabel_it(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Overwriting "done" with "cancelled" would tell the user their finished
  download was aborted.

  The spy is the point: asserting only that `#stage` still reads "done" would
  pass just as well if `c` were unbound entirely, which is a different (and
  untrue) claim. This pins that the binding really fires and that the *body*
  is what declines to do anything."""
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(DoneEvent(returncode=0))
    await pilot.pause()

    fired: list[bool] = []
    real_action_cancel = screen.action_cancel
    monkeypatch.setattr(
      screen, "action_cancel", lambda: (fired.append(True), real_action_cancel())[1]
    )

    await pilot.press("c")
    await pilot.pause()

    assert fired == [True], "the `c` binding never reached action_cancel"
    assert _stage(screen) == "done"
    assert screen.cancelled is False


async def test_a_successful_exit_after_a_cancel_still_reads_as_done() -> None:
  """The other side of the sibling test above. yt-dlp exits non-zero when it
  is actually interrupted, so exit 0 after a cancel means the download beat
  the SIGTERM and the file is on disk. "cancelled" would send the user looking
  for something they already have."""
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.action_cancel()
    await pilot.pause()
    screen.apply_event(DoneEvent(returncode=0))
    await pilot.pause()
    assert _stage(screen) == "done"


async def test_an_over_long_log_line_is_capped() -> None:
  """`runner._pump` drops a line past its 1 MiB stream limit, but the *tail*
  of that line still arrives as an ordinary line -- so a ~1 MiB `LogEvent` is
  reachable in normal operation, and handing it to a wrapping RichLog is
  thousands of rendered strips for one line."""
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(LogEvent("y" * 200_000))
    await pilot.pause()
    assert len(screen.log_lines) == 1
    assert len(screen.log_lines[0]) < MAX_LOG_LINE_CHARS + 100
    assert screen.log_lines[0].startswith("yyy")
    # The widget must get the capped text too, not just the list.
    assert len(screen.query_one("#log", RichLog).lines) < 30


async def test_the_log_line_list_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
  """`log_lines` grows for the life of a run; a long playlist emits tens of
  thousands of lines. Bound it, keeping the newest."""
  monkeypatch.setattr(run_screen_module, "MAX_LOG_LINES", 5)
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    for index in range(12):
      screen.apply_event(LogEvent(f"line {index}"))
    await pilot.pause()
    assert screen.log_lines == [f"line {index}" for index in range(7, 12)]


async def test_events_arriving_after_the_screen_is_popped_do_not_crash() -> None:
  """Textual prunes a screen's children *before* dispatching `Unmount`, so
  there is a window where the driving worker is alive and the widgets it
  renders into are gone. Unguarded, the first event in that window raises
  `NoMatches` inside the worker and takes the app down -- on the ordinary
  "press escape while a download is running" path."""
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    app.pop_screen()
    await pilot.pause()

    screen.apply_event(ProgressEvent(1, 2, 0.0, 0, 0, 0, "late"))
    screen.apply_event(LogEvent("late"))
    screen.apply_event(DoneEvent(returncode=0))
    await pilot.pause()

    assert app.is_running is True
    assert isinstance(app.screen, MainScreen)


# Wiring from the main screen


async def test_download_pushes_a_run_screen_with_the_current_command(
  _guarded_run: list[list[str]],
) -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    app.url = "https://example.com/v"
    await pilot.pause()
    expected = app.current_command()

    await pilot.press("enter")  # Input.Submitted -> MainScreen.action_download
    await pilot.pause()

    assert isinstance(app.screen, RunScreen)
    assert app.screen.argv == expected
    # And it actually started that command -- the guard fixture recorded the
    # argv instead of spawning it, since argv[0] is "yt-dlp".
    assert _guarded_run == [expected]


async def test_download_with_a_blank_url_says_so_instead_of_doing_nothing(
  _guarded_run: list[list[str]],
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Pressing enter on an empty box and getting no reaction at all reads as a
  broken key rather than a missing URL, so the no-op has to be audible."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    main_screen = app.screen
    notifications: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
      main_screen, "notify", lambda *args, **kwargs: notifications.append((args, kwargs))
    )
    app.url = "   "
    await pilot.pause()

    await pilot.press("enter")
    await pilot.pause()

    assert notifications, "expected a notify() call telling the user to enter a URL"

    assert app.screen is main_screen
    assert _guarded_run == []


# Labels that describe what the key/message actually does (final review minors)


async def test_escape_is_labelled_as_a_cancel_not_merely_as_going_back() -> None:
  """`escape` sets `cancelled = True` and pops, which unmounts the screen and
  stops the run -- it kills the download, exactly like `c`, and only differs
  in leaving the screen too. The footer said "back" and the README said "goes
  back", so the two keys read as different actions when they are not, and
  nothing warned that leaving throws the download away."""
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    # Read the description Textual resolved, not the raw BINDINGS tuple, so
    # this keeps holding if the entry ever becomes a `Binding` object.
    binding = screen._bindings.key_to_bindings["escape"][0]
    assert "cancel" in binding.description.lower()


async def test_the_failure_message_does_not_point_at_a_log_already_on_screen() -> None:
  """It read "press escape for the log". The log is the biggest widget on this
  screen and is already showing, and `escape` leaves for the main screen --
  the one place the log is not."""
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(DoneEvent(returncode=1))
    await pilot.pause()
    stage = _stage(screen)
    assert "failed (exit 1)" in stage
    assert "escape" not in stage.lower()
