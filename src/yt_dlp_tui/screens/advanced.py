"""The four knobs worth exposing, plus a verbatim escape hatch.

Pushed on top of `MainScreen` by `MainScreen.action_advanced`. `escape`
(cancel) and `ctrl+s` (save) are both non-printable, so
`Input.check_consume_key` (which only claims `character.isprintable()`)
never claims them, and `Input` has no `BINDINGS` entry for either key -- so
they reach this screen's own bindings even though the first `Input`
(`#opt-dir`) holds focus at mount (`AUTO_FOCUS = "*"`, inherited from `App`
since `Screen.AUTO_FOCUS` defaults to `None`). See
`tests/test_advanced_screen.py` for the real-keypress tests that verify this
rather than assume it, matching the same key-consumption model documented in
`screens/main.py`.
"""

import shlex
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, Static

from yt_dlp_tui.command import Overrides


class AdvancedScreen(Screen):
  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("escape", "cancel", "cancel"),
    ("ctrl+s", "save", "save"),
  ]

  def compose(self) -> ComposeResult:
    current = self.app.overrides
    # `str(current.height_cap or "")` would render a height of 0 as "" --
    # 0 is falsy in Python even though it's a real (if unusual) value for
    # this field. Check `is None` explicitly instead of relying on truthiness.
    height = "" if current.height_cap is None else str(current.height_cap)
    with Vertical():
      yield Static("Advanced -- ctrl+s save, esc cancel")
      yield Label("Output directory")
      yield Input(value=str(current.output_dir or ""), id="opt-dir")
      yield Label("Max height (e.g. 720)")
      yield Input(value=height, id="opt-height")
      yield Label("Audio format (audio presets only)")
      yield Input(value=current.audio_format or "", id="opt-audio")
      yield Label("Extra yt-dlp flags (appended verbatim)")
      yield Input(value=shlex.join(current.extra_args), id="opt-extra")
    yield Footer()

  def _value(self, widget_id: str) -> str:
    return self.query_one(f"#{widget_id}", Input).value.strip()

  def action_save(self) -> None:
    height_raw = self._value("opt-height")
    try:
      height = int(height_raw) if height_raw else None
    except ValueError:
      height = None  # ignore junk rather than block the user
    try:
      extra = tuple(shlex.split(self._value("opt-extra")))
    except ValueError:
      extra = ()  # unbalanced quotes
    directory = self._value("opt-dir")
    audio = self._value("opt-audio")
    self.app.overrides = Overrides(
      output_dir=Path(directory) if directory else None,
      height_cap=height,
      audio_format=audio or None,
      extra_args=extra,
    )
    self.app.pop_screen()

  def action_cancel(self) -> None:
    self.app.pop_screen()
