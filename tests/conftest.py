"""Suite-wide guards that must not depend on any individual test remembering
them -- the same structural approach as the per-module `_stub_probe` fixtures.
"""

import pytest


@pytest.fixture(autouse=True)
def _xdg_dirs_in_tmp(
  tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
  """Point `XDG_CONFIG_HOME` / `XDG_DATA_HOME` at a throwaway directory.

  `YtDlpTuiApp.on_mount` creates the download-archive directory (without it,
  both playlist presets fail mid-download on a fresh machine), so every test
  that starts an app now writes to whatever `config.data_dir()` resolves to --
  which is the developer's real `~/.local/share` unless something moves it.
  A test suite has no business creating directories under `$HOME`.

  Function-scoped and applied before the test body, so a test that sets either
  variable itself (`tests/test_config.py`, the archive tests in
  `tests/test_main_screen.py`) still wins.
  """
  base = tmp_path_factory.mktemp("xdg")
  monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "config"))
  monkeypatch.setenv("XDG_DATA_HOME", str(base / "share"))
