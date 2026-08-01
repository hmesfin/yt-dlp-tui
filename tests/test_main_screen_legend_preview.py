"""Coverage for Task 11: the context-sensitive key legend and the elided
command preview on `MainScreen`.

Split out of `test_main_screen.py` rather than appended to it -- that file is
already 393 lines and the ceiling for any file in this project is 500.

Most tests below never type into `#url-input`: they press `escape` and
`v` only after the input is blurred, or set `app.url` / `app.selected_preset`
/ `app.overrides` directly the same way
`test_command_preview_reflects_selected_preset` already does in
`test_main_screen.py`. Neither path fires `Input.Changed`.

One test is the exception on purpose:
`test_v_types_into_input_while_focused_and_does_not_toggle` presses `v`
*while the input still has focus*, specifically to prove it types rather
than toggling -- and typing is exactly what fires `Input.Changed` ->
`on_input_changed` -> `run_worker(self._probe(...))` for a real subprocess.
This file therefore carries its own copy of `test_main_screen.py`'s autouse
`_stub_probe` fixture rather than assume "we don't type" the way an earlier
draft of this file wrongly did -- `yt-dlp` is on PATH in this environment,
so that assumption would have shelled out for real.
"""

import re
import shlex

import pytest
from conftest import DroppedTokens
from textual.widgets import Input, Static

from yt_dlp_tui.app import YtDlpTuiApp
from yt_dlp_tui.command import Overrides
from yt_dlp_tui.presets import BUILTIN_PRESETS
from yt_dlp_tui.probe import ProbeResult, Tooling
from yt_dlp_tui.screens import main as main_screen_module
from yt_dlp_tui.screens.main import MainBody

OFFLINE_TOOLING = Tooling(ytdlp=None, ffmpeg=None)


@pytest.fixture(autouse=True)
def _stub_probe(monkeypatch: pytest.MonkeyPatch) -> None:
  """Same structural guarantee as `test_main_screen.py`'s fixture of the same
  name: no test in this file can reach a real subprocess through `_probe`,
  regardless of what gets typed into `#url-input`."""

  async def default_fake_probe(url: str, *, ytdlp: str) -> ProbeResult:
    return ProbeResult(title="Stubbed Title", duration=90, extractor="Fake")

  monkeypatch.setattr(main_screen_module, "probe", default_fake_probe)


# The spec's four machinery flags, named exactly -- duplicated here rather
# than imported from the implementation so a test bites even if the
# implementation's own notion of "machinery" drifts.
_MACHINERY_FLAG_NAMES = {"--newline", "--no-colors", "--progress-template"}


def _make_app() -> YtDlpTuiApp:
  return YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=OFFLINE_TOOLING)


def _shown_command(app: YtDlpTuiApp) -> list[str]:
  """The preview parsed back into an argv, so it can be diffed against the real
  one. `refresh_preview` renders it with `shlex.join`, so `shlex.split` is its
  exact inverse."""
  return shlex.split(str(app.screen.query_one("#command-preview", Static).content))


async def test_legend_reflects_focus_state_through_escape_and_refocus() -> None:
  """Drives real key presses / clicks -- not `action_blur_url()` directly --
  because the defect this replaces (the empty `Footer`) was invisible to any
  test that didn't route through Textual's real focus and binding chain."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    legend = app.screen.query_one("#key-legend", Static)
    input_widget = app.screen.query_one("#url-input", Input)
    assert app.focused is input_widget

    assert str(legend.content) == "⏎ download · esc for keys · ^q quit"

    await pilot.press("escape")
    assert app.focused is not input_widget
    assert str(legend.content) == "a advanced · q quit · v full command · ↑↓ preset · ⏎ download"

    await pilot.click("#url-input")
    assert app.focused is input_widget
    assert str(legend.content) == "⏎ download · esc for keys · ^q quit"


async def test_elided_preview_omits_machinery_and_keeps_format_output_and_url() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    app.url = "https://example.com/v"
    await pilot.pause()
    preview = str(app.screen.query_one("#command-preview", Static).content)

    for flag in _MACHINERY_FLAG_NAMES:
      assert flag not in preview
    assert "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b" in preview  # the preset's -f selector
    assert "--merge-output-format" in preview
    assert "-o" in preview
    assert "https://example.com/v" in preview


async def test_extra_args_survive_elision() -> None:
  """The over-filtering trap: a user's own `extra_args`, set through
  `Overrides` (the same dataclass `AdvancedScreen.action_save` builds), must
  never be caught by a filter that is supposed to only touch four named
  flags. Also asserts the four machinery flags actually are gone -- without
  that, a no-op "filter" would pass this test for the wrong reason (extra
  args obviously survive a filter that doesn't filter anything)."""
  app = _make_app()
  async with app.run_test() as pilot:
    app.overrides = Overrides(extra_args=("--sleep-requests", "2"))
    await pilot.pause()
    preview = str(app.screen.query_one("#command-preview", Static).content)

    for flag in _MACHINERY_FLAG_NAMES:
      assert flag not in preview
    assert "--sleep-requests" in preview
    assert "2" in preview


async def test_v_after_escape_reveals_full_command_matching_current_command() -> None:
  """The revealed text must equal `shlex.join(current_command())` verbatim --
  not a second, separately-assembled argv -- and the elision note (which only
  makes sense for the elided view) must disappear alongside it."""
  app = _make_app()
  async with app.run_test() as pilot:
    app.url = "https://example.com/v"
    await pilot.pause()
    preview = app.screen.query_one("#command-preview", Static)
    note = app.screen.query_one("#command-preview-note", Static)
    elided = str(preview.content)
    assert "--progress-template" not in elided  # sanity: starts elided
    assert note.display is True

    await pilot.press("escape")
    await pilot.press("v")
    await pilot.pause()

    full = str(preview.content)
    assert full == shlex.join(app.current_command())
    assert "--progress-template" in full
    assert note.display is False


async def test_v_types_into_input_while_focused_and_does_not_toggle() -> None:
  """`v` is printable, so while `#url-input` has focus it must type, exactly
  like `a`/`q` -- not fire the toggle early. Typing does legitimately change
  the preview (the URL tail becomes "-- v" instead of "-- ''"), so this
  checks the *mode* stayed elided rather than the whole string being
  unchanged -- an exact-string check would conflate "still elided" with "the
  URL didn't change", and the latter is not what this test is about."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    input_widget = app.screen.query_one("#url-input", Input)
    assert app.focused is input_widget

    await pilot.press("v")

    assert input_widget.value == "v"  # "v" was typed, not consumed as a binding
    preview_after = str(app.screen.query_one("#command-preview", Static).content)
    note = app.screen.query_one("#command-preview-note", Static)
    assert "--progress-template" not in preview_after  # still elided, not toggled to full
    assert note.display is True
    assert "machine-readable flags hidden" in str(note.content)


async def test_hidden_count_label_reflects_what_the_preview_actually_dropped(
  dropped_tokens: DroppedTokens,
) -> None:
  """The oracle is the difference between the argv that will run and the argv
  on screen -- not a recomputation of the implementation's filtering rule,
  which is what the version this replaces did (and why it could not catch the
  rule being wrong).

  Nothing here names the machinery values, only that four *flags* went and
  that they are the four the spec fixes; the two remaining dropped tokens are
  the payloads those flags carry."""
  app = _make_app()
  async with app.run_test() as pilot:
    app.url = "https://example.com/v"
    await pilot.pause()
    note = str(app.screen.query_one("#command-preview-note", Static).content)

    dropped = dropped_tokens(app.current_command(), _shown_command(app))
    assert [token for token in dropped if token.startswith("--")] == [
      "--newline",
      "--no-colors",
      "--progress-template",
      "--progress-template",
    ]
    assert len(dropped) == 6  # the four flags plus the two template payloads

    match = re.search(r"\+(\d+) machine-readable flags? hidden", note)
    assert match is not None
    assert int(match.group(1)) == 4


async def test_a_users_flag_named_like_machinery_still_shows_with_its_value(
  dropped_tokens: DroppedTokens,
) -> None:
  """The shown-equals-run invariant at the level the user sees it. Filtering by
  token name reached into `extra_args`, so `--postprocessor-args --no-colors`
  rendered as `--postprocessor-args` with the value stripped -- a command that
  reads as broken and is not the one being run."""
  app = _make_app()
  async with app.run_test() as pilot:
    app.url = "https://example.com/v"
    app.overrides = Overrides(extra_args=("--postprocessor-args", "--no-colors"))
    await pilot.pause()

    shown = _shown_command(app)
    assert shown[-4:] == ["--postprocessor-args", "--no-colors", "--", "https://example.com/v"]
    assert len(dropped_tokens(app.current_command(), shown)) == 6
    note = str(app.screen.query_one("#command-preview-note", Static).content)
    assert "+4 machine-readable flags hidden" in note


async def test_toggle_state_persists_across_url_and_preset_changes() -> None:
  """The brief requires the elided/full choice to survive every kind of
  preview refresh -- a URL edit, a preset change, an override save -- until
  the user flips it back. `app.url` / `app.selected_preset` are set directly
  here (as `test_command_preview_reflects_selected_preset` in
  `test_main_screen.py` already does) rather than typed, so this cannot reach
  the probe/subprocess path."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    preview = app.screen.query_one("#command-preview", Static)
    note = app.screen.query_one("#command-preview-note", Static)
    # Confirm the starting point is actually elided -- otherwise the
    # "stays full after each change" assertions below would pass just as
    # well against a preview that was never elided to begin with.
    assert "--progress-template" not in str(preview.content)

    await pilot.press("escape")
    await pilot.press("v")
    await pilot.pause()
    assert str(preview.content) == shlex.join(app.current_command())
    assert note.display is False

    app.url = "https://example.com/v2"
    await pilot.pause()
    assert str(preview.content) == shlex.join(app.current_command())
    assert note.display is False

    app.selected_preset = next(p for p in BUILTIN_PRESETS if p.id == "audio-m4a")
    await pilot.pause()
    assert str(preview.content) == shlex.join(app.current_command())
    assert note.display is False

    app.overrides = Overrides(extra_args=("--sleep-requests", "2"))
    await pilot.pause()
    assert str(preview.content) == shlex.join(app.current_command())
    assert note.display is False


# Narrow terminals (final whole-branch review, finding I5)


def _rendered(widget: Static) -> str:
  """Every rendered line of a widget, joined. A `height: 1` widget renders one
  line however much text it holds, so this shows what was actually cut."""
  return " ".join(widget.render_line(y).text for y in range(widget.size.height)).strip()


async def test_the_elision_note_is_not_cut_off_in_a_narrow_terminal() -> None:
  """`height: 1` truncated the note mid-phrase at 46 columns
  (`+4 machine-readable flags hidden · v to`) and dropped it entirely at 30,
  where the preview is at its most elided and the label matters most. That
  label is what keeps the shortened command honest; a preview that hides four
  flags with nothing on screen saying so is the defect the elision was
  explicitly not allowed to introduce."""
  app = _make_app()
  async with app.run_test(size=(46, 30)) as pilot:
    app.url = "https://example.com/v"
    await pilot.pause()
    note = app.screen.query_one("#command-preview-note", Static)
    assert "v to show all" in _rendered(note)


async def test_the_key_legend_keeps_the_always_live_escape_hatch_when_narrow() -> None:
  """At 30 columns the legend lost `^q quit` -- the one key that works
  regardless of focus, and the only documented way out while the URL box has
  the letter keys."""
  app = _make_app()
  async with app.run_test(size=(30, 30)) as pilot:
    await pilot.pause()
    legend = app.screen.query_one("#key-legend", Static)
    assert "^q quit" in _rendered(legend)


async def test_a_long_probed_title_is_not_truncated_to_one_line() -> None:
  """`#meta` carries a yt-dlp title, which routinely runs past a terminal
  width, and `height: 1` cut it with no ellipsis to say so."""
  title = "A Very Long Video Title That Goes On And On · 12:34 · SomeExtractor"
  app = _make_app()
  async with app.run_test(size=(46, 30)) as pilot:
    await pilot.pause()
    meta = app.screen.query_one("#meta", Static)
    meta.update(title)
    await pilot.pause()
    assert "SomeExtractor" in _rendered(meta)


async def test_the_main_body_scrolls_instead_of_clipping_a_short_terminal() -> None:
  """`height: auto` only fixes text cut sideways. Below ~32 columns the wrapped
  content is taller than the screen, and the plain `Vertical` clipped the tail
  -- the command preview itself was cut mid-URL -- with nothing to indicate it.

  Also pins the constraint that made this change delicate: the scroll container
  must not be focusable, or `AUTO_FOCUS = "*"` gives it the focus that belongs
  to `#url-input`, and the whole keymap ruling rests on the URL box having it
  at launch."""
  app = _make_app()
  async with app.run_test(size=(30, 20)) as pilot:
    app.url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    await pilot.pause()
    body = app.screen.query_one("#main-body", MainBody)
    assert body.max_scroll_y > 0
    assert app.focused is app.screen.query_one("#url-input", Input)


async def test_the_main_body_does_not_scroll_at_an_ordinary_size() -> None:
  """The scrollbar is an overflow affordance, not permanent chrome."""
  app = _make_app()
  async with app.run_test(size=(78, 26)) as pilot:
    app.url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    await pilot.pause()
    assert app.screen.query_one("#main-body", MainBody).max_scroll_y == 0
