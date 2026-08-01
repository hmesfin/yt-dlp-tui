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
      yield Static("Advanced — ctrl+s save, esc cancel")
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

  def _parse_height(self, raw: str) -> int | None:
    """Blank means "no cap" and is silent. Anything else that doesn't parse
    to a positive whole number is dropped -- pinned as "ignored rather than
    fatal" -- but the user is told, since a silently vanished value is a
    different (and worse) failure than a rejected one. A non-positive height
    (`0`, `-5`) parses fine as an int but produces a `-f` selector no format
    can ever satisfy (`build_command`), so it is rejected the same way."""
    if not raw:
      return None
    try:
      value = int(raw)
    except ValueError:
      value = None
    if value is not None and value <= 0:
      value = None
    if value is None:
      self.notify(
        f"Ignoring height {raw!r} — must be a positive whole number.",
        severity="warning",
      )
    return value

  def _parse_extra_args(self, raw: str) -> tuple[str, ...]:
    """An unterminated quote makes `shlex.split` raise `ValueError`, which
    would otherwise discard every flag the user typed with no explanation --
    worse than the height case, since the field is blank again once this
    pops back to MainScreen. Still degrades to `()` (not authorized to keep
    the screen open to let the user fix it), but now says so."""
    try:
      return tuple(shlex.split(raw))
    except ValueError:
      self.notify(
        f"Ignoring extra args {raw!r} — unbalanced quotes.",
        severity="warning",
      )
      return ()

  def action_save(self) -> None:
    height = self._parse_height(self._value("opt-height"))
    extra = self._parse_extra_args(self._value("opt-extra"))
    directory = self._value("opt-dir")
    audio = self._value("opt-audio")
    self.app.overrides = Overrides(
      # `.expanduser()`: yt-dlp runs as a subprocess with no shell, so a
      # literal "~/Videos" is never expanded on its own -- it would create a
      # directory named "~" under the process cwd instead of the user's home.
      output_dir=Path(directory).expanduser() if directory else None,
      height_cap=height,
      audio_format=audio or None,
      extra_args=extra,
    )
    self.app.pop_screen()

  def action_cancel(self) -> None:
    self.app.pop_screen()
