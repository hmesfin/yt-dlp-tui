"""Textual application shell and shared state.

The URL, selected preset, override set, and last probe result all live here
as reactive attributes so both the main screen and later screens (advanced
options, the run screen) can read and mutate the same source of truth. When
any of them change, `watch_*` pushes a refresh into whichever `MainScreen` is
currently on the stack -- widgets never poll the app for state on their own.
"""

from typing import ClassVar

from textual.app import App
from textual.reactive import reactive

from yt_dlp_tui import config
from yt_dlp_tui.command import Overrides, build_command
from yt_dlp_tui.presets import Preset, load_presets, order_for
from yt_dlp_tui.probe import ProbeResult, Tooling, detect_tooling
from yt_dlp_tui.screens.main import MainScreen


class YtDlpTuiApp(App):
  CSS_PATH = "app.tcss"
  TITLE = "yt-dlp-tui"
  # "q" yields to typing: AUTO_FOCUS puts focus on the URL input at mount,
  # Input claims every printable key for itself (Input.check_consume_key),
  # and Screen._binding_chain strips a claimed key out of the Screen/App
  # bindings map before dispatch even considers them -- so "q" only quits
  # once the URL box isn't focused (see MainScreen's "escape" binding).
  # "ctrl+q" produces no printable character, so Input never claims it: it
  # is the always-live escape hatch regardless of focus.
  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("q", "quit", "quit"),
    ("ctrl+q", "quit", ""),
  ]

  url: reactive[str] = reactive("")
  probe_result: reactive[ProbeResult | None] = reactive(None)
  selected_preset: reactive[Preset | None] = reactive(None)
  overrides: reactive[Overrides] = reactive(Overrides())

  def __init__(
    self,
    presets: tuple[Preset, ...] | None = None,
    tooling: Tooling | None = None,
  ) -> None:
    """`presets` and `tooling` are injectable seams for tests: the defaults
    read the user's real config file and probe PATH for `yt-dlp`/`ffmpeg`,
    neither of which a test suite should depend on."""
    super().__init__()
    # Problems found before the first frame is drawn, shown by `MainScreen`'s
    # startup banner alongside the missing-binary warnings. A list rather than
    # a notify: `on_mount` runs before `push_screen(MainScreen())`, so a toast
    # raised there would be mounted on the default screen and immediately
    # covered by the one being pushed over it.
    self.startup_warnings: list[str] = []
    self.presets = presets if presets is not None else load_presets()
    self.tooling = tooling if tooling is not None else detect_tooling()
    self.selected_preset = self.presets[0]

  @property
  def ordered_presets(self) -> tuple[Preset, ...]:
    is_playlist = bool(self.probe_result and self.probe_result.is_playlist)
    return order_for(self.presets, is_playlist=is_playlist)

  def current_command(self) -> list[str]:
    preset = self.selected_preset or self.presets[0]
    return build_command(
      self.url,
      preset,
      self.overrides,
      ytdlp=self.tooling.ytdlp or "yt-dlp",
      download_dir=config.default_download_dir(),
      archive=config.archive_path(),
    )

  def _main_screen(self) -> MainScreen | None:
    for screen in self.screen_stack:
      if isinstance(screen, MainScreen):
        return screen
    return None

  def watch_url(self, url: str) -> None:
    if (screen := self._main_screen()) is not None:
      screen.refresh_preview()

  def watch_selected_preset(self, preset: Preset | None) -> None:
    if (screen := self._main_screen()) is not None:
      screen.refresh_preview()

  def watch_overrides(self, overrides: Overrides) -> None:
    if (screen := self._main_screen()) is not None:
      screen.refresh_preview()

  async def watch_probe_result(self, result: ProbeResult | None) -> None:
    # Reordering only depends on is_playlist, so a probe result also needs
    # the preset list itself rebuilt, not just the preview text. Async
    # because `refresh_presets()` must await `ListView.clear()`/`.append()`
    # (see screens/main.py) -- Textual detects a watcher returning an
    # awaitable and schedules it via `call_next` automatically.
    if (screen := self._main_screen()) is not None:
      await screen.refresh_presets()
      screen.refresh_preview()

  def _ensure_data_dir(self) -> None:
    """Create the directory the playlist download archive lives in.

    Nothing else in the app ever creates it. yt-dlp's
    `record_download_archive` opens the archive with `locked_file(fn, "a")`,
    which does not create parent directories, and its call site has no guard,
    so on a fresh machine both playlist presets die with `FileNotFoundError`
    -- and only *after* the first item of the playlist has already
    downloaded, because the read path (`in_download_archive`) tolerates a
    missing file while the write path does not. This app never passes
    `--ignore-errors`, so that exception is fatal to the run.

    Deliberately not in `config.py`, whose stated contract is pure path
    computation with no I/O. Deliberately at startup rather than inside
    `action_download`: it is the same directory for every download, creating
    it is idempotent, and doing it once means no download path can forget.
    """
    directory = config.data_dir()
    try:
      directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
      # A read-only home or a plain file in the way. The user may never run a
      # playlist preset, so this cannot be fatal -- but it must not be silent
      # either, since the failure it predicts happens mid-download.
      self.startup_warnings.append(
        f"Could not create {directory} — playlist downloads cannot write their archive ({error})."
      )

  def on_mount(self) -> None:
    self._ensure_data_dir()
    self.push_screen(MainScreen())


def main() -> None:
  YtDlpTuiApp().run()
