"""Turn one line of yt-dlp output into one typed event. Pure and total:
never raises on malformed input, since a parser crash would kill the UI.
"""

import json
from dataclasses import dataclass

from yt_dlp_tui.command import POSTPROCESS_PREFIX, PROGRESS_PREFIX


@dataclass(frozen=True)
class ProgressEvent:
  downloaded: int
  total: int
  speed: float
  eta: int
  index: int
  count: int
  title: str

  @property
  def fraction(self) -> float:
    return self.downloaded / self.total if self.total > 0 else 0.0


@dataclass(frozen=True)
class PostProcessEvent:
  status: str
  processor: str


@dataclass(frozen=True)
class LogEvent:
  text: str
  is_error: bool = False


@dataclass(frozen=True)
class DoneEvent:
  returncode: int

  @property
  def ok(self) -> bool:
    return self.returncode == 0


Event = ProgressEvent | PostProcessEvent | LogEvent | DoneEvent


def _progress(payload: str) -> ProgressEvent:
  d = json.loads(payload)
  return ProgressEvent(
    downloaded=int(d.get("b") or 0),
    total=int(d.get("t") or 0),
    speed=float(d.get("s") or 0.0),
    eta=int(d.get("e") or 0),
    index=int(d.get("i") or 0),
    count=int(d.get("n") or 0),
    title=str(d.get("title") or ""),
  )


def _postprocess(payload: str) -> PostProcessEvent:
  d = json.loads(payload)
  return PostProcessEvent(status=str(d.get("st") or ""), processor=str(d.get("pp") or ""))


def parse_line(line: str, *, is_error: bool = False) -> Event | None:
  text = line.rstrip("\n").rstrip()
  if not text.strip():
    return None
  for prefix, build in ((PROGRESS_PREFIX, _progress), (POSTPROCESS_PREFIX, _postprocess)):
    if text.startswith(prefix):
      try:
        return build(text[len(prefix) :])
      except (json.JSONDecodeError, ValueError, TypeError):
        # Malformed template output (e.g. a bare `NA`) must not kill the run.
        return LogEvent(text=text, is_error=is_error)
  return LogEvent(text=text, is_error=is_error)
