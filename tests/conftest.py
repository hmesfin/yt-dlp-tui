"""Suite-wide guards that must not depend on any individual test remembering
them -- the same structural approach as the per-module `_stub_probe` fixtures.
"""

from collections.abc import Callable

import pytest

DroppedTokens = Callable[[list[str], list[str]], list[str]]


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


@pytest.fixture
def dropped_tokens() -> DroppedTokens:
  """Return the tokens `shown` is missing relative to `full`, in order.

  An oracle for `elide_machinery`, shared by `test_command.py` (which checks
  the function) and `test_main_screen_legend_preview.py` (which checks what the
  screen actually renders). It says nothing about *which* tokens should go --
  the caller asserts that against the spec's named flags -- so a test using it
  cannot pass by restating the implementation's own filtering rule, which is
  exactly how the previous pair of tests missed a real defect.

  Also asserts `shown` is a subsequence of `full`: eliding may only ever drop
  tokens, never reorder or rewrite them, or the shown-equals-run invariant is
  gone before the count is even considered.
  """

  def _dropped(full: list[str], shown: list[str]) -> list[str]:
    remaining = iter(shown)
    expected = next(remaining, None)
    missing: list[str] = []
    for token in full:
      if token == expected:
        expected = next(remaining, None)
      else:
        missing.append(token)
    assert expected is None, f"{shown!r} is not a subsequence of {full!r}"
    return missing

  return _dropped
