"""Cheap metadata lookup and startup tool detection.

Probe failure is never fatal: a URL we cannot describe may still download
fine, so nothing in this module raises out to the caller.
"""

import asyncio
import contextlib
import json
import shutil
from dataclasses import dataclass

# Grace period after SIGTERM before escalating to SIGKILL on a probe that ran
# past its timeout.
_TERM_GRACE = 3.0
# Upper bound on reaping a SIGKILLed probe, so cleanup can never block forever.
_KILL_GRACE = 3.0
# Exit status has to be polled: proc.wait() resolves only once every PIPE
# stream has *also* disconnected (see runner.py), which is a needless
# dependency here -- we only care that the child itself is gone.
_POLL_INTERVAL = 0.02


@dataclass(frozen=True)
class ProbeResult:
  title: str = ""
  duration: int | None = None
  extractor: str = ""
  is_playlist: bool = False
  playlist_count: int | None = None
  ok: bool = True

  @classmethod
  def failed(cls) -> "ProbeResult":
    return cls(ok=False)


@dataclass(frozen=True)
class Tooling:
  ytdlp: str | None
  ffmpeg: str | None


def _as_int(value: object) -> int | None:
  """Coerce a probe field to int, or None. Never raises: extractors vary."""
  if value is None:
    return None
  try:
    return int(value)  # type: ignore[arg-type]
  except (TypeError, ValueError):
    return None


def parse_probe_json(raw: str) -> ProbeResult:
  """Parse one line of `yt-dlp -J` output. Total: any input yields a result."""
  try:
    data = json.loads(raw)
  except (json.JSONDecodeError, ValueError):
    return ProbeResult.failed()
  if not isinstance(data, dict):
    # A JSON scalar/list/null is valid JSON but not a valid payload shape;
    # `.get` on it would raise AttributeError.
    return ProbeResult.failed()
  return ProbeResult(
    title=str(data.get("title") or ""),
    duration=_as_int(data.get("duration")),
    extractor=str(data.get("extractor_key") or ""),
    is_playlist=data.get("_type") == "playlist",
    playlist_count=_as_int(data.get("playlist_count")),
    ok=True,
  )


async def _wait_for_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
  """Poll for the child to exit, returning False if `timeout` runs out.

  Deliberately does not await proc.wait(): that future only resolves once
  every PIPE stream has disconnected too, which a stuck grandchild (or even
  just a slow reader) could delay indefinitely. proc.returncode is set the
  moment the child is reaped, independent of pipe state.
  """
  loop = asyncio.get_running_loop()
  deadline = loop.time() + timeout
  while proc.returncode is None:
    if loop.time() >= deadline:
      return False
    await asyncio.sleep(_POLL_INTERVAL)
  return True


async def _kill(proc: asyncio.subprocess.Process) -> None:
  """Stop a probe that ran past its timeout and confirm it is actually gone.

  A bare terminate()-and-return leaves the child to die (or not) on its own
  schedule: SIGTERM can be ignored, and nothing here would notice. `-J
  --flat-playlist` is metadata-only and never spawns ffmpeg or aria2c, so
  (unlike runner.py) there is no process group of helpers to worry about --
  but the yt-dlp process itself still needs a SIGKILL escalation and a
  bounded wait before we can call the probe over.
  """
  if proc.returncode is not None:
    return
  with contextlib.suppress(ProcessLookupError):
    proc.terminate()
  if await _wait_for_exit(proc, _TERM_GRACE):
    return
  with contextlib.suppress(ProcessLookupError):
    proc.kill()
  await _wait_for_exit(proc, _KILL_GRACE)


async def probe(url: str, *, ytdlp: str = "yt-dlp", timeout: float = 20.0) -> ProbeResult:
  """Look up cheap metadata for `url` without downloading anything.

  `--playlist-items 1` keeps this cheap on large channels/playlists while
  still returning the full `playlist_count`. Never raises: a probe failure
  (bad URL, missing binary, timeout, garbage output) is reported as
  `ProbeResult.failed()`, not an exception.
  """
  argv = [ytdlp, "-J", "--no-warnings", "--flat-playlist", "--playlist-items", "1", "--", url]
  try:
    proc = await asyncio.create_subprocess_exec(
      *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
  except (OSError, ValueError, TypeError):
    # OSError: binary missing/not executable. ValueError: e.g. a NUL byte in
    # `url` (create_subprocess_exec rejects it outright). Pasted/typed URLs
    # are untrusted input, so this must degrade like any other probe failure.
    return ProbeResult.failed()
  try:
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
  except TimeoutError:
    await _kill(proc)
    return ProbeResult.failed()
  except asyncio.CancelledError:
    # Task 7 runs probe() under an exclusive worker, so an ordinary keystroke
    # cancels an in-flight probe long before the timeout above ever fires --
    # that path must not leak the child either. A second cancellation can
    # land on any `await` here, so this must not await anything: send SIGKILL
    # outright and let the event loop's own SIGCHLD bookkeeping reap it in
    # the background. Cancellation still propagates -- swallowing it would
    # break Textual's worker semantics.
    if proc.returncode is None:
      with contextlib.suppress(ProcessLookupError):
        proc.kill()
    raise
  if proc.returncode != 0:
    return ProbeResult.failed()
  return parse_probe_json(stdout.decode("utf-8", errors="replace"))


def detect_tooling(ytdlp: str = "yt-dlp") -> Tooling:
  """Locate the binaries the app depends on, once at startup."""
  return Tooling(ytdlp=shutil.which(ytdlp), ffmpeg=shutil.which("ffmpeg"))


def preflight_warnings(tooling: Tooling, presets: tuple) -> list[str]:
  """Human-readable startup problems. Empty list means the environment is fine."""
  warnings: list[str] = []
  if tooling.ytdlp is None:
    warnings.append("yt-dlp not found on PATH — downloads will fail.")
  if tooling.ffmpeg is None and any(p.needs_ffmpeg for p in presets):
    warnings.append("ffmpeg not found — merge and audio-extract presets are unavailable.")
  return warnings
