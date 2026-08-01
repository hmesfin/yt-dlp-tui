"""Process-lifetime coverage for the run screen: no download may outlive the
screen that shows it, or the app that shows the screen.

Split out of `test_run_screen.py` (which keeps the pure event-rendering tests)
because these are the slow ones and because they answer a different question:
not "did the widget update" but "is the child process actually dead".

Every assertion here is on a pid, via `os.kill(pid, 0)` until
`ProcessLookupError`, the technique `tests/test_runner.py` established. UI text
is never the evidence -- a screen can say "cancelled" over a yt-dlp that is
still downloading, and the runner spawns with `start_new_session=True`, so
nothing else in the system (not the terminal's Ctrl-C, not SIGHUP) will ever
signal that child.

**Two of these drive `App.run_async`, not `App.run_test`, and that is the
point.** The two harnesses tear down in opposite orders:

* `run_test` (`textual/app.py:2213`) calls `await app._shutdown()` from the
  *test* task while the app task is still pending, so `Unmount` fires
  **before** `_process_messages`' `finally: workers.cancel_all()`.
* `run_async` (`app.py:2286-2300`) awaits `_process_messages` -- which returns
  only *after* its own `finally: workers.cancel_all()` -- and only then runs
  `await asyncio.shield(app._shutdown())`, so `Unmount` fires **after**.

Production uses `run_async`. A suite that only drives `run_test` cannot see a
teardown bug that depends on that ordering, and the first version of this
file's orphan test could not: it passed against a `RunScreen` that orphaned
the child on 3/3 real runs.
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
from textual.widgets import ProgressBar, Static

from yt_dlp_tui import runner
from yt_dlp_tui.app import YtDlpTuiApp
from yt_dlp_tui.events import Event
from yt_dlp_tui.presets import BUILTIN_PRESETS
from yt_dlp_tui.probe import ProbeResult, Tooling
from yt_dlp_tui.screens import main as main_screen_module
from yt_dlp_tui.screens import run as run_screen_module
from yt_dlp_tui.screens.main import MainScreen
from yt_dlp_tui.screens.run import RunScreen

OFFLINE_TOOLING = Tooling(ytdlp=None, ffmpeg=None)

# A child that announces its own pid and then refuses to end on its own, so a
# cancel that fails to kill it is observable rather than a race.
ANNOUNCE_AND_SLEEP = (
  "import os, time\nprint(f'up {os.getpid()}', flush=True)\nwhile True: time.sleep(0.05)\n"
)

# The same, but deaf to SIGTERM, so the runner has to sit out its whole grace
# period and escalate to SIGKILL -- many event-loop turns instead of one. That
# is what makes an incomplete teardown visible: with an ordinary child SIGTERM
# lands in about a millisecond and almost any teardown looks correct. yt-dlp
# installs its own SIGTERM handling and a wedged ffmpeg behaves the same way,
# so this is the realistic stubborn child, not a contrived one.
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
  """Same structural guard as `test_run_screen.py`: the run screen can only
  ever spawn this interpreter, never a real `yt-dlp`."""
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


def _stage(screen: RunScreen) -> str:
  return str(screen.query_one("#stage", Static).content)


def _child(body: str) -> list[str]:
  return [sys.executable, "-c", body]


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
  """Poll with `time.sleep` only.

  Deliberately never `await`s: inside an async test the event loop is this
  coroutine, so a blocking sleep denies it turns. Anything teardown merely
  *scheduled* cannot progress while this runs, and a kill that was requested
  but not awaited shows up as a live pid instead of being masked.
  """
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if not _alive(pid):
      return
    time.sleep(0.02)
  raise AssertionError(f"child pid {pid} is still alive")


def _reap(pid: int) -> None:
  """Belt and braces: never let a failing test leak a sleeping child."""
  with contextlib.suppress(ProcessLookupError, PermissionError):
    os.killpg(pid, signal.SIGKILL)
  with contextlib.suppress(ProcessLookupError):
    os.kill(pid, signal.SIGKILL)


async def _screen(app: YtDlpTuiApp, pilot: Pilot, argv: list[str]) -> RunScreen:
  await app.push_screen(RunScreen(argv, autostart=True))
  await pilot.pause()
  screen = app.screen
  assert isinstance(screen, RunScreen)
  return screen


# In-app teardown: the run stops when the screen goes away


async def test_a_real_run_streams_events_into_the_widgets() -> None:
  """`test_run_screen.py` drives `apply_event` by hand, so all of it would pass
  even if the driver never consumed the runner. This spawns a real (hermetic)
  child through the real runner and asserts the widgets moved."""
  body = (
    'print(\'PROG:{"b":5,"t":10,"s":1024.0,"e":2,"i":1,"n":2,"title":"T"}\', flush=True)\n'
    "print('[download] Destination: a.mp4', flush=True)\n"
  )
  app = _make_app()
  async with app.run_test() as pilot:
    screen = await _screen(app, pilot, _child(body))
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
      screen = await _screen(app, pilot, _child(ANNOUNCE_AND_SLEEP))
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
      screen = await _screen(app, pilot, _child(ANNOUNCE_AND_SLEEP))
      pid = await _child_pid(screen, pilot)

      await pilot.press("escape")
      await _settle(pilot, lambda: not _alive(pid))

      _assert_gone(pid)
      assert isinstance(app.screen, MainScreen)
  finally:
    if pid is not None:
      _reap(pid)


async def test_exiting_the_app_leaves_no_orphaned_download(monkeypatch: pytest.MonkeyPatch) -> None:
  """The `run_test` half of the app-exit guarantee. See the module docstring
  for why this one alone is not enough, and `_assert_gone` for why the
  assertion never awaits."""
  monkeypatch.setattr(runner, "_TERM_TIMEOUT", 0.5)
  app = _make_app()
  pid: int | None = None
  try:
    async with app.run_test() as pilot:
      screen = await _screen(app, pilot, _child(SIGTERM_DEAF))
      pid = await _child_pid(screen, pilot)
    # The app has fully shut down here: the child must already be gone, not
    # merely scheduled for cancellation.
    _assert_gone(pid, timeout=0.5)
  finally:
    if pid is not None:
      _reap(pid)


# Production teardown: the same guarantee through App.run_async


async def _run_async_until_quit(app: YtDlpTuiApp, argv: list[str], *, press_c: bool) -> int:
  """Drive a whole `App.run_async` lifecycle and return the child's pid.

  This is the production entry point (`App.run` is `asyncio.run(run_async())`),
  and it is the only way to exercise the `cancel_all()`-then-`Unmount` shutdown
  order described in the module docstring.
  """
  found: dict[str, int] = {}

  async def auto_pilot(pilot: Pilot) -> None:
    await pilot.pause()
    screen = await _screen(app, pilot, argv)
    found["pid"] = await _child_pid(screen, pilot)
    if press_c:
      await pilot.press("c")
      # Quit *during* the SIGTERM grace period, which is the window where a
      # second cancel truncates the kill. `_TERM_TIMEOUT` is 1.0s for these
      # tests and the child ignores SIGTERM, so the cleanup is guaranteed to
      # still be running 0.15s in -- a ~7x margin, and erring either way only
      # makes the test less discriminating, never flaky.
      await asyncio.sleep(0.15)
    app.exit()

  await app.run_async(headless=True, auto_pilot=auto_pilot)
  return found["pid"]


async def test_quitting_a_real_app_run_leaves_no_orphaned_download(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(runner, "_TERM_TIMEOUT", 1.0)
  app = _make_app()
  pid: int | None = None
  try:
    pid = await _run_async_until_quit(app, _child(SIGTERM_DEAF), press_c=False)
    _assert_gone(pid, timeout=0.5)
  finally:
    if pid is not None:
      _reap(pid)


async def test_cancelling_then_quitting_leaves_no_orphaned_download(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """`c` then `ctrl+q` is the ordinary way a user aborts a stuck download, and
  it is the harder case: the kill is already in flight when the app starts
  shutting down, so anything that cancels into it a second time abandons the
  escalation to SIGKILL and orphans the child."""
  monkeypatch.setattr(runner, "_TERM_TIMEOUT", 1.0)
  app = _make_app()
  pid: int | None = None
  try:
    pid = await _run_async_until_quit(app, _child(SIGTERM_DEAF), press_c=True)
    _assert_gone(pid, timeout=0.5)
  finally:
    if pid is not None:
      _reap(pid)
