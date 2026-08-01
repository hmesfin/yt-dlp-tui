"""URL entry, preset selection, and the live command preview.

Widget mutation (setting `app.url`, `app.selected_preset`, `app.probe_result`)
happens here; the actual re-render of the preset list / preview Static is
driven back by `YtDlpTuiApp`'s `watch_*` methods (see app.py) so there is a
single place that decides what a state change should redraw, rather than
every event handler re-deriving it.

`action_advanced` pushes `AdvancedScreen` (Task 8) and `action_download`
pushes `RunScreen` (Task 9). Both are ordinary top-of-file imports: neither
screen module imports this one or app.py, so there is no cycle to work around
with a function-level import.
"""

import shlex
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

from yt_dlp_tui.probe import preflight_warnings, probe
from yt_dlp_tui.screens.advanced import AdvancedScreen
from yt_dlp_tui.screens.run import RunScreen


class MainScreen(Screen):
  # "a" is filtered out of this Screen's bindings (by Input.check_consume_key
  # via Screen._binding_chain) while the URL input has focus, same as "q" on
  # the App -- typing wins. "escape" produces no printable character, so it
  # is never claimed by the Input and is always reachable: it blurs the URL
  # box, and once nothing owns the letter keys "a" fires normally. There is
  # no BINDINGS entry for "enter": Input owns it for its own submit action
  # (see on_input_submitted below), and a Screen-level entry for a key an
  # ancestor never actually receives while Input is focused would be dead
  # weight, not a real affordance.
  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("escape", "blur_url", ""),
    ("a", "advanced", "advanced"),
  ]

  def compose(self) -> ComposeResult:
    yield Header()
    with Vertical():
      # A separate Static from #meta, deliberately: #meta is owned by the
      # probe path (on_input_changed clears it on every keystroke, _probe
      # overwrites it once a lookup resolves), so anything written there at
      # mount is gone the instant the user types the first character of a
      # URL -- which is the first thing anyone does. A missing-ffmpeg warning
      # only matters until the user has actually seen it, so it needs a home
      # neither of those handlers ever touches.
      yield Static("", id="tool-warning", markup=False)
      yield Input(placeholder="Paste a URL…", id="url-input")
      # markup=False on both: Textual 8 parses content markup in
      # `Static.update`, and both of these carry text we do not control.
      # `#meta` shows a probed yt-dlp title, where "[MV] Song" renders as
      # " Song" and "10 [things] you" as "10  you" -- silent content loss the
      # user cannot detect, and bracketed titles are near-universal on
      # YouTube. `#command-preview` shows `shlex.join(cmd)`, carrying the
      # user's URL and `-o` template. An unbalanced tag ("a [/b] c") is worse
      # than mangled: `Static.update` raises MarkupError, here out of a
      # reactive watcher, which takes the app down.
      yield Static("", id="meta", markup=False)
      yield ListView(id="preset-list")
      yield Static("", id="command-preview", markup=False)
    yield Footer()

  async def on_mount(self) -> None:
    warnings = preflight_warnings(self.app.tooling, self.app.presets)
    banner = self.query_one("#tool-warning", Static)
    # `height: auto` does not collapse an empty Static to zero rows -- with no
    # warnings this still reserved a blank line above #url-input. `display`
    # is the actual on/off switch; only flip it on when there's something to
    # show.
    banner.display = bool(warnings)
    if warnings:
      banner.update(" ".join(warnings))
    await self.refresh_presets()
    self.refresh_preview()

  async def refresh_presets(self) -> None:
    # `ListView.clear()`/`.append()`/`.extend()` are only "optionally
    # awaitable" when there is a single call in flight: `clear()` posts a
    # `Prune` message and detaches children once that message is processed,
    # it does not remove them from the node list synchronously. Firing
    # `clear()` and a mount back-to-back without awaiting -- which is what an
    # un-awaited call looks like -- lets a second refresh (e.g. from a probe
    # result arriving) mount before the previous `clear()`'s removals have
    # landed, raising `DuplicateIds` on the reused `preset-<id>` widget ids.
    listing = self.query_one("#preset-list", ListView)
    await listing.clear()
    items = [
      ListItem(Static(preset.name), id=f"preset-{preset.id}") for preset in self.app.ordered_presets
    ]
    await listing.extend(items)
    # `ListView` composed empty and populated here: its own `_on_mount` only
    # sets `index` when `self.children` is non-empty *at mount time*, which
    # it never is for us, so nothing ever highlights a row on its own. Set it
    # explicitly on every rebuild -- including reorders -- so there is always
    # a visible selection and so a reorder actually steers `selected_preset`
    # (via the `Highlighted` message this posts, handled below). `validate_index`
    # clamps this to `None` on its own if `items` is empty.
    listing.index = 0

  def refresh_preview(self) -> None:
    cmd = self.app.current_command()
    self.query_one("#command-preview", Static).update(shlex.join(cmd))

  async def on_input_changed(self, event: Input.Changed) -> None:
    self.app.url = event.value
    # The old title/duration belongs to whatever URL was there before this
    # edit -- clear it immediately rather than leaving it on screen (forever,
    # if the field is edited down to empty, since an empty URL never
    # re-probes below).
    self.query_one("#meta", Static).update("")
    if event.value.strip():
      self.run_worker(self._probe(event.value), exclusive=True)

  async def on_input_submitted(self, event: Input.Submitted) -> None:
    # `Input` owns "enter" for its own `action_submit`, which posts this
    # message rather than falling through to a Screen-level binding -- this
    # is the actual (and only) way "download" fires while the URL input is
    # focused, which is where `AUTO_FOCUS` puts focus at mount and where it
    # stays for as long as the user is typing a URL.
    self.action_download()

  def action_blur_url(self) -> None:
    # "escape" is the owner-approved way out of the URL input: it produces
    # no printable character, so Input never claims it the way it claims
    # every letter (see MainScreen.BINDINGS), and it is always reachable.
    # Un-focusing (rather than moving focus to the next widget) is the
    # simplest thing that reliably unblocks "a" at the Screen level.
    self.set_focus(None)

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
    self.app.push_screen(AdvancedScreen())

  def action_download(self) -> None:
    if self.app.url.strip():
      self.app.push_screen(RunScreen(self.app.current_command()))
    else:
      # Pressing enter on an empty box and getting nothing at all reads as a
      # broken key, not as a missing URL.
      self.notify("Enter a URL first.")
