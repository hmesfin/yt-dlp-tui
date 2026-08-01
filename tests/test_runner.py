import asyncio
import contextlib
import gc
import os
import signal
import sys
import time

import pytest

from yt_dlp_tui import runner
from yt_dlp_tui.events import DoneEvent, Event, LogEvent, ProgressEvent
from yt_dlp_tui.runner import run

GOOD = 'PROG:{"b":5,"t":10,"s":1.0,"e":1,"i":1,"n":2,"title":"t"}'
# A grandchild that inherits the pipes, as ffmpeg and aria2c do under yt-dlp.
_SLEEPER = "import time; time.sleep(30)"


def _emit(*lines: str, code: int = 0, stderr: str = "") -> list[str]:
  body = "".join(f"print({line!r}, flush=True)\n" for line in lines)
  if stderr:
    body += f"import sys; print({stderr!r}, file=sys.stderr, flush=True)\n"
  body += f"raise SystemExit({code})\n"
  return [sys.executable, "-c", body]


def _spawner(then: str, *, escapes: bool = False) -> list[str]:
  """A child that spawns a pipe-inheriting grandchild, announces both pids as
  `up <child> <grandchild>`, and then does `then`.

  With `escapes`, the grandchild gets its own session, so it survives a signal
  sent to the child's process group and keeps the inherited pipes open.
  """
  body = (
    "import os, subprocess, sys\n"
    f"kid = subprocess.Popen([sys.executable, '-c', {_SLEEPER!r}], "
    f"start_new_session={escapes})\n"
    "print(f'up {os.getpid()} {kid.pid}', flush=True)\n"
    f"{then}\n"
  )
  return [sys.executable, "-c", body]


def _pids(event: Event) -> tuple[int, int]:
  assert isinstance(event, LogEvent) and event.text.startswith("up ")
  child, grandchild = event.text.split()[1:3]
  return int(child), int(grandchild)


async def _collect(argv: list[str]) -> list[Event]:
  return [ev async for ev in run(argv)]


def _wait_until_gone(pid: int, timeout: float = 5.0) -> None:
  """Block until `pid` is neither alive nor a reaped-pending zombie."""
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    try:
      os.kill(pid, 0)
    except ProcessLookupError:
      return
    time.sleep(0.02)
  raise AssertionError(f"child pid {pid} is still alive")


async def test_yields_progress_then_done() -> None:
  events = await _collect(_emit(GOOD))
  assert isinstance(events[0], ProgressEvent)
  assert isinstance(events[-1], DoneEvent)
  assert events[-1].ok is True


async def test_plain_stdout_becomes_log_events() -> None:
  events = await _collect(_emit("[download] Destination: a.mp4"))
  assert any(isinstance(e, LogEvent) and "Destination" in e.text for e in events)


async def test_stderr_is_captured_and_flagged() -> None:
  events = await _collect(_emit("ok", stderr="ERROR: nope"))
  assert any(isinstance(e, LogEvent) and e.is_error and "nope" in e.text for e in events)


async def test_nonzero_exit_is_reported() -> None:
  events = await _collect(_emit("boom", code=3))
  assert events[-1] == DoneEvent(returncode=3)


async def test_exactly_one_done_event_last() -> None:
  events = await _collect(_emit(GOOD, GOOD, "noise"))
  assert sum(isinstance(e, DoneEvent) for e in events) == 1
  assert isinstance(events[-1], DoneEvent)


async def test_missing_binary_yields_error_log_and_done() -> None:
  events = await _collect(["/nonexistent/yt-dlp-xyz"])
  assert isinstance(events[-1], DoneEvent)
  assert events[-1].ok is False
  assert any(isinstance(e, LogEvent) and e.is_error for e in events)


async def test_closing_the_iterator_terminates_the_child_promptly() -> None:
  # A child that never exits on its own: if the finally block fails to
  # terminate it, aclose() blocks forever and wait_for raises TimeoutError.
  argv = [
    sys.executable,
    "-c",
    "import os, time\nprint(f'up {os.getpid()}', flush=True)\nwhile True: time.sleep(0.05)\n",
  ]
  agen = run(argv)
  first = await agen.__anext__()
  assert isinstance(first, LogEvent) and first.text.startswith("up ")
  pid = int(first.text.split()[1])
  await asyncio.wait_for(agen.aclose(), timeout=10)
  _wait_until_gone(pid)


BURST = 4000
PAD = "x" * 60


def _burst_argv(count: int) -> list[str]:
  body = (
    "import sys\n"
    f"for i in range({count}):\n"
    f"  print(f'OUT {{i}} {PAD}', flush=True)\n"
    f"  print(f'ERR {{i}} {PAD}', file=sys.stderr, flush=True)\n"
  )
  return [sys.executable, "-c", body]


async def test_large_interleaved_burst_loses_no_lines() -> None:
  # ~280 KiB on each pipe, far past the 64 KiB kernel pipe buffer: if either
  # stream stops being drained the child blocks on write and this deadlocks.
  events = await asyncio.wait_for(_collect(_burst_argv(BURST)), timeout=60)
  assert sum(isinstance(e, DoneEvent) for e in events) == 1
  assert isinstance(events[-1], DoneEvent)
  assert events[-1].ok is True
  out = [e.text for e in events if isinstance(e, LogEvent) and not e.is_error]
  err = [e.text for e in events if isinstance(e, LogEvent) and e.is_error]
  assert out == [f"OUT {i} {PAD}" for i in range(BURST)]
  assert err == [f"ERR {i} {PAD}" for i in range(BURST)]


async def test_oversized_line_does_not_stall_the_stream() -> None:
  # A single line longer than the stream limit makes StreamReader.readline
  # raise; an unhandled raise kills the reader and hangs proc.wait() forever.
  huge = runner._STREAM_LIMIT * 2
  argv = [
    sys.executable,
    "-c",
    f"print('X' * {huge}, flush=True)\nprint('tail', flush=True)\n",
  ]
  events = await asyncio.wait_for(_collect(argv), timeout=30)
  assert any(isinstance(e, LogEvent) and e.text == "tail" for e in events)
  assert sum(isinstance(e, DoneEvent) for e in events) == 1
  assert isinstance(events[-1], DoneEvent)
  assert events[-1].ok is True


async def test_oversized_line_followed_by_more_output_does_not_deadlock() -> None:
  # Regression: if the over-long line kills the reader, the child then blocks
  # writing to a pipe nobody drains and proc.wait() never returns.
  huge = runner._STREAM_LIMIT * 2
  body = (
    f"print('X' * {huge}, flush=True)\n"
    "for i in range(20000):\n"
    "  print(f'L {i} ' + 'y' * 60, flush=True)\n"
  )
  events = await asyncio.wait_for(_collect([sys.executable, "-c", body]), timeout=60)
  texts = [e.text for e in events if isinstance(e, LogEvent)]
  assert f"L 19999 {'y' * 60}" in texts
  assert sum(isinstance(e, DoneEvent) for e in events) == 1
  assert isinstance(events[-1], DoneEvent)
  assert events[-1].ok is True


async def test_aclose_kills_a_child_that_ignores_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(runner, "_TERM_TIMEOUT", 0.2)
  body = (
    "import os, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print(f'up {os.getpid()}', flush=True)\n"
    "while True: time.sleep(0.05)\n"
  )
  agen = run([sys.executable, "-c", body])
  first = await agen.__anext__()
  pid = int(first.text.split()[1])
  await asyncio.wait_for(agen.aclose(), timeout=10)
  _wait_until_gone(pid)


async def test_reader_failure_is_surfaced_and_still_ends_with_one_done(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  # If a reader dies for an unforeseen reason we must not wait on an exit that
  # can never come: stop the child, report, and still close with one DoneEvent.
  async def boom(
    stream: asyncio.StreamReader,
    queue: asyncio.Queue[Event | None],
    *,
    is_error: bool,
  ) -> None:
    raise RuntimeError("reader exploded")

  monkeypatch.setattr(runner, "_pump", boom)
  argv = [sys.executable, "-c", "import time\nprint('hi', flush=True)\ntime.sleep(30)\n"]
  events = await asyncio.wait_for(_collect(argv), timeout=15)
  assert any(isinstance(e, LogEvent) and e.is_error and "exploded" in e.text for e in events)
  assert sum(isinstance(e, DoneEvent) for e in events) == 1
  assert isinstance(events[-1], DoneEvent)


async def test_grandchild_holding_the_pipes_still_yields_one_done() -> None:
  # yt-dlp hands its stdout/stderr to ffmpeg and aria2c, so the pipes can stay
  # open long after yt-dlp itself is gone. EOF alone must not gate the stream:
  # without a backstop this yields no DoneEvent at all and hangs forever.
  events = await asyncio.wait_for(_collect(_spawner("raise SystemExit(0)")), timeout=15)
  assert any(isinstance(e, LogEvent) and e.text.startswith("up ") for e in events)
  assert sum(isinstance(e, DoneEvent) for e in events) == 1
  assert isinstance(events[-1], DoneEvent)
  assert events[-1].ok is True


async def test_aclose_is_prompt_when_a_grandchild_holds_the_pipes() -> None:
  # proc.wait() only resolves once every inherited pipe is closed, so waiting
  # on it burns the whole SIGTERM grace period on an already-dead child. The
  # holder here has its own session, so killing the child's group cannot free
  # the pipes for us -- only watching proc.returncode gets this right.
  agen = run(_spawner("import time; time.sleep(30)", escapes=True))
  _, holder_pid = _pids(await agen.__anext__())
  try:
    started = time.monotonic()
    await asyncio.wait_for(agen.aclose(), timeout=30)
    elapsed = time.monotonic() - started
    assert elapsed < runner._TERM_TIMEOUT / 2, f"aclose() took {elapsed:.1f}s"
  finally:
    with contextlib.suppress(ProcessLookupError):
      os.kill(holder_pid, signal.SIGKILL)


async def test_aclose_kills_the_whole_process_group() -> None:
  # An orphaned ffmpeg would keep writing the output file after a cancel.
  agen = run(_spawner("import time; time.sleep(30)"))
  child_pid, grandchild_pid = _pids(await agen.__anext__())
  await asyncio.wait_for(agen.aclose(), timeout=30)
  _wait_until_gone(child_pid)
  _wait_until_gone(grandchild_pid)


async def test_child_that_closes_its_pipes_but_lingers_still_completes(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(runner, "_EXIT_TIMEOUT", 0.3)
  body = "import os, time\nprint('bye', flush=True)\nos.close(1)\nos.close(2)\ntime.sleep(30)\n"
  events = await asyncio.wait_for(_collect([sys.executable, "-c", body]), timeout=15)
  assert sum(isinstance(e, DoneEvent) for e in events) == 1
  assert isinstance(events[-1], DoneEvent)
  assert events[-1].ok is False


async def test_early_close_leaves_no_unretrieved_asyncio_noise(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  # Closing early while a reader has failed must still retrieve that reader's
  # exception, or asyncio logs "Task exception was never retrieved" at GC time.
  async def one_then_boom(
    stream: asyncio.StreamReader,
    queue: asyncio.Queue[Event | None],
    *,
    is_error: bool,
  ) -> None:
    queue.put_nowait(LogEvent(text="hi", is_error=is_error))
    raise RuntimeError("reader exploded")

  monkeypatch.setattr(runner, "_pump", one_then_boom)
  loop = asyncio.get_running_loop()
  captured: list[dict[str, object]] = []
  loop.set_exception_handler(lambda _loop, context: captured.append(context))
  try:
    agen = run([sys.executable, "-c", "import time\ntime.sleep(30)\n"])
    await agen.__anext__()
    await asyncio.wait_for(agen.aclose(), timeout=30)
    del agen
    gc.collect()
    await asyncio.sleep(0.05)
  finally:
    loop.set_exception_handler(None)
  assert captured == [], [str(context.get("message")) for context in captured]


async def test_aclose_leaves_no_live_helper_tasks() -> None:
  # The reader and watchdog must be finished, not merely cancel()-ed, by the
  # time aclose() returns: a task left mid-cancellation is destroyed pending if
  # the consumer's loop closes straight after, which is exactly what a TUI does
  # on quit. asyncio.all_tasks() lists only tasks that are not done yet.
  # The pipe holder here escapes into its own session, so the readers stay
  # blocked through the kill and cancellation is the only thing that ends them.
  before = asyncio.all_tasks()
  agen = run(_spawner("import time; time.sleep(30)", escapes=True))
  _, holder_pid = _pids(await agen.__anext__())
  try:
    # Awaited bare, not via wait_for: wait_for would run aclose() in a task of
    # its own, handing the loop the very scheduling turn this test denies it.
    await agen.aclose()
    leftover = {task.get_coro().__qualname__ for task in asyncio.all_tasks() - before}
    leftover.discard("test_aclose_leaves_no_live_helper_tasks")
    assert leftover == set(), leftover
  finally:
    with contextlib.suppress(ProcessLookupError):
      os.kill(holder_pid, signal.SIGKILL)


async def test_empty_argv_is_reported_not_raised() -> None:
  events = await _collect([])
  assert events == [events[0], DoneEvent(returncode=127)]
  assert isinstance(events[0], LogEvent) and events[0].is_error


async def test_nul_byte_in_argv_is_reported_not_raised() -> None:
  events = await _collect([sys.executable, "-c", "print('x')\0"])
  assert isinstance(events[-1], DoneEvent)
  assert events[-1].returncode == 127
  assert any(isinstance(e, LogEvent) and e.is_error for e in events)


async def test_events_stream_before_the_child_exits() -> None:
  # The UI needs progress as it happens, not one batch at exit.
  argv = [
    sys.executable,
    "-c",
    "import time\nprint('early', flush=True)\ntime.sleep(30)\n",
  ]
  agen = run(argv)
  try:
    first = await asyncio.wait_for(agen.__anext__(), timeout=5)
    assert isinstance(first, LogEvent) and first.text == "early"
  finally:
    await asyncio.wait_for(agen.aclose(), timeout=10)
