"""The one sanctioned network test: a real download against a real URL.

Deselected by default (see the `network` marker in pyproject.toml); run
explicitly with `uv run pytest -m network -v`. Every other test file in this
project is offline by construction.
"""

import shutil
from pathlib import Path

import pytest

from yt_dlp_tui.command import Overrides, build_command
from yt_dlp_tui.events import DoneEvent, ProgressEvent
from yt_dlp_tui.presets import BUILTIN_PRESETS
from yt_dlp_tui.runner import run

URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"  # Big Buck Bunny, CC-licensed


@pytest.mark.network
async def test_real_download_end_to_end(tmp_path: Path) -> None:
  if shutil.which("yt-dlp") is None:
    pytest.skip("yt-dlp not on PATH")
  preset = next(p for p in BUILTIN_PRESETS if p.id == "data-saver")
  argv = build_command(
    URL, preset, Overrides(height_cap=144, output_dir=tmp_path), download_dir=tmp_path
  )
  events = [ev async for ev in run(argv)]
  assert isinstance(events[-1], DoneEvent)
  assert events[-1].ok, [e for e in events if getattr(e, "is_error", False)]
  assert any(isinstance(e, ProgressEvent) for e in events)
  assert list(tmp_path.iterdir())
