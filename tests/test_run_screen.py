"""Coverage for the run screen: event rendering, the log cap, cancellation, and
the guarantee that no download outlives the TUI.

Two things here are load-bearing:

1. **Nothing may spawn a real `yt-dlp` or touch the network.** This is the
   first screen that runs subprocesses on purpose, so the guarantee is
   structural rather than per-test discipline (an earlier round of
   `test_main_screen.py` leaked two real `yt-dlp -J` invocations out of one
   un-guarded keystroke). The autouse `_guarded_run` fixture replaces the
   `run` name `screens/run.py` resolves at call time; it only delegates to the
   real runner when `argv[0]` is this interpreter, and records everything
   else. `_stub_probe` does the same for the `MainScreen` underneath.

2. **Every "the run was cancelled" assertion checks the process, not the UI
   text.** `tests/test_runner.py` established the technique: the child prints
   its pid and the test polls `os.kill(pid, 0)` until `ProcessLookupError`. A
   test that only asserts `#stage` says "cancelled" passes just as happily on
   code that leaves an orphaned yt-dlp downloading in the background -- and
   the runner spawns with `start_new_session=True`, so nothing else (not the
   terminal's Ctrl-C, not SIGHUP) will ever signal that child.

Textual 8.2.8, same as the other screen tests: `app.query_one` never searches
a pushed screen (use `app.screen.query_one`), and `Static` exposes what
`.update()` stored as `.content`, not `.renderable`.
"""

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Callable

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

# Only ever rendered into `#title`; every test that uses it passes
# `autostart=False`, so nothing is spawned. The guard fixture below would
# refuse to spawn it anyway.
ARGV = ["true"]

# A child that announces its own pid and then refuses to end on its own, so a
# cancel that fails to kill it is observable rather than a race.
ANNOUNCE_AND_SLEEP = (
  "import os, time\nprint(f'up {os.getpid()}', flush=True)\nwhile True: time.sleep(0.05)\n"
)

# The same, but deaf to SIGTERM -- the runner then has to sit out its whole
# SIGTERM grace period and escalate to SIGKILL, which takes many event-loop
# turns instead of one. yt-dlp installs its own SIGTERM handling, so this is
# the realistic shape of a stubborn child rather than a contrived one.
SIGTERM_DEAF = (
  "import os, signal, time\n"
  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
  "print(f'up {os.getpid()}', flush=True)\n"
  "while True: time.sleep(0.05)\n"
)


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


def _child(body: str) -> list[str]:
  return [sys.executable, "-c", body]


async def _screen(
  app: YtDlpTuiApp,
  pilot: Pilot,
  argv: list[str] | None = None,
  *,
  autostart: bool = False,
) -> RunScreen:
  await app.push_screen(RunScreen(ARGV if argv is None else argv, autostart=autostart))
  await pilot.pause()
  screen = app.screen
  assert isinstance(screen, RunScreen)
  return screen


def _stage(screen: RunScreen) -> str:
  return str(screen.query_one("#stage", Static).content)


async def _settle(pilot: Pilot, predicate: Callable[[], bool], timeout: float = 15.0) -> None:
  """Pump the app until `predicate` holds, or fail."""
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if predicate():
      return
    await pilot.pause()
    await asyncio.sleep(0.02)
  raise AssertionError("timed out waiting for the app to settle")


def _alive(pid: int) -> bool:
  try:
    os.kill(pid, 0)
  except ProcessLookupError:
    return False
  return True


async def _child_pid(screen: RunScreen, pilot: Pilot) -> int:
  await _settle(pilot, lambda: any(line.startswith("up ") for line in screen.log_lines))
  line = next(line for line in screen.log_lines if line.startswith("up "))
  pid = int(line.split()[1])
  assert _alive(pid), "the child was already gone before the test did anything"
  return pid


def _assert_gone(pid: int, timeout: float = 5.0) -> None:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if not _alive(pid):
      return
    time.sleep(0.02)
  raise AssertionError(f"child pid {pid} is still alive")


def _reap(pid: int) -> None:
  """Belt and braces: never let a failing test leak a sleeping child."""
  with contextlib.suppress(ProcessLookupError):
    os.killpg(pid, signal.SIGKILL)
  with contextlib.suppress(ProcessLookupError):
    os.kill(pid, signal.SIGKILL)


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


async def test_cancelling_a_finished_run_does_not_relabel_it() -> None:
  """`c` still reaches its binding once the run is over. Overwriting "done"
  with "cancelled" would tell the user their finished download was aborted."""
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot)
    screen.apply_event(DoneEvent(returncode=0))
    await pilot.pause()

    await pilot.press("c")
    await pilot.pause()

    assert _stage(screen) == "done"
    assert screen.cancelled is False


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


# A real run: events reach the widgets, and cancellation kills the process


async def test_a_real_run_streams_events_into_the_widgets() -> None:
  """Everything above drives `apply_event` by hand, so all of it would pass
  even if the worker never consumed the runner. This spawns a real (hermetic)
  child through the real runner and asserts the widgets moved."""
  body = (
    'print(\'PROG:{"b":5,"t":10,"s":1024.0,"e":2,"i":1,"n":2,"title":"T"}\', flush=True)\n'
    "print('[download] Destination: a.mp4', flush=True)\n"
  )
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot, _child(body), autostart=True)
    await _settle(pilot, lambda: screen.finished)
    assert screen.query_one("#progress", ProgressBar).progress == 50.0
    assert any("Destination" in line for line in screen.log_lines)
    assert "done" in _stage(screen).lower()


async def test_c_cancels_the_run_and_kills_the_child() -> None:
  """The keymap ruling: no `Input` on this screen, so a bare `c` reaches its
  binding. Proven with a real keypress and a dead pid, not by `#stage`."""
  app = _make_app()
  pid: int | None = None
  try:
    async with app.run_test() as pilot:
      screen = await _screen(app, pilot, _child(ANNOUNCE_AND_SLEEP), autostart=True)
      pid = await _child_pid(screen, pilot)

      await pilot.press("c")
      await _settle(pilot, lambda: not _alive(pid))

      _assert_gone(pid)
      assert "cancel" in _stage(screen).lower()
      assert app.is_running is True  # cancelling a run does not quit the app
  finally:
    if pid is not None:
      _reap(pid)


async def test_escape_goes_back_to_the_main_screen_and_stops_the_run() -> None:
  app = _make_app()
  pid: int | None = None
  try:
    async with app.run_test() as pilot:
      screen = await _screen(app, pilot, _child(ANNOUNCE_AND_SLEEP), autostart=True)
      pid = await _child_pid(screen, pilot)

      await pilot.press("escape")
      await _settle(pilot, lambda: not _alive(pid))

      _assert_gone(pid)
      assert isinstance(app.screen, MainScreen)
  finally:
    if pid is not None:
      _reap(pid)


async def test_exiting_the_app_leaves_no_orphaned_download(monkeypatch: pytest.MonkeyPatch) -> None:
  """The responsibility the runner's `start_new_session=True` created and
  handed to this screen: the child is in its own session, so the terminal's
  Ctrl-C never reaches it and SIGHUP never reaches it. If the TUI exits
  without closing the run, the download survives as an orphan holding the
  network and writing to the output file.

  Two details make this bite where the obvious version does not. Textual's own
  teardown (`Widget._on_unmount` -> `WorkerManager.cancel_node`) does call
  `Task.cancel()` on the driving worker, and a cancelled run kills its process
  group on the way out -- but only if the loop keeps handing it turns. With an
  ordinary child SIGTERM lands in ~1ms and the incidental turns during
  shutdown suffice, so deleting the screen's teardown changes nothing. So the
  child here **ignores SIGTERM** (yt-dlp installs its own handling; a wedged
  ffmpeg behaves the same), forcing the runner through its grace period and on
  to SIGKILL -- many loop turns, not one. And the assertion below uses only
  `time.sleep`, never `await`, so the loop gets **no further turns**: a kill
  that was requested but not awaited shows up as a live pid.
  """
  monkeypatch.setattr(runner, "_TERM_TIMEOUT", 0.5)
  app = _make_app()
  pid: int | None = None
  try:
    async with app.run_test() as pilot:
      screen = await _screen(app, pilot, _child(SIGTERM_DEAF), autostart=True)
      pid = await _child_pid(screen, pilot)
    # The app has fully shut down here: the child must already be gone, not
    # merely scheduled for cancellation.
    _assert_gone(pid, timeout=0.5)
  finally:
    if pid is not None:
      _reap(pid)


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


async def test_download_with_a_blank_url_does_not_push_a_run_screen(
  _guarded_run: list[list[str]],
) -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    main_screen = app.screen
    app.url = "   "
    await pilot.pause()

    await pilot.press("enter")
    await pilot.pause()

    assert app.screen is main_screen
    assert _guarded_run == []
