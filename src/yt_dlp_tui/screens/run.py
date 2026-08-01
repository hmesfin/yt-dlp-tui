"""Live progress for a single job. A playlist is one job with an N-of-M counter.

Two responsibilities beyond drawing:

* **Nothing may outlive the TUI.** `runner.run` spawns with
  `start_new_session=True`, which puts yt-dlp (and the ffmpeg/aria2c it
  spawns) in their own session. The terminal's Ctrl-C no longer reaches them
  and neither does SIGHUP, so this screen is the only thing that will ever
  signal them.

  The runner's cleanup -- SIGTERM, a grace period, then SIGKILL -- runs
  *inside* the driving task's own cancellation, unwinding through
  `runner.run`'s `finally`. That makes a second `Task.cancel()` on that task
  actively destructive: it raises `CancelledError` at whichever `await` the
  cleanup is parked on, abandoning the escalation and leaving a child that
  shrugged off the SIGTERM alive forever. So the driver is a **plain
  `asyncio.Task`, deliberately not a Textual worker**. Textual's
  `WorkerManager.cancel_all()` fires from `_process_messages`' `finally` --
  which on the production `run_async` path happens *before* `Unmount` is
  dispatched (`app.py:2286-2300`), the opposite order from `run_test` -- and
  it calls `Task.cancel()` on every registered worker unconditionally.
  Anything registered there is therefore cancellable by Textual at a moment
  of its choosing, twice if the user already pressed `c`. A plain task is
  invisible to the manager, so `stop_run()` is the only thing that ever
  cancels the driver, exactly once, and it always awaits the result.

  `on_unmount` is what does that awaiting, and it is dispatched ahead of
  `Widget._on_unmount` because `MessagePump._get_dispatch_methods` walks the
  MRO subclass-first.

* **The runner does not truncate.** A `LogEvent` can carry ~1 MiB of text (an
  over-long line is dropped by `runner._pump`, but the tail of it still
  arrives as an ordinary line), so both the log widget and `log_lines` are
  capped here.
"""

import asyncio
import shlex
from typing import ClassVar

from textual._context import NoActiveAppError
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, ProgressBar, RichLog, Static
from textual.worker import WorkerFailed

from yt_dlp_tui.events import DoneEvent, Event, LogEvent, PostProcessEvent, ProgressEvent
from yt_dlp_tui.runner import run

MAX_LOG_LINES = 2000
# yt-dlp's own lines are well under a terminal width; anything past this is a
# pathological line we only need enough of to recognise.
MAX_LOG_LINE_CHARS = 500


def _human_rate(bytes_per_second: float) -> str:
  if bytes_per_second <= 0:
    return "—"
  units = ("B/s", "KiB/s", "MiB/s", "GiB/s")
  value = bytes_per_second
  for unit in units:
    if value < 1024 or unit == units[-1]:
      return f"{value:.1f} {unit}"
    value /= 1024
  return f"{value:.1f} {units[-1]}"


def _clip(text: str) -> str:
  """Bound one log line, saying so rather than silently swallowing the rest."""
  if len(text) <= MAX_LOG_LINE_CHARS:
    return text
  return f"{text[:MAX_LOG_LINE_CHARS]}… (+{len(text) - MAX_LOG_LINE_CHARS} more characters)"


class RunScreen(Screen):
  # No `Input` lives on this screen, so unlike MainScreen a bare letter key is
  # not claimed by a focused widget and reaches these bindings (`RichLog` takes
  # focus but only binds scroll keys and does not override
  # `check_consume_key`). No `priority=True` anywhere, per the owner's keymap
  # ruling.
  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("escape", "back", "back"),
    ("c", "cancel", "cancel"),
  ]

  def __init__(self, argv: list[str], autostart: bool = True) -> None:
    super().__init__()
    self.argv = argv
    self.autostart = autostart
    self.log_lines: list[str] = []
    self.finished = False
    self.cancelled = False
    self._run_task: asyncio.Task[None] | None = None
    self._stopping = False

  def compose(self) -> ComposeResult:
    with Vertical():
      # markup=False on every Static: Textual 8 parses content markup in
      # `Static.update`, and yt-dlp titles and output are full of square
      # brackets. "[MV] Title" silently renders as " Title" with markup on,
      # and an unbalanced bracket raises MarkupError from inside the worker.
      yield Static("", id="title", markup=False)
      yield Static("", id="counter", markup=False)
      yield ProgressBar(total=100.0, show_eta=False, id="progress")
      yield Static("starting…", id="stage", markup=False)
      yield RichLog(id="log", max_lines=MAX_LOG_LINES, wrap=True)
    yield Footer()

  def on_mount(self) -> None:
    self.query_one("#title", Static).update(shlex.join(self.argv))
    if self.autostart:
      # Not `self.run_worker(...)`: see the module docstring. The reference is
      # held so the loop cannot garbage-collect a task nobody is awaiting yet.
      self._run_task = asyncio.create_task(self._drive(), name="yt-dlp-tui run")
      self._run_task.add_done_callback(self._on_drive_done)

  def _on_drive_done(self, task: asyncio.Task[None]) -> None:
    """Surface a driver crash the way `run_worker(exit_on_error=True)` would.

    A plain task swallows its exception into "never retrieved" noise at GC
    time; `Worker._run` hands it to `App._handle_exception` instead, and a bug
    in `apply_event` should be just as loud here as it would be there.
    """
    if task.cancelled():
      return
    error = task.exception()
    if error is None:
      return
    try:
      app = self.app
    except NoActiveAppError:
      # The screen is already detached from the DOM, so there is no app left
      # to tell. Raising out of a done callback would only add asyncio noise.
      return
    # `_handle_exception` -> `_fatal_error` builds a `rich.traceback.Traceback`
    # with no explicit trace, which reads `sys.exc_info()`. `Worker._run` calls
    # it from inside `except Exception as error:` so that works there; a done
    # callback has no exception context, and the Traceback constructor raises
    # `ValueError: Value for 'trace' required if not called in except: block`
    # -- *before* `_fatal_error` reaches `_close_messages_no_wait()`. The app
    # then never shuts down, and asyncio's default handler paints the raw
    # ValueError over the running TUI. Re-raising here restores the exception
    # context the call needs, and keeps the real traceback the user should see.
    try:
      raise error
    except BaseException:  # noqa: BLE001 - deliberately re-caught to set sys.exc_info()
      app._handle_exception(WorkerFailed(error))

  async def _drive(self) -> None:
    stream = run(self.argv)
    try:
      async for event in stream:
        self.apply_event(event)
    finally:
      # Cancellation unwinds through the generator's own `finally`, which
      # stops the process group, so this is usually a no-op. It is not a no-op
      # when `apply_event` raises: without it a UI bug would leave the child
      # running with nobody left to read its pipes.
      await stream.aclose()

  async def stop_run(self) -> None:
    """Cancel the run and wait until the child's process group is gone.

    Bounded by the runner's own escalation (SIGTERM, then SIGKILL, every wait
    with a timeout), so this can take up to ~10s in the pathological case
    where something ignores SIGTERM. Milliseconds otherwise.

    Safe to call more than once (`c` then quit does exactly that): the second
    call waits on the first call's cleanup instead of cancelling into it.
    """
    task = self._run_task
    if task is None:
      return
    if not self._stopping:
      self._stopping = True
      task.cancel()
    # `asyncio.wait` and not `await task` / `gather(task)`. Both of those
    # propagate: `await task` re-raises the driver's CancelledError here, and
    # cancelling a coroutine parked on `gather(task)` cancels `task` itself --
    # which is precisely the second cancel this design exists to avoid, and it
    # happens for real when `cancel_all()` cancels the `c`-handler worker
    # while it is waiting here. `asyncio.wait` never touches what it waits on.
    await asyncio.wait({task})

  async def on_unmount(self) -> None:
    await self.stop_run()

  def apply_event(self, event: Event) -> None:
    """Render one event. Synchronous so tests can drive it without a subprocess."""
    if not self.is_running:
      # The screen's message loop has stopped, which Textual does *before* it
      # prunes the screen's children -- so between here and `on_unmount`
      # stopping the driver there is a window where the widgets below no
      # longer exist and `query_one` would raise `NoMatches` inside the
      # worker, taking the app down. Late events are simply dropped.
      return
    if isinstance(event, ProgressEvent):
      self.query_one("#progress", ProgressBar).update(progress=event.fraction * 100.0)
      if event.title:
        self.query_one("#title", Static).update(event.title)
      counter = f"item {event.index}/{event.count}" if event.count else ""
      rate = _human_rate(event.speed)
      eta = f"  ETA {event.eta}s" if event.eta else ""
      self.query_one("#counter", Static).update(f"{counter}   {rate}{eta}".strip())
    elif isinstance(event, PostProcessEvent):
      verb = "…" if event.status == "started" else " done"
      # postprocess events carry no percentage — stage text only
      self.query_one("#stage", Static).update(f"{event.processor}{verb}")
    elif isinstance(event, LogEvent):
      text = _clip(event.text)
      self.log_lines.append(text)
      if len(self.log_lines) > MAX_LOG_LINES:
        del self.log_lines[: len(self.log_lines) - MAX_LOG_LINES]
      self.query_one("#log", RichLog).write(text)
    elif isinstance(event, DoneEvent):
      self.finished = True
      self.query_one("#stage", Static).update(self._done_message(event))

  def _done_message(self, event: DoneEvent) -> str:
    # `ok` is checked before `cancelled` on purpose. Exit 0 after a cancel
    # means the child finished the download before the SIGTERM reached it --
    # yt-dlp exits non-zero when it is actually interrupted -- so the file is
    # on disk and "done" is the true statement. Reporting "cancelled" there
    # would send the user looking for a download they already have.
    if event.ok:
      return "done"
    if self.cancelled:
      return "cancelled"
    if event.returncode < 0:
      # asyncio reports a signalled child as the negated signal number. That
      # is not an exit status, and calling it a failure would blame yt-dlp for
      # something that stopped it.
      return f"stopped by signal {-event.returncode}"
    return f"failed (exit {event.returncode}) — press escape for the log"

  def action_cancel(self) -> None:
    if self.cancelled or self.finished:
      # Nothing to cancel once the run is over; replacing "done" with
      # "cancelled" would misreport a download the user already has.
      return
    self.cancelled = True
    self.query_one("#stage", Static).update("cancelling…")
    # Not awaited inline: killing the process group is bounded but not
    # instant, and an action blocks this screen's message pump while it runs.
    self.run_worker(self._cancel(), group="cancel")

  async def _cancel(self) -> None:
    await self.stop_run()
    if self.is_running:
      self.query_one("#stage", Static).update("cancelled")

  def action_back(self) -> None:
    # Popping unmounts this screen, and `on_unmount` stops the run: there is
    # nowhere left to render it, so leaving it going would be an invisible
    # download the user cannot see, cancel, or find.
    #
    # Deliberately not awaited. `pop_screen` returns immediately and the kill
    # finishes in the background, so MainScreen is usable again while a
    # stubborn child is still being shot -- the user could start a second
    # download in that window. That is safe (each RunScreen owns its own
    # generator and its own process group, so the runner's non-re-entrancy
    # constraint still holds) and the alternative is freezing the UI for up to
    # ~10s on the way back to a screen the user asked for.
    self.cancelled = True
    self.app.pop_screen()
