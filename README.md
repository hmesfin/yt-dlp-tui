# yt-dlp-tui

A terminal UI for yt-dlp that puts named presets first, so ordinary downloads
don't require remembering flags.

The assembled command is always on screen before it runs. That's deliberate —
the tool should teach the flags rather than hide them. The preview elides the
four flags that are pure plumbing (`--newline`, `--no-colors`, both
`--progress-template` pairs) so the flags actually worth learning aren't
buried in JSON; `v` (after `escape`) reveals the exact command verbatim.

```
┌─ yt-dlp-tui ─────────────────────────────────┐
│ URL  https://youtube.com/watch?v=…           │
│      Rick Astley - Never Gonna Give You Up   │
│      3:33 · Youtube                          │
├──────────────────────────────────────────────┤
│ ▸ Best video (mp4, plays everywhere)         │
│   Audio only  →  m4a + cover + tags          │
│   Playlist  →  video, archived               │
│   Playlist  →  audio, archived               │
│   Data saver  (720p cap)                     │
├──────────────────────────────────────────────┤
│ yt-dlp -f 'bv*[ext=mp4]+ba[ext=m4a]/…        │
│ +4 machine-readable flags hidden · v to      │
│ show all                                     │
└──────────────────────────────────────────────┘
```

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- `ffmpeg` on PATH, for the presets that merge streams or extract audio

yt-dlp is a project dependency, not a system package. That matters: a
distro-packaged yt-dlp goes stale within weeks and stops working against
YouTube. Running through `uv` uses the pinned one.

At startup the app checks for both binaries on PATH and shows a warning above
the URL box if either is missing — before you pick a preset that needs it,
not after a download fails partway through.

## Run it

```bash
uv sync
uv run yt-dlp-tui
```

## Keys

The URL box has focus at launch, so it gets every letter you type. Press
`escape` to leave it — focus moves to the preset list, and the single-letter
keys go live.

| key | does |
|---|---|
| `enter` | start the download (from either the URL box or the preset list) |
| `escape` | leave the URL box for the preset list |
| `↑` `↓` | change preset (after `escape`) |
| `a` | advanced options (after `escape`) |
| `q` | quit (after `escape`) |
| `v` | toggle the full, unelided command (after `escape`) |
| `ctrl+q` | quit, from anywhere |

`tab` moves between the URL box and the preset list either way, if you'd
rather not use `escape`. Clicking a preset selects it; it does not start the
download.

The bar at the bottom of the main screen always reflects which of these are
live: it reads `⏎ download · esc for keys · ^q quit` while the URL box has
focus, and switches to `a advanced · q quit · v full command · ↑↓ preset ·
⏎ download` once `escape` has left it.

In the advanced drawer: `ctrl+s` saves, `escape` cancels.
On the run screen: `c` cancels the download, `escape` goes back.

Cancelling kills yt-dlp and everything it spawned, including ffmpeg. Quitting
the app while a download runs does the same.

## Presets

Five ship built in:

| id | what it does |
|---|---|
| `video-mp4` | best video as mp4, plays everywhere |
| `audio-m4a` | audio only, m4a with cover art and tags |
| `playlist-video` | playlist as video, archived so reruns skip what you have |
| `playlist-audio` | playlist as audio, archived |
| `data-saver` | capped at 720p |

Paste a playlist URL and the playlist presets sort to the top.

## Config

Add or replace presets in `~/.config/yt-dlp-tui/config.toml` (or
`$XDG_CONFIG_HOME`). A preset with an existing `id` replaces the built-in; a
new `id` is appended.

```toml
[[preset]]
id = "flac"
name = "Lossless"
args = ["-x", "--audio-format", "flac"]
output_template = "%(title)s.%(ext)s"
```

Downloads go to `~/Videos` unless you override the output directory in the
advanced drawer. The playlist archive lives at
`~/.local/share/yt-dlp-tui/archive.txt`.

## Development

```bash
uv run pytest              # offline
uv run pytest -m network   # one real download against a live URL
uv run ruff check src tests
```

The offline suite spawns no real yt-dlp and makes no network calls. Subprocess
tests drive `sys.executable -c` instead. The one exception is the `network`
marker, deselected by default, which downloads a small Creative Commons clip
end to end to prove the whole pipeline against a real `yt-dlp`.

Architecture: `command.py` turns (url, preset, overrides) into an argv list and
is pure, so every flag decision is tested offline. `runner.py` is the only
module that touches subprocesses. `events.py` parses one output line into one
typed event and never raises. The Textual screens render what those produce.
