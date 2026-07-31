"""URL entry, preset selection, and the live command preview.

Widget mutation (setting `app.url`, `app.selected_preset`, `app.probe_result`)
happens here; the actual re-render of the preset list / preview Static is
driven back by `YtDlpTuiApp`'s `watch_*` methods (see app.py) so there is a
single place that decides what a state change should redraw, rather than
every event handler re-deriving it.

Deliberate stub: `action_advanced` and `action_download` are replaced by
Tasks 8 and 9, which will add a top-of-file import for the screen they push
(`advanced.py` / `run.py` never import this module or app.py, so there is no
cycle to work around with a function-level import).
"""

import shlex
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

from yt_dlp_tui.probe import probe


class MainScreen(Screen):
  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("a", "advanced", "advanced"),
    ("enter", "download", "download"),
  ]

  def compose(self) -> ComposeResult:
    yield Header()
    with Vertical():
      yield Input(placeholder="Paste a URL…", id="url-input")
      yield Static("", id="meta")
      yield ListView(id="preset-list")
      yield Static("", id="command-preview")
    yield Footer()

  async def on_mount(self) -> None:
    await self.refresh_presets()
    self.refresh_preview()

  async def refresh_presets(self) -> None:
    # `ListView.clear()`/`.append()` are only "optionally awaitable" when
    # there is a single call in flight: `clear()` posts a `Prune` message and
    # detaches children once that message is processed, it does not remove
    # them from the node list synchronously. Firing `clear()` and `append()`
    # back-to-back without awaiting -- which is what an un-awaited call looks
    # like -- lets a second refresh (e.g. from a probe result arriving) run
    # `append()` before the previous `clear()`'s removals have landed,
    # raising `DuplicateIds` on the reused `preset-<id>` widget ids.
    listing = self.query_one("#preset-list", ListView)
    await listing.clear()
    for preset in self.app.ordered_presets:
      await listing.append(ListItem(Static(preset.name), id=f"preset-{preset.id}"))

  def refresh_preview(self) -> None:
    cmd = self.app.current_command()
    self.query_one("#command-preview", Static).update(shlex.join(cmd))

  async def on_input_changed(self, event: Input.Changed) -> None:
    self.app.url = event.value
    if event.value.strip():
      self.run_worker(self._probe(event.value), exclusive=True)

  async def on_input_submitted(self, event: Input.Submitted) -> None:
    # `Input` binds "enter" to its own `action_submit` (which posts this
    # message) before a Screen-level BINDINGS entry for "enter" ever sees the
    # key -- the Screen binding below stays only so the Footer keeps showing
    # "enter: download"; this is what actually fires it while the URL input
    # (the widget AUTO_FOCUS puts focus on) is focused.
    self.action_download()

  async def _probe(self, url: str) -> None:
    result = await probe(url, ytdlp=self.app.tooling.ytdlp or "yt-dlp")
    if result.ok:
      duration = f" · {result.duration // 60}:{result.duration % 60:02d}" if result.duration else ""
      count = f" · {result.playlist_count} items" if result.playlist_count else ""
      self.query_one("#meta", Static).update(
        f"{result.title}{duration}{count} · {result.extractor}"
      )
    else:
      self.query_one("#meta", Static).update("(could not read metadata — download may still work)")
    self.app.probe_result = result

  def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
    if event.item is None or event.item.id is None:
      return
    preset_id = event.item.id.removeprefix("preset-")
    self.app.selected_preset = next(p for p in self.app.presets if p.id == preset_id)

  def action_advanced(self) -> None:
    self.notify("Advanced options arrive in Task 8.")

  def action_download(self) -> None:
    self.notify("The run screen arrives in Task 9.")
