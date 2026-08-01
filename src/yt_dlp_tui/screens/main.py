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
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.events import DescendantBlur, DescendantFocus
from textual.screen import Screen
from textual.widgets import Header, Input, ListItem, ListView, Static

from yt_dlp_tui.command import elide_machinery
from yt_dlp_tui.presets import Preset
from yt_dlp_tui.probe import preflight_warnings, probe
from yt_dlp_tui.screens.advanced import AdvancedScreen
from yt_dlp_tui.screens.run import RunScreen

# Task 11: `MainScreen`'s own `Footer` always rendered empty. `AUTO_FOCUS`
# puts focus on `#url-input` at mount, `Input.check_consume_key` claims every
# printable character (see the BINDINGS comment below), and
# `Screen._binding_chain` strips a claimed key out of every ancestor's
# binding map before `Footer` ever sees it -- so `a`/`q` never showed up
# there, mount or not. A legend that just states the keys live in `BINDINGS`
# would have the same problem one level removed: it would advertise `a` and
# `q` while the input is focused and they are dead. These two strings are
# keyed to the two states the screen actually has (`_refresh_legend` below
# picks between them off the real focus state, not off mount alone), so the
# legend can never be truer or falser than the keys underneath it.
LEGEND_INPUT_FOCUSED = "⏎ download · esc for keys · ^q quit"
# Unchanged from Task 11 on purpose: with `escape` moving focus to the preset
# list (and `PresetList` re-pointing `enter`), all five of these keys are live
# in the state this string describes, so the honest legend and the pinned one
# are now the same text. Nothing here is reworded for its own sake.
LEGEND_INPUT_BLURRED = "a advanced · q quit · v full command · ↑↓ preset · ⏎ download"


class MainBody(VerticalScroll, can_focus=False):
  """The screen's content column, scrollable rather than clipped.

  `height: auto` on the text widgets fixes text cut off *sideways*, but below
  about 32 columns the wrapped content is simply taller than the terminal, and
  a plain `Vertical` (`overflow: hidden hidden`) clipped the tail with nothing
  on screen to say so -- the command preview itself was cut mid-URL. A scroll
  container puts a scrollbar there instead, so the content is reachable and
  visibly incomplete rather than invisibly truncated.

  `can_focus=False` is load-bearing, not tidiness. `ScrollableContainer`
  declares `can_focus=True`, and `App.AUTO_FOCUS = "*"` focuses the first
  focusable widget at mount -- which would be this container instead of
  `#url-input`. That would break the premise the entire keymap ruling rests
  on (the URL box has focus at launch, so letters type rather than fire
  bindings) and start the legend in the wrong state; verified by running the
  suite with it focusable, which fails 10 tests.

  The cost of that: the body can be scrolled with the mouse but not the
  keyboard, since nothing can focus it. Textual still scrolls a focused child
  into view, so `escape` (focus -> preset list) reaches the middle of the
  column; a keyboard-only user on a ~30-column terminal cannot scroll past
  it to the elision note. Strictly better than clipping it with no scrollbar,
  and short of a focusable container there is no way to have both.
  """


class PresetList(ListView):
  """The preset rows. Exists only to re-point `enter` at the download action.

  `ListView` binds `enter` to its own `action_select_cursor`, and
  `Screen._check_bindings` stops at the first namespace in the chain with a
  matching key -- so a Screen-level `enter` binding would never be reached
  while this widget has focus, which (after the `escape` fix in
  `action_blur_url`) is the state the blurred legend describes. Overriding the
  binding here is what makes that legend's `⏎ download` true.

  Handling `ListView.Selected` on the screen would have been the shorter fix
  and is wrong: `_on_list_item__child_clicked` posts `Selected` for a *mouse
  click* too, so it would leave a mouse user unable to change preset without
  starting a download. A binding only fires for the key.

  `screen.download` rather than a bare `download`: Textual resolves the
  `screen` namespace against `App.screen` (`App._action_targets`), so the
  action runs on `MainScreen`, which is where it lives.
  """

  BINDINGS: ClassVar[list[Binding]] = [
    Binding("enter", "screen.download", "download", show=False),
  ]


class MainScreen(Screen):
  # "a" is filtered out of this Screen's bindings (by Input.check_consume_key
  # via Screen._binding_chain) while the URL input has focus, same as "q" on
  # the App -- typing wins. "escape" produces no printable character, so it
  # is never claimed by the Input and is always reachable: it blurs the URL
  # box, and once nothing owns the letter keys "a" fires normally. There is
  # no BINDINGS entry for "enter": Input owns it for its own submit action
  # (see on_input_submitted below), and a Screen-level entry for a key an
  # ancestor never actually receives while Input is focused would be dead
  # weight, not a real affordance. "v" (Task 11) is exactly the same story as
  # "a": printable, so it only reaches this Screen once `escape` has blurred
  # the input -- that is the documented tradeoff, not a bug to route around.
  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("escape", "blur_url", ""),
    ("a", "advanced", "advanced"),
    ("v", "toggle_full_command", "full command"),
  ]

  # Whether the command preview shows the real argv verbatim (True) or the
  # machinery-elided view (False). Plain instance state, not a `reactive`:
  # nothing needs to watch it fire on its own, `action_toggle_full_command`
  # (the only writer) already calls `refresh_preview()` itself, and
  # `refresh_preview()` (the only reader) is called on every kind of preview
  # refresh already -- URL edits, preset changes, override saves -- so
  # storing this on the screen instance (which Task 8/9 never tear down;
  # `AdvancedScreen`/`RunScreen` are pushed on top and popped, this one
  # never is) makes the choice persist across all of them for free, with no
  # flag to thread through `App.watch_*` or any other call site.
  _show_full_command: bool = False

  # The preset ids, in the order the rows were last built. `refresh_presets`
  # compares against it to tell a *reorder* (probe revealed a playlist) from a
  # rebuild that produced the same rows again -- which is what every one of the
  # per-keystroke probe results does. See `refresh_presets` for why that
  # distinction is the whole fix.
  _preset_order: tuple[str, ...] = ()

  # The presets behind the rows currently mounted, in row order. Rows are keyed
  # by position (see `refresh_presets`), so this is what turns a row back into
  # a `Preset`.
  _rendered_presets: tuple[Preset, ...] = ()

  def compose(self) -> ComposeResult:
    yield Header()
    with MainBody(id="main-body"):
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
      yield PresetList(id="preset-list")
      yield Static("", id="command-preview", markup=False)
      # A separate Static from #command-preview, not a second line appended
      # to the same one: two existing tests
      # (test_bracketed_text_survives_the_meta_and_preview_statics,
      # test_an_unbalanced_bracket_does_not_crash_the_app in
      # test_main_screen.py) compare #command-preview's *rendered* lines
      # against its stored `.content` with spaces stripped, on the premise
      # that wrapping only ever inserts line breaks -- it never deletes or
      # reorders characters. Embedding a literal "\n\n" plus this note in
      # that same string would still be markup-safe, but it would violate
      # that premise: `.render_line` never re-emits the "\n" itself (each
      # wrapped row is its own line), while `.content` would still contain
      # it, so the two could no longer match. Keeping the elision count and
      # the "v to show all" hint on their own widget sidesteps that rather
      # than weakening either test. No user/yt-dlp content here (just the
      # fixed hint text this module owns), but markup=False anyway, on the
      # same "nothing on this screen renders with markup on" rule the rest
      # of this compose follows.
      yield Static("", id="command-preview-note", markup=False)
    # Task 11: `Footer` replaced with a Static legend kept in sync with real
    # focus state by `_refresh_legend` (see `on_descendant_focus`/`_blur`
    # below) -- see the LEGEND_* comment above for why a plain `Footer` can
    # never do this correctly here.
    yield Static(LEGEND_INPUT_FOCUSED, id="key-legend", markup=False)

  async def on_mount(self) -> None:
    warnings = [*self.app.startup_warnings, *preflight_warnings(self.app.tooling, self.app.presets)]
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
    self._refresh_legend()

  async def refresh_presets(self) -> None:
    # `ListView.clear()`/`.append()`/`.extend()` are only "optionally
    # awaitable" when there is a single call in flight: `clear()` posts a
    # `Prune` message and detaches children once that message is processed,
    # it does not remove them from the node list synchronously. Firing
    # `clear()` and a mount back-to-back without awaiting -- which is what an
    # un-awaited call looks like -- lets a second refresh (e.g. from a probe
    # result arriving) mount before the previous `clear()`'s removals have
    # landed, raising `DuplicateIds` on the reused `preset-<n>` widget ids.
    listing = self.query_one("#preset-list", ListView)
    # Read the highlight off the widget rather than `app.selected_preset`:
    # this is what the user can actually see selected right now, and it cannot
    # lag behind a `Highlighted` message that has not been dispatched yet.
    previous = self._preset_at(listing.highlighted_child)
    previous_id = previous.id if previous is not None else None
    await listing.clear()
    # markup=False on the row's Static for the same reason as #meta and
    # #command-preview above: `preset.name` comes out of the user's
    # config.toml, which the README tells people to write. "Lossless [FLAC]
    # rip" renders as "Lossless  rip" with markup on, and a name containing
    # "[/b]" raises MarkupError out of this very call during `on_mount`, so
    # the app never finishes starting.
    #
    # Rows are keyed by *position*, not by `preset.id`. The id comes from the
    # user's config.toml, on which the README places no constraint, and a
    # Textual widget id must be an identifier: `id = "flac hq"` raised
    # `BadIdentifier: 'preset-flac hq' is an invalid id` out of this call at
    # startup, as would a dot or a leading digit. `_rendered_presets` below is
    # what turns a row back into the preset it stands for.
    self._rendered_presets = self.app.ordered_presets
    items = [
      ListItem(Static(preset.name, markup=False), id=f"preset-{index}")
      for index, preset in enumerate(self._rendered_presets)
    ]
    await listing.extend(items)
    # `ListView` composed empty and populated here: its own `_on_mount` only
    # sets `index` when `self.children` is non-empty *at mount time*, which
    # it never is for us, so nothing ever highlights a row on its own. Set it
    # explicitly on every rebuild so there is always a visible selection and
    # so a reorder actually steers `selected_preset` (via the `Highlighted`
    # message this posts, handled below). `validate_index` clamps this to
    # `None` on its own if `items` is empty.
    #
    # Not unconditionally `0`, though. `watch_probe_result` rebuilds on every
    # probe result, and probes fire per keystroke with no debounce, so an
    # unconditional reset silently dragged the highlight back to row 0 under a
    # user who was mid-selection -- picking a preset takes about as long as a
    # network probe, so that is a live race, not a theoretical one. Row 0 is
    # forced only when the ordering actually changed, which is exactly the
    # case the spec's playlist behaviour needs (playlist presets sort to the
    # top and one is preselected); every same-order rebuild keeps whatever the
    # user had highlighted.
    order = tuple(preset.id for preset in self.app.ordered_presets)
    reordered = order != self._preset_order
    self._preset_order = order
    listing.index = order.index(previous_id) if not reordered and previous_id in order else 0

  def refresh_preview(self) -> None:
    # Both the elided and the full view come from this one `build_command`
    # result (via `App.current_command()`) -- never two separate calls. The
    # spec's invariant is that the command shown is the command that runs;
    # deriving both views from the same argv is what keeps that true even
    # when `extra_args`, the preset, or the URL change underneath the toggle.
    cmd = self.app.current_command()
    note = self.query_one("#command-preview-note", Static)
    if self._show_full_command:
      text = shlex.join(cmd)
      note.display = False
    else:
      visible, hidden = elide_machinery(cmd)
      text = shlex.join(visible)
      # `hidden` is always 4 today (build_command always adds all four
      # machinery flags), but this stays a real conditional rather than a
      # hardcoded label: it is what keeps the count honest if that ever
      # changes, and it is what a hardcoded "+4" would fail to catch.
      note.display = bool(hidden)
      if hidden:
        noun = "flag" if hidden == 1 else "flags"
        note.update(f"+{hidden} machine-readable {noun} hidden · v to show all")
    self.query_one("#command-preview", Static).update(text)

  def action_toggle_full_command(self) -> None:
    self._show_full_command = not self._show_full_command
    self.refresh_preview()

  def _refresh_legend(self) -> None:
    input_widget = self.query_one("#url-input", Input)
    text = LEGEND_INPUT_FOCUSED if input_widget.has_focus else LEGEND_INPUT_BLURRED
    self.query_one("#key-legend", Static).update(text)

  def on_descendant_focus(self, event: DescendantFocus) -> None:
    self._refresh_legend()

  def on_descendant_blur(self, event: DescendantBlur) -> None:
    self._refresh_legend()

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
    #
    # Focus moves to the preset list rather than being dropped with
    # `set_focus(None)`. Dropping it did unblock the letter keys, but it left
    # *nothing* focused, so `↑↓` reached no widget and `⏎` had no handler --
    # the two keys LEGEND_INPUT_BLURRED promises in exactly that state. The
    # only route to a non-default preset was two undocumented `tab` presses,
    # and there was no keyboard route to start a download at all. `ListView`
    # does not override `check_consume_key`, so the letter keys stay live with
    # it focused (verified by real keypresses in
    # tests/test_main_screen_focus_presets.py).
    self.set_focus(self.query_one("#preset-list", ListView))

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

  def _preset_at(self, item: ListItem | None) -> Preset | None:
    """The preset a row stands for, or None for no row / a stale one.

    Reads the row's own position out of its id rather than `ListView.index`:
    `Highlighted` is a message, so by the time it is dispatched the index may
    already have moved on, while the row that was highlighted has not.
    """
    if item is None or item.id is None:
      return None
    index = int(item.id.removeprefix("preset-"))
    if 0 <= index < len(self._rendered_presets):
      return self._rendered_presets[index]
    return None

  def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
    preset = self._preset_at(event.item)
    if preset is not None:
      self.app.selected_preset = preset

  def action_advanced(self) -> None:
    self.app.push_screen(AdvancedScreen())

  def action_download(self) -> None:
    if self.app.url.strip():
      self.app.push_screen(RunScreen(self.app.current_command()))
    else:
      # Pressing enter on an empty box and getting nothing at all reads as a
      # broken key, not as a missing URL. markup=False even though this
      # string is a fixed literal this module owns: the project rule is that
      # nothing on these screens renders with markup on, so there is no site
      # left for the next person to copy the wrong default from.
      self.notify("Enter a URL first.", markup=False)
