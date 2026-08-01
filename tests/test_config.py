from pathlib import Path

from yt_dlp_tui import config


def test_config_path_honours_xdg(monkeypatch):
  monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdgcfg")
  assert config.config_path() == Path("/tmp/xdgcfg/yt-dlp-tui/config.toml")


def test_config_dir_falls_back_to_home(monkeypatch):
  monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
  monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
  assert config.config_dir() == Path("/home/tester/.config/yt-dlp-tui")


def test_archive_path_uses_data_dir(monkeypatch):
  monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdgdata")
  assert config.archive_path() == Path("/tmp/xdgdata/yt-dlp-tui/archive.txt")
