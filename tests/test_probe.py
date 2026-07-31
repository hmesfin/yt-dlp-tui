import asyncio
import json
import os
import stat
import time
from pathlib import Path

import pytest

from yt_dlp_tui import probe as probe_module
from yt_dlp_tui.probe import ProbeResult, detect_tooling, parse_probe_json, probe

VIDEO = json.dumps(
  {
    "_type": "video",
    "id": "aqz-KE-bpKQ",
    "title": "Big Buck Bunny 60fps 4K",
    "duration": 635,
    "extractor_key": "Youtube",
    "playlist_count": None,
  }
)
PLAYLIST = json.dumps(
  {
    "_type": "playlist",
    "id": "PLbpi",
    "title": "Top Trending Videos",
    "playlist_count": 20,
    "extractor_key": "YoutubeTab",
    "duration": None,
  }
)


def test_parses_single_video() -> None:
  r = parse_probe_json(VIDEO)
  assert r.title == "Big Buck Bunny 60fps 4K"
  assert r.duration == 635
  assert r.extractor == "Youtube"
  assert r.is_playlist is False
  assert r.ok is True


def test_parses_playlist_and_count() -> None:
  r = parse_probe_json(PLAYLIST)
  assert r.is_playlist is True
  assert r.playlist_count == 20
  assert r.duration is None


def test_malformed_json_returns_not_ok_instead_of_raising() -> None:
  r = parse_probe_json("not json at all")
  assert r.ok is False
  assert r.title == ""


def test_empty_output_returns_not_ok() -> None:
  assert parse_probe_json("").ok is False


def test_missing_fields_do_not_raise() -> None:
  r = parse_probe_json('{"_type": "video"}')
  assert r.ok is True
  assert r.title == ""
  assert r.duration is None


def test_non_object_json_returns_not_ok() -> None:
  # json.loads accepts these; `.get` on them would raise AttributeError.
  for raw in ("5", "[1,2]", '"text"', "true", "null"):
    assert parse_probe_json(raw).ok is False


def test_junk_field_types_degrade_instead_of_raising() -> None:
  r = parse_probe_json('{"_type":"video","duration":"abc","playlist_count":[1,2]}')
  assert r.duration is None
  assert r.playlist_count is None
  assert r.ok is True


def test_failed_result_helper() -> None:
  assert ProbeResult.failed().ok is False


def test_detect_tooling_reports_missing_binary() -> None:
  tools = detect_tooling(ytdlp="/nonexistent/yt-dlp-xyz")
  assert tools.ytdlp is None


def test_detect_tooling_finds_real_python() -> None:
  # `python3` stands in for any binary guaranteed on PATH.
  assert detect_tooling(ytdlp="python3").ytdlp is not None


# --- probe() itself -----------------------------------------------------
#
# The brief's Step 1 tests only cover parse_probe_json/detect_tooling. probe()
# is a required deliverable too, and TDD applies to it: these exercise the
# actual subprocess path, including the timeout/kill hardening described in
# the task-6 report (a bare terminate()-and-return was the shape that bit
# runner.py before -- see probe._kill).
#
# A fake "yt-dlp" is a tiny executable script whose behaviour is selected by
# the trailing `url` argument, since probe() only lets us vary `ytdlp` and
# `url` in the argv it builds.

_FAKE_YTDLP = """#!/usr/bin/env python3
import json
import os
import signal
import sys
import time

url = sys.argv[-1]

if url == "GOOD":
  print(json.dumps({"_type": "video", "title": "T", "duration": 5,
                     "extractor_key": "X", "playlist_count": None}))
  sys.exit(0)
elif url == "FAIL":
  sys.exit(1)
elif url.startswith("HANG:"):
  with open(url.split(":", 1)[1], "w") as f:
    f.write(str(os.getpid()))
  time.sleep(30)
elif url.startswith("IGNORESIG:"):
  signal.signal(signal.SIGTERM, signal.SIG_IGN)
  with open(url.split(":", 1)[1], "w") as f:
    f.write(str(os.getpid()))
  time.sleep(30)
else:
  sys.exit(1)
"""


def _fake_ytdlp(tmp_path: Path) -> str:
  script = tmp_path / "fake-yt-dlp"
  script.write_text(_FAKE_YTDLP)
  script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
  return str(script)


def _wait_until_gone(pid: int, timeout: float = 5.0) -> None:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    try:
      os.kill(pid, 0)
    except ProcessLookupError:
      return
    time.sleep(0.02)
  raise AssertionError(f"pid {pid} is still alive")


async def test_probe_returns_parsed_result_on_success(tmp_path: Path) -> None:
  r = await probe("GOOD", ytdlp=_fake_ytdlp(tmp_path))
  assert r.ok is True
  assert r.title == "T"
  assert r.duration == 5


async def test_probe_returns_failed_on_nonzero_exit(tmp_path: Path) -> None:
  r = await probe("FAIL", ytdlp=_fake_ytdlp(tmp_path))
  assert r.ok is False


async def test_probe_returns_failed_when_binary_is_missing() -> None:
  r = await probe("anything", ytdlp="/nonexistent/yt-dlp-xyz")
  assert r.ok is False


async def test_probe_returns_failed_on_nul_byte_in_url(tmp_path: Path) -> None:
  # create_subprocess_exec raises ValueError on an embedded NUL byte; a typed
  # or pasted URL is untrusted input and must degrade, not crash the worker.
  r = await probe("bad\0url", ytdlp=_fake_ytdlp(tmp_path))
  assert r.ok is False


async def test_probe_returns_failed_and_kills_child_on_timeout(tmp_path: Path) -> None:
  pid_file = tmp_path / "pid"
  r = await probe(f"HANG:{pid_file}", ytdlp=_fake_ytdlp(tmp_path), timeout=0.3)
  assert r.ok is False
  deadline = time.monotonic() + 5.0
  while not pid_file.exists() and time.monotonic() < deadline:
    await asyncio.sleep(0.02)
  pid = int(pid_file.read_text())
  _wait_until_gone(pid)


async def test_probe_escalates_to_sigkill_when_child_ignores_sigterm(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  monkeypatch.setattr(probe_module, "_TERM_GRACE", 0.2)
  pid_file = tmp_path / "pid"
  r = await probe(f"IGNORESIG:{pid_file}", ytdlp=_fake_ytdlp(tmp_path), timeout=0.3)
  assert r.ok is False
  deadline = time.monotonic() + 5.0
  while not pid_file.exists() and time.monotonic() < deadline:
    await asyncio.sleep(0.02)
  pid = int(pid_file.read_text())
  _wait_until_gone(pid)
