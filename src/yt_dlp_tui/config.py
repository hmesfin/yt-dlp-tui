# src/yt_dlp_tui/config.py
"""Filesystem locations. Pure path computation, no I/O."""
import os
from pathlib import Path

APP_NAME = "yt-dlp-tui"


def config_dir() -> Path:
  base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
  return Path(base) / APP_NAME


def config_path() -> Path:
  return config_dir() / "config.toml"


def data_dir() -> Path:
  base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
  return Path(base) / APP_NAME


def archive_path() -> Path:
  return data_dir() / "archive.txt"


def default_download_dir() -> Path:
  return Path.home() / "Videos"
