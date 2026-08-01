"""Spawn yt-dlp and stream its output as typed events.

The only module in the package that touches subprocesses.

yt-dlp routinely spawns ffmpeg and aria2c, which inherit its stdout and stderr.
Two consequences shape this module: pipe EOF cannot be trusted to mean "the run
is over", and `proc.wait()` cannot be trusted to mean "the child has exited" —
asyncio resolves it only once every inherited pipe has closed as well.
"""

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator, Callable

from yt_dlp_tui.events import DoneEvent, Event, LogEvent, parse_line

# Per-stream read buffer. A line longer than this cannot be returned by
# readline(), so keep it far above anything yt-dlp plausibly prints.
_STREAM_LIMIT = 1024 * 1024
# Grace period after SIGTERM before we escalate to SIGKILL.
_TERM_TIMEOUT = 5.0
# Upper bound on reaping a SIGKILLed group, so cleanup can never block forever.
_KILL_TIMEOUT = 5.0
# How long to keep waiting for pipe EOF once the child itself has exited.
# Anything still holding the pipes by then is a grandchild we outlived.
_EOF_GRACE = 1.0
# How long a child may linger after its output has ended before we stop it.
_EXIT_TIMEOUT = 10.0
# Exit and process-group state have to be polled; asyncio exposes no callback
# for "the child was reaped" that is independent of the pipes.
_POLL_INTERVAL = 0.02
# Reported when a child cannot be reaped at all (an unkillable D-state process).
_UNKNOWN_RETURNCODE = -1


async def _poll_until(predicate: Callable[[], bool], timeout: float) -> bool:
  """Wait for `predicate` to hold, returning False if `timeout` runs out."""
  loop = asyncio.get_running_loop()
  deadline = loop.time() + timeout
  while not predicate():
    if loop.time() >= deadline:
      return False
    await asyncio.sleep(_POLL_INTERVAL)
  return True


def _pgid(proc: asyncio.subprocess.Process) -> int:
  """The child's process group id.

  `start_new_session=True` makes the child a session and group leader, so its
  pgid is its pid. Deriving it rather than calling os.getpgid() keeps it usable
  after the child has been reaped, which is exactly when a surviving ffmpeg
  still needs signalling.
  """
  return proc.pid


def _group_alive(pgid: int) -> bool:
  """True while any process remains in the child's group."""
  if pgid <= 0 or pgid == os.getpgid(0):
    return False
  try:
    os.killpg(pgid, 0)
  except (ProcessLookupError, PermissionError):
    return False
  return True


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
  """Signal the child's whole process group, so ffmpeg and aria2c go down too."""
  pgid = _pgid(proc)
  if pgid > 0 and pgid != os.getpgid(0):
    try:
      os.killpg(pgid, sig)
    except ProcessLookupError:
      pass  # Nothing left in the group.
    except PermissionError:
      # Cannot signal the group; fall back to the child alone.
      with contextlib.suppress(ProcessLookupError):
        proc.send_signal(sig)
    return
  # No separate group (never expected here): signalling ours would kill the TUI.
  with contextlib.suppress(ProcessLookupError):
    proc.send_signal(sig)


def _exited(proc: asyncio.subprocess.Process) -> bool:
  """True once the child has been reaped, regardless of pipe state."""
  return proc.returncode is not None


def _settled(proc: asyncio.subprocess.Process) -> bool:
  """True once the child is reaped and its process group is empty."""
  return _exited(proc) and not _group_alive(_pgid(proc))


async def _pump(
  stream: asyncio.StreamReader,
  queue: asyncio.Queue[Event | None],
  *,
  is_error: bool,
) -> None:
  """Read one pipe to EOF, posting each parsed line to the queue."""
  while True:
    try:
      raw = await stream.readline()
    except ValueError:
      # The line was longer than _STREAM_LIMIT; readline() discarded what it
      # had buffered and gave up on it. Carry on reading anyway: leaving the
      # pipe unread wedges the child the moment it fills the kernel buffer.
      queue.put_nowait(LogEvent(text="[dropped an over-long output line]", is_error=True))
      continue
    if not raw:
      return
    event = parse_line(raw.decode("utf-8", errors="replace"), is_error=is_error)
    if event is not None:
      queue.put_nowait(event)


async def _drain(proc: asyncio.subprocess.Process, queue: asyncio.Queue[Event | None]) -> None:
  """Read both pipes concurrently, then post None to mark end of output.

  The sentinel is what makes the consumer loop lossless: the queue is FIFO, so
  every event a pump produced is already ahead of it.
  """
  try:
    async with asyncio.TaskGroup() as group:
      for stream, is_error in ((proc.stdout, False), (proc.stderr, True)):
        if stream is not None:
          group.create_task(_pump(stream, queue, is_error=is_error))
  finally:
    queue.put_nowait(None)


async def _watchdog(
  proc: asyncio.subprocess.Process,
  drain: asyncio.Task[None],
  queue: asyncio.Queue[Event | None],
) -> None:
  """Backstop the end-of-output sentinel.

  A grandchild that inherited the pipes holds them open indefinitely, so EOF
  alone would never end the stream. Once the child is gone, give the readers a
  bounded grace period to finish and then declare the output over regardless.
  """
  loop = asyncio.get_running_loop()
  while not _exited(proc):
    await asyncio.sleep(_POLL_INTERVAL)
  deadline = loop.time() + _EOF_GRACE
  while not drain.done() and loop.time() < deadline:
    await asyncio.sleep(_POLL_INTERVAL)
  if not drain.done():
    queue.put_nowait(None)


async def _stop(proc: asyncio.subprocess.Process) -> None:
  """SIGTERM the child's process group so yt-dlp, ffmpeg and aria2c can all
  clean up, then SIGKILL whatever is still standing."""
  if _settled(proc):
    return
  _signal_group(proc, signal.SIGTERM)
  if await _poll_until(lambda: _settled(proc), _TERM_TIMEOUT):
    return
  _signal_group(proc, signal.SIGKILL)
  await _poll_until(lambda: _settled(proc), _KILL_TIMEOUT)


async def _exit_code(proc: asyncio.subprocess.Process) -> int:
  """The child's exit status, stopping it if it lingers after its output ends."""
  if not await _poll_until(lambda: _exited(proc), _EXIT_TIMEOUT):
    await _stop(proc)
  return proc.returncode if proc.returncode is not None else _UNKNOWN_RETURNCODE


async def _shutdown(
  proc: asyncio.subprocess.Process, tasks: tuple[asyncio.Task[None], ...]
) -> None:
  """Stop the child's process group first, then retire the helper tasks."""
  await _stop(proc)
  for task in tasks:
    task.cancel()
  # gather() retrieves whatever each task ended with, so a cancelled or failed
  # helper never surfaces as a "never retrieved" / "still pending" warning.
  await asyncio.gather(*tasks, return_exceptions=True)


async def run(argv: list[str]) -> AsyncIterator[Event]:
  """Yield events from `argv`, always ending with exactly one DoneEvent.

  Closing the iterator early SIGTERMs the child's whole process group, so
  yt-dlp can clean up its partial files and no orphaned ffmpeg is left writing
  to the output file.
  """
  try:
    proc = await asyncio.create_subprocess_exec(
      *argv,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
      limit=_STREAM_LIMIT,
      start_new_session=True,
    )
  except (OSError, ValueError, TypeError) as exc:
    target = argv[0] if argv else argv
    yield LogEvent(text=f"Could not start {target!r}: {exc}", is_error=True)
    yield DoneEvent(returncode=127)
    return

  queue: asyncio.Queue[Event | None] = asyncio.Queue()
  drain = asyncio.create_task(_drain(proc, queue))
  watchdog = asyncio.create_task(_watchdog(proc, drain, queue))
  try:
    while True:
      item = await queue.get()
      if item is None:
        break
      yield item
    if drain.done():
      # Only safe to await when it has finished: on the watchdog's path the
      # readers are still blocked on pipes somebody else is holding open.
      results = await asyncio.gather(drain, return_exceptions=True)
      failures = [r for r in results if isinstance(r, BaseException)]
      for failure in failures:
        yield LogEvent(text=f"Output reader failed: {failure!r}", is_error=True)
      if failures:
        # A dead reader may have left a pipe undrained, which would block the
        # child forever; stop it rather than wait on an exit that cannot come.
        await _stop(proc)
    yield DoneEvent(returncode=await _exit_code(proc))
  finally:
    await _shutdown(proc, (drain, watchdog))
