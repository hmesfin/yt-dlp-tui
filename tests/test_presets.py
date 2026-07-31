from pathlib import Path

from yt_dlp_tui.presets import BUILTIN_PRESETS, load_presets, order_for


def test_five_builtin_presets_with_unique_ids():
  assert len(BUILTIN_PRESETS) == 5
  ids = [p.id for p in BUILTIN_PRESETS]
  assert len(set(ids)) == 5


def test_video_preset_targets_mp4_for_compatibility():
  video = next(p for p in BUILTIN_PRESETS if p.id == "video-mp4")
  assert "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b" in video.args
  assert "--merge-output-format" in video.args
  assert video.needs_ffmpeg is True


def test_audio_preset_embeds_thumbnail_and_metadata():
  audio = next(p for p in BUILTIN_PRESETS if p.id == "audio-m4a")
  assert "-x" in audio.args
  assert "--embed-thumbnail" in audio.args
  assert "--embed-metadata" in audio.args


def test_playlist_presets_are_flagged():
  playlist_ids = {p.id for p in BUILTIN_PRESETS if p.is_playlist}
  assert playlist_ids == {"playlist-video", "playlist-audio"}


def test_order_for_playlist_sorts_playlist_presets_first():
  ordered = order_for(BUILTIN_PRESETS, is_playlist=True)
  assert ordered[0].is_playlist and ordered[1].is_playlist


def test_order_for_single_video_keeps_builtin_order():
  assert order_for(BUILTIN_PRESETS, is_playlist=False) == BUILTIN_PRESETS


def test_user_toml_overrides_builtin_by_id(tmp_path: Path):
  cfg = tmp_path / "config.toml"
  cfg.write_text(
    "[[preset]]\n"
    'id = "video-mp4"\n'
    'name = "My video"\n'
    'args = ["-f", "mine"]\n'
    'output_template = "%(title)s.%(ext)s"\n'
  )
  presets = load_presets(cfg)
  video = next(p for p in presets if p.id == "video-mp4")
  assert video.name == "My video"
  assert video.args == ("-f", "mine")
  assert len(presets) == 5  # replaced, not appended


def test_user_toml_appends_new_preset(tmp_path: Path):
  cfg = tmp_path / "config.toml"
  cfg.write_text(
    "[[preset]]\n"
    'id = "flac"\n'
    'name = "Lossless"\n'
    'args = ["-x", "--audio-format", "flac"]\n'
    'output_template = "%(title)s.%(ext)s"\n'
  )
  presets = load_presets(cfg)
  assert len(presets) == 6
  assert presets[-1].id == "flac"


def test_missing_config_returns_builtins(tmp_path: Path):
  assert load_presets(tmp_path / "nope.toml") == BUILTIN_PRESETS
