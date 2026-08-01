"""End-to-end coverage for the main screen: preset list, live command preview,
playlist reordering, key routing between the URL input and Screen/App
bindings, and the download affordance.

Textual 8.2.8 is installed (the plan targeted >=0.79); a few things differ
from a naive transcription of the plan and are called out inline:

- `Static` no longer exposes `.renderable` -- the public accessor for what
  was set via `.update()` is `.content`.
- `App.query_one`/`.query` do NOT search whatever screen is currently on top
  of the stack. `App._get_dom_base()` returns `default_screen`, which is
  cached once (`_compose_screen`) from `self.screen` at `_on_compose()` time
  -- i.e. the auto-created default screen that exists before `on_mount` ever
  runs `push_screen(MainScreen())` -- and is never refreshed afterwards. So
  `app.query_one("#preset-list")` always misses once a real screen has been
  pushed; querying has to go through `app.screen.query_one(...)` instead.
- `ListView` composed empty and populated later never highlights a row on
  its own: `ListView._on_mount` only sets `index` when `self.children` is
  non-empty *at mount time* (`_list_view.py`), which it never is here. So
  `refresh_presets()` sets `listing.index = 0` itself after every rebuild.
- `Input` claims every printable key for itself
  (`Input.check_consume_key` returns True for any printable character), and
  `Screen._binding_chain` deletes a key an ancestor claims from every
  ancestor's own bindings map before dispatch even runs -- so while the URL
  input has focus (which `App.AUTO_FOCUS = "*"` gives it at mount), bare
  letter bindings like "a" and "q" are unreachable; they type into the box
  instead. `enter` isn't filtered this way (it isn't a printable character),
  but `Input` binds it to its own `action_submit`, and `Screen._check_bindings`
  stops at the *first* namespace with a matching binding while walking the
  chain from the focused widget outward -- `Input` is always first -- so a
  Screen-level `enter` binding would never be reached either way. The design
  (owner-approved after a spec conflict: bare letter bindings vs. an
  auto-focused input can't both fully work) is: typing wins while the URL
  input has focus; `escape` blurs it so `a`/`q` become live; `ctrl+q`
  produces no printable character so `Input` never claims it and it always
  quits; `enter` inside the box triggers download via `Input.Submitted`,
  never via a BINDINGS entry.

Several tests press printable keys into a focused `#url-input`, which fires
real `Input.Changed` events -> `on_input_changed` -> an unconditional
`self.run_worker(self._probe(event.value), exclusive=True)` for any
non-empty value. That is a real subprocess spawn
(`asyncio.create_subprocess_exec` on whatever `yt-dlp` resolves to on PATH)
if left alone, and it is not safe to reason about test-by-test which
keypresses are "non-empty enough" to trigger it -- a prior round of this
file got that wrong (a test that pressed "a" then "q" typed `"aq"` into the
box, which happened to make the real `yt-dlp` exit immediately on today's
machine only because it rejects `"aq"` as an invalid URL; a different string,
or a different `yt-dlp` on PATH, and the offline suite makes a network
call). So the guarantee is structural instead: the autouse `_stub_probe`
fixture below monkeypatches `yt_dlp_tui.screens.main.probe` for *every* test
in this file, before any of them run, whether or not that test's author
remembered a real URL could reach it. A test that needs different probe
behaviour (see `test_typing_a_url_updates_state_and_replaces_stale_meta`)
overrides it locally with the same `monkeypatch` fixture instance. `tooling`
and `presets` are also injected on every app so the suite never reads a real
config file or shells out to detect `yt-dlp`/`ffmpeg`.
"""

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, ListView, Static

from yt_dlp_tui import config
from yt_dlp_tui.app import YtDlpTuiApp
from yt_dlp_tui.presets import BUILTIN_PRESETS, Preset
from yt_dlp_tui.probe import ProbeResult, Tooling
from yt_dlp_tui.screens import main as main_screen_module

OFFLINE_TOOLING = Tooling(ytdlp=None, ffmpeg=None)


@pytest.fixture(autouse=True)
def _stub_probe(monkeypatch: pytest.MonkeyPatch) -> None:
  """Structural guarantee, not per-test discipline: no test in this file can
  reach a real subprocess through `_probe`, regardless of what gets typed
  into `#url-input`, because `probe` is never the real one to begin with."""

  async def default_fake_probe(url: str, *, ytdlp: str) -> ProbeResult:
    return ProbeResult(title="Stubbed Title", duration=90, extractor="Fake")

  monkeypatch.setattr(main_screen_module, "probe", default_fake_probe)


def _make_app(tooling: Tooling = OFFLINE_TOOLING) -> YtDlpTuiApp:
  return YtDlpTuiApp(presets=BUILTIN_PRESETS, tooling=tooling)


def _rendered(widget: Static) -> str:
  """Every rendered line of a widget, spaces removed, so the result does not
  depend on where the text happened to wrap."""
  return "".join(widget.render_line(y).text for y in range(widget.size.height)).replace(" ", "")


async def test_app_starts_and_shows_presets() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    listing = app.screen.query_one("#preset-list", ListView)
    # Rows are keyed by position, not by `preset.id` -- a preset id read from
    # the user's config.toml is not required to be a valid widget id (see
    # tests/test_user_config.py). The order is asserted through the rendered
    # names, which is what the user actually sees.
    assert [item.id for item in listing.children] == [
      f"preset-{index}" for index in range(len(BUILTIN_PRESETS))
    ]
    assert [item.query_one(Static).render_line(0).text.strip() for item in listing.children] == [
      preset.name for preset in BUILTIN_PRESETS
    ]
    # Not just present -- actually highlighted, so the user can see which
    # preset drives the preview on a screen whose whole premise is that.
    assert listing.index == 0
    assert listing.highlighted_child is not None
    assert app.selected_preset == BUILTIN_PRESETS[0]


async def test_command_preview_updates_with_url() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    app.url = "https://example.com/v"
    await pilot.pause()
    preview = app.screen.query_one("#command-preview").content
    assert "https://example.com/v" in str(preview)


async def test_command_preview_reflects_selected_preset() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    app.url = "https://example.com/v"
    app.selected_preset = next(p for p in BUILTIN_PRESETS if p.id == "audio-m4a")
    await pilot.pause()
    assert "--embed-thumbnail" in str(app.screen.query_one("#command-preview").content)


async def test_playlist_probe_reorders_presets_and_steers_selection() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    listing = app.screen.query_one("#preset-list", ListView)
    # By rendered name, since row ids are positional and therefore identical
    # before and after a reorder.
    before_order = [item.query_one(Static).render_line(0).text for item in listing.children]

    app.probe_result = ProbeResult(title="A list", is_playlist=True, playlist_count=20)
    await pilot.pause()

    # The property pinned-behaviour 4 names:
    assert app.ordered_presets[0].is_playlist
    # The actual widget rebuild that property is supposed to drive -- this is
    # the exact path that raised DuplicateIds when clear()/append() weren't
    # awaited, so assert the row order changed and landed correctly rather
    # than just trusting the rebuild happened.
    after_order = [item.query_one(Static).render_line(0).text for item in listing.children]
    assert after_order != before_order
    assert app.ordered_presets[0].name in after_order[0]
    assert all(p.is_playlist for p in app.ordered_presets[:2])
    # And the reorder has to steer selection, not just the list contents:
    # nothing else moves `selected_preset` off the pre-reorder preset.
    assert listing.index == 0
    assert app.selected_preset.is_playlist


async def test_empty_url_does_not_crash_preview() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    app.url = ""
    await pilot.pause()
    preview = str(app.screen.query_one("#command-preview", Static).content)
    # `build_command` appends `["--", url]`; shlex.join renders an empty url
    # as `''`, so the preview should visibly still be a real, well-formed
    # command rather than merely "exists".
    assert preview.endswith("-- ''")


async def test_enter_in_url_input_triggers_download(monkeypatch: pytest.MonkeyPatch) -> None:
  """Presses a real "enter" key rather than calling `on_input_submitted`
  directly: the thing worth pinning is that Textual actually routes
  `Input.Submitted` to that handler while the URL input has focus, not that
  a one-line method calls the method it's named after. Also confirms the
  premise that lets every other test in this file skip a real network probe:
  `Input.action_submit` posts `Submitted` without changing `.value`, so this
  can never fire `Input.Changed` / `_probe`."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    screen = app.screen
    called = []
    monkeypatch.setattr(screen, "action_download", lambda: called.append(True))
    input_widget = app.screen.query_one("#url-input", Input)
    assert app.focused is input_widget

    await pilot.press("enter")

    assert called == [True]
    assert app.url == ""  # Input.Changed never fired -- no probe was scheduled


async def test_letter_keys_type_into_focused_url_input(monkeypatch: pytest.MonkeyPatch) -> None:
  """Owner-approved tradeoff: typing wins over the bare `a`/`q` bindings
  while the URL box has focus (which is where `AUTO_FOCUS` puts it at
  mount)."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    screen = app.screen
    advanced_calls = []
    monkeypatch.setattr(screen, "action_advanced", lambda: advanced_calls.append(True))
    input_widget = app.screen.query_one("#url-input", Input)
    assert app.focused is input_widget

    await pilot.press("a")
    await pilot.press("q")

    assert input_widget.value == "aq"
    assert advanced_calls == []
    assert app.is_running is True


async def test_escape_blurs_input_and_frees_the_letter_keys(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    screen = app.screen
    advanced_calls = []
    monkeypatch.setattr(screen, "action_advanced", lambda: advanced_calls.append(True))
    input_widget = app.screen.query_one("#url-input", Input)

    await pilot.press("escape")
    assert app.focused is not input_widget

    await pilot.press("a")

    assert advanced_calls == [True]
    assert input_widget.value == ""  # "a" fired the action, it did not get typed


async def test_ctrl_q_quits_while_url_input_is_focused() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    assert app.focused is app.screen.query_one("#url-input", Input)
    assert app.is_running is True

    await pilot.press("ctrl+q")

    assert app.is_running is False


async def test_ctrl_q_quits_after_escape() -> None:
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    await pilot.press("escape")
    assert app.is_running is True

    await pilot.press("ctrl+q")

    assert app.is_running is False


async def test_typing_a_url_updates_state_and_replaces_stale_meta(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Drives `on_input_changed` -> `_probe` for real (the autouse fixture
  above keeps `probe` fake, so no real subprocess is spawned). Covers two
  things nothing else in this file does: that Textual actually routes
  `Input.Changed` to the handler that sets `app.url`, and that `#meta`
  doesn't keep showing a previous URL's title once the user starts editing.

  The staleness half only bites during the window between the edit and the
  probe completing -- `on_input_changed`'s own clear vs. `_probe`'s eventual
  overwrite land at the same "instant" if the fake probe resolves
  immediately, and the assertion would pass even with the clear deleted. So
  this overrides the autouse default with a fake that blocks on an
  `asyncio.Event` this test controls, to make that window observable."""
  probe_may_finish = asyncio.Event()

  async def gated_fake_probe(url: str, *, ytdlp: str) -> ProbeResult:
    await probe_may_finish.wait()
    return ProbeResult(title="Stubbed Title", duration=90, extractor="Fake")

  monkeypatch.setattr(main_screen_module, "probe", gated_fake_probe)
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    meta = app.screen.query_one("#meta", Static)
    meta.update("stale title from a previous URL")

    await pilot.press("a", "b", "c")
    await pilot.pause()

    # The probe for "abc" is still blocked on probe_may_finish -- if the
    # interim clear in on_input_changed didn't run, this would still read
    # the pre-edit "stale title..." here.
    assert app.url == "abc"
    assert str(meta.content) == ""

    probe_may_finish.set()
    await pilot.pause()

    assert "Stubbed Title" in str(meta.content)


async def test_bracketed_text_survives_the_meta_and_preview_statics() -> None:
  """Textual 8 parses content markup in `Static.update`, and both of these
  Statics carry text nobody controls: `#meta` shows a probed yt-dlp title and
  `#command-preview` shows `shlex.join(cmd)`, which carries the user's URL and
  `-o` template. With markup on, "[MV] Song" renders as " Song" -- silent
  content loss the user cannot detect, and bracketed titles ("[MV]",
  "[Official Video]", "[4K]") are near-universal on YouTube.

  `.content` reads back the raw string whether or not markup is enabled, so
  asserting on it would not bite; the assertion has to be on the *rendered*
  line.
  """
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    meta = app.screen.query_one("#meta", Static)
    meta.update("[MV] Song [Official Video] · Fake")
    await pilot.pause()
    assert "[MV] Song [Official Video]" in meta.render_line(0).text

    app.url = "https://example.com/watch?v=[abc]"
    await pilot.pause()
    preview = app.screen.query_one("#command-preview", Static)
    # The preview wraps over many lines, so compare the whole rendered block
    # against what was stored. Wrapping only inserts line breaks -- it never
    # deletes characters -- so ignoring spaces makes this wrap-independent,
    # while markup parsing *would* delete the bracketed runs.
    assert _rendered(preview) == str(preview.content).replace(" ", "")
    assert "[abc]" in str(preview.content)


async def test_an_unbalanced_bracket_does_not_crash_the_app() -> None:
  """Worse than mangled text: with markup on, `Static.update("a [/b] c")`
  raises `MarkupError` from inside a reactive watcher, and with Textual's
  default `exit_on_error` that takes the whole app down mid-session. A URL is
  the easiest place for a user to paste one."""
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    app.screen.query_one("#meta", Static).update("a [/b] c")
    app.url = "https://example.com/[/close]"
    await pilot.pause()

    assert app.is_running is True
    preview = app.screen.query_one("#command-preview", Static)
    assert _rendered(preview) == str(preview.content).replace(" ", "")
    assert "[/close]" in str(preview.content)


async def test_bracketed_preset_names_survive_the_preset_list() -> None:
  """Third site of the same markup defect: the preset rows are
  `ListItem(Static(preset.name))`, and `preset.name` comes straight out of the
  user's `config.toml`, which the README tells people to write. `[FLAC]` and
  `[1080p]` are exactly the kind of thing that goes in a preset name."""
  presets = (Preset(id="flac", name="Lossless [FLAC] rip", args=("-x",)), *BUILTIN_PRESETS)
  app = YtDlpTuiApp(presets=presets, tooling=OFFLINE_TOOLING)
  async with app.run_test() as pilot:
    await pilot.pause()
    # The `Static` inside the row, not the `ListItem`: a `ListItem` renders
    # its children through the compositor, so its own `render_line` is blank.
    row = app.screen.query_one("#preset-list", ListView).children[0].query_one(Static)
    assert "Lossless [FLAC] rip" in row.render_line(0).text


async def test_an_unbalanced_bracket_in_a_preset_name_does_not_crash_the_app() -> None:
  """`Static.update`/`Static.render` raise `MarkupError` on an unbalanced tag.
  Here it happens during `MainScreen.on_mount`'s first `refresh_presets()`, so
  the app dies at startup and the user has no way back in short of editing the
  config file they cannot see."""
  presets = (Preset(id="flac", name="Lossless [/b] rip", args=("-x",)), *BUILTIN_PRESETS)
  app = YtDlpTuiApp(presets=presets, tooling=OFFLINE_TOOLING)
  async with app.run_test() as pilot:
    await pilot.pause()
    assert app.is_running is True
    # The `Static` inside the row, not the `ListItem`: a `ListItem` renders
    # its children through the compositor, so its own `render_line` is blank.
    row = app.screen.query_one("#preset-list", ListView).children[0].query_one(Static)
    assert "Lossless [/b] rip" in row.render_line(0).text


# Preflight tool warnings (Task 10)


async def test_preflight_warning_shown_at_mount_and_survives_a_probe() -> None:
  """A regression test for a defect in the original plan: writing the
  preflight warning into `#meta` does not survive contact with this screen,
  because `on_input_changed` clears `#meta` on every keystroke and `_probe`
  overwrites it with the probed title once it resolves -- both of which
  happen the moment a user types a URL, which is the first thing anyone does.
  The warning has to live on a widget neither of those paths ever touches, or
  it is gone exactly when it matters (missing ffmpeg + an audio preset =
  mid-download failure with no warning ever shown).

  `_make_app()`'s default `OFFLINE_TOOLING` has both binaries missing, so
  mount should populate `#tool-warning` with both messages. Then this types a
  URL -- routed through the autouse `_stub_probe` fixture, so no real
  subprocess runs -- and confirms `#meta` updates (the probe path actually
  ran) while `#tool-warning` is untouched.
  """
  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    warning = app.screen.query_one("#tool-warning", Static)
    assert "yt-dlp" in str(warning.content)
    assert "ffmpeg" in str(warning.content)

    await pilot.press("a", "b", "c")
    await pilot.pause()

    meta = app.screen.query_one("#meta", Static)
    assert "Stubbed Title" in str(meta.content)  # the probe path really ran
    assert "yt-dlp" in str(warning.content)
    assert "ffmpeg" in str(warning.content)


async def test_no_preflight_warning_when_tooling_is_complete() -> None:
  """`str(content) == ""` alone would still pass if the `if warnings:` guard
  in `on_mount` were deleted outright (`" ".join([])` is also `""`), so it
  wouldn't actually pin the guard. `display is False` does: it only holds if
  something explicitly hides the widget when there is nothing to say, which
  also closes the layout bug where an empty `height: auto` Static still
  reserved a blank row above `#url-input`."""
  tooling = Tooling(ytdlp="/usr/bin/yt-dlp", ffmpeg="/usr/bin/ffmpeg")
  app = _make_app(tooling)
  async with app.run_test() as pilot:
    await pilot.pause()
    warning = app.screen.query_one("#tool-warning", Static)
    assert warning.display is False


# The playlist archive directory (final whole-branch review, finding C3)


async def test_the_archive_directory_exists_before_a_download_can_start(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  """Both playlist presets pass `--download-archive` and nothing in `src/` ever
  created its parent directory. yt-dlp's `record_download_archive` opens the
  file with `locked_file(fn, "a")`, which does not create parents, and its call
  site has no guard -- so on a fresh machine a playlist download dies with
  FileNotFoundError *after* the first item is already on disk. The read path
  tolerates a missing file, which is why it survives until then.

  Asserted through a real app start rather than by calling a helper, because
  "something creates it before yt-dlp is ever spawned" is the actual claim.
  """
  monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
  archive = config.archive_path()
  assert not archive.parent.exists()

  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    assert archive.parent.is_dir()


async def test_an_uncreatable_archive_directory_warns_instead_of_crashing(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  """`mkdir` is I/O and can fail (read-only home, a plain file in the way).
  Failing to create a directory the user may never need must not take the app
  down before it has drawn a single frame -- it goes in the same startup
  banner the missing-binary warnings use."""
  blocker = tmp_path / "share"
  blocker.write_text("this is a file, not a directory")
  monkeypatch.setenv("XDG_DATA_HOME", str(blocker))

  app = _make_app()
  async with app.run_test() as pilot:
    await pilot.pause()
    assert app.is_running is True
    assert "archive" in str(app.screen.query_one("#tool-warning", Static).content)
