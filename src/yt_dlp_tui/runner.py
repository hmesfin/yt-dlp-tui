"""Spawn yt-dlp and stream its output as typed events.

The only module in the package that touches subprocesses.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from yt_dlp_tui.events import DoneEvent, Event, LogEvent, parse_line

# Per-stream read buffer. A line longer than this cannot be returned by
# readline(), so keep it far above anything yt-dlp plausibly prints.
_STREAM_LIMIT = 1024 * 1024
# Grace period after SIGTERM before we escalate to SIGKILL.
_TERM_TIMEOUT = 5.0
# Upper bound on reaping a SIGKILLed child, so cleanup can never block forever.
_KILL_TIMEOUT = 5.0


class Cancelled(Exception):
  """Raised to signal that a run was aborted at the user's request."""


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


async def _stop(proc: asyncio.subprocess.Process) -> None:
  """SIGTERM the child so yt-dlp can clean up, escalating to SIGKILL."""
  if proc.returncode is not None:
    return
  with contextlib.suppress(ProcessLookupError):
    proc.terminate()
  try:
    await asyncio.wait_for(proc.wait(), timeout=_TERM_TIMEOUT)
    return
  except TimeoutError:
    pass
  with contextlib.suppress(ProcessLookupError):
    proc.kill()
  with contextlib.suppress(TimeoutError):
    await asyncio.wait_for(proc.wait(), timeout=_KILL_TIMEOUT)


async def _shutdown(proc: asyncio.subprocess.Process, drain: asyncio.Task[None]) -> None:
  """Stop the child first, then retire the reader task."""
  await _stop(proc)
  drain.cancel()
  # gather() retrieves whatever the task ended with, so a cancelled or failed
  # reader never surfaces as a "never retrieved" / "still pending" warning.
  await asyncio.gather(drain, return_exceptions=True)


async def run(argv: list[str]) -> AsyncIterator[Event]:
  """Yield events from `argv`, always ending with exactly one DoneEvent.

  Closing the iterator early sends SIGTERM to the child so yt-dlp can clean up
  its own partial files.
  """
  try:
    proc = await asyncio.create_subprocess_exec(
      *argv,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
      limit=_STREAM_LIMIT,
    )
  except OSError as exc:
    yield LogEvent(text=f"Could not start {argv[0]!r}: {exc}", is_error=True)
    yield DoneEvent(returncode=127)
    return

  queue: asyncio.Queue[Event | None] = asyncio.Queue()
  drain = asyncio.create_task(_drain(proc, queue))
  try:
    while True:
      item = await queue.get()
      if item is None:
        break
      yield item
    results = await asyncio.gather(drain, return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    for failure in failures:
      yield LogEvent(text=f"Output reader failed: {failure!r}", is_error=True)
    if failures:
      # A dead reader may have left a pipe undrained, which would block the
      # child forever; stop it rather than wait on an exit that cannot come.
      await _stop(proc)
    yield DoneEvent(returncode=await proc.wait())
  finally:
    await _shutdown(proc, drain)
