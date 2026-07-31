# yt-dlp-tui — Design

Date: 2026-07-31
Status: Approved, pending implementation plan

## Problem

`yt-dlp` is excellent and nearly unusable from memory. The flags needed for
ordinary tasks — a video that plays everywhere, an audio rip with tags, a
playlist you can resume — are long, easy to get subtly wrong, and forgotten
between uses. The cost is recall.

This tool replaces recall with recognition: name the handful of things you
actually do, and let the machine remember the incantations.

## Scope

Three use cases, confirmed with the user:

1. Single video, good quality, plays everywhere
2. Audio rip / podcast, with cover art and metadata
3. Playlists and channels in bulk, skipping what's already downloaded

Out of scope for v1: subtitles, SponsorBlock, persistent queue, concurrent
downloads, history database, cookies/auth, format-explorer table.

## Interaction model

Preset-first. URL plus a named preset gets you downloading in about two
keystrokes. An advanced drawer exposes the few knobs worth exposing.

```
┌─ yt-dlp-tui ─────────────────────────────────┐
│ URL  https://youtube.com/watch?v=dQw4w9WgXcQ │
│      Rick Astley - Never Gonna Give You Up   │
│      3:33 · YouTube                          │
├──────────────────────────────────────────────┤
│ PRESET                                       │
│ ▸ 1  Best video (mp4, everywhere-playable)   │
│   2  Audio only  → m4a + cover + tags        │
│   3  Playlist → video, archived              │
│   4  Playlist → audio, archived              │
│   5  Data saver  (720p cap)                  │
├──────────────────────────────────────────────┤
│ → ~/Videos/Rick Astley - Never Gonna….mp4    │
│ yt-dlp -f 'bv*[ext=mp4]+ba[ext=m4a]/b' …     │
│                                    [a] edit  │
├──────────────────────────────────────────────┤
│ ⏎ download   a advanced   o output   q quit  │
└──────────────────────────────────────────────┘
```

The assembled command is always visible before it runs. This is a feature, not
debug output: the tool should teach the flags rather than hide them forever.

### Run screen

One job at a time. A playlist is one job with an N-of-M counter. No queue, no
concurrency.

```
┌─ downloading ────────────────────────────────┐
│ Lofi Girl — beats to relax                   │
│ item 14/87                                   │
│ ▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░  63%  4.2 MiB/s  0:11   │
│ [download] 100% of 8.2MiB → 014.m4a          │
│ [ExtractAudio] Destination: 014.m4a          │
│ 12 done · 1 skipped · 0 failed               │
│ ^C cancel   l full log                       │
└──────────────────────────────────────────────┘
```

## Architecture

```
src/yt_dlp_tui/
  command.py    preset + url + overrides → argv list      ← pure, no I/O
  presets.py    Preset dataclass, built-in defaults, TOML loading
  runner.py     async subprocess driver → event stream     ← only module touching subprocess
  probe.py      metadata fetch via yt-dlp -J
  config.py     XDG paths, defaults
  screens/      main.py · advanced.py · run.py
  app.py        Textual App, screen wiring
```

Python, `uv`, `src/` layout, matching the sibling tools in `cli-tools/`.

### The load-bearing decision

`command.py` is pure. Every bit of "which incantation means what" lives in one
function taking data and returning an argv list, with no side effects. The hard
part of this project is therefore exhaustively testable without a network or a
subprocess, and `runner.py` stays a dumb pipe.

Each module answers: what does it do, how is it used, what does it depend on.
`command.py` depends on nothing. `runner.py` depends on argv and produces
events. The screens depend on both and own no domain logic.

### Driving yt-dlp: subprocess, not library

Rejected: `import yt_dlp` with `progress_hooks`.

The decisive reason is honesty. The UI promises to show the real command. Via
the library that displayed string is a reconstruction — plausible-looking, and
guaranteed to drift from what actually executes. The tool's teaching value rests
entirely on that line being true. A subprocess makes it true by construction.

Secondary: immune to yt-dlp version churn, Ctrl-C is a clean process kill rather
than fighting a thread, and a broken yt-dlp cannot take the TUI down with it.

### Progress without regex scraping

Most yt-dlp wrappers scrape the human-readable progress bar with regexes. That
breaks whenever the output format is tweaked.

Instead, hand yt-dlp a template and it emits whatever shape we ask for:

```
--newline --no-colors --progress-template \
  'PROG:{"bytes":%(progress.downloaded_bytes|0)j,"total":%(progress.total_bytes|0)j,
         "spd":%(progress.speed|0)j,"eta":%(progress.eta|0)j,
         "idx":%(info.playlist_index|0)j,"n":%(info.n_entries|0)j,
         "title":%(info.title|"")j}'
```

- `--newline` stops carriage-return redraws, so each update is its own line.
- `j` is JSON encoding of the field.
- `|0` / `|""` supplies a default.

**Verified 2026-07-31** against a real download (Big Buck Bunny,
`aqz-KE-bpKQ`): the template emits one valid-JSON line per update.

**Gotcha 1, verified:** without any `|default`, missing values render as bare
`NA`, which is invalid JSON. The final progress line emits `"eta":NA` and a
naive `json.loads` throws exactly at completion.

**Gotcha 2, verified 2026-07-31 during implementation — a `|default` alone is
not sufficient.** When a field's value is `None`, yt-dlp substitutes the default
as a raw literal and *skips the `j` JSON conversion entirely* (`create_key()`
sets `value, fmt = default, 's'` before the `j` branch is reached). So an empty
default renders nothing at all:

| template | value | renders | result |
|---|---|---|---|
| `%(info.title\|)j` | `None` | `{"title":}` | invalid JSON |
| `%(info.title\|"")j` | `None` | `{"title":""}` | valid |
| `%(progress.eta\|0)j` | `None` | `{"eta":0}` | valid |

Numeric defaults are safe only by coincidence — a bare `0` is already valid
JSON. **String fields must quote the default: `|""`, not `|`.** Every field
needs a default, and that default must itself be valid JSON.

Playlist position comes from `info.playlist_index` / `info.n_entries` in the
template, not from scraping `Downloading item N of M`.

**Verified 2026-07-31 (was previously unconfirmed):** `--progress-template`
accepts a `[TYPES:]` prefix, and a second `--progress-template
'postprocess:PP:{…}'` flag coexists with the download one. It emits
`{"st":"started"|"finished","pp":"ExtractAudio"}` per postprocessor.

Important limit: postprocess events carry **no byte or percentage progress** —
only start and finish per stage. The UI can therefore show "Extracting audio…"
as an indeterminate status but cannot show a percentage during ffmpeg work.

Also verified: `_type` in the probe JSON is `"video"` vs `"playlist"`, which is
the playlist-detection mechanism; and `playlist_count` is returned even under
`--playlist-items 1`, so the probe stays cheap on large channels.

The comma-fallback template syntax `%(progress.total_bytes,progress.total_bytes_estimate|0)j`
parses and runs. Used defensively for live/DASH streams lacking `total_bytes`;
note the fallback branch itself has not been exercised.

### yt-dlp version: pinned, not system

yt-dlp is a project dependency managed by `uv`. A config key may point at a
different binary.

This is not a style preference. **Verified 2026-07-31:** the system yt-dlp on
this machine (2024.04.09, distro-packaged) cannot download from YouTube at all —
every player API request returns HTTP 400, leaving only images available. yt-dlp
ships roughly weekly because sites keep breaking it, so any distro package is
structurally always stale.

## Presets

Five, each a complete named intent.

| # | Preset | Core flags |
|---|--------|-----------|
| 1 | Best video (mp4, plays everywhere) | `-f 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b' --merge-output-format mp4` |
| 2 | Audio only → m4a + cover + tags | `-x --audio-format m4a --embed-thumbnail --embed-metadata` |
| 3 | Playlist → video, archived | preset 1 + `--download-archive` + indexed output template |
| 4 | Playlist → audio, archived | preset 2 + same |
| 5 | Data saver (720p cap) | `-f 'bv*[height<=720]+ba/b[height<=720]'` |

Preset 1 targets avc1+aac specifically rather than "best", because best is often
VP9 or AV1, which chokes on older players. That is the mp4-plays-everywhere
promise.

Presets 3 and 4 use `--download-archive` plus `--no-overwrites` and an indexed
output template (`%(playlist)s/%(playlist_index)03d - %(title)s.%(ext)s`).

### Why presets and not preset × toggle

Presets 1 and 2 are format choices; "archive" is a behavior. Strictly they are
orthogonal and could be composed from toggles. Deliberately not doing that:
combinatorial toggles are how you arrive back at "too many options to remember."
Five concrete named things you recognize beats two axes you must compose. The
duplication lives in a TOML file rather than in the user's head.

### Playlist detection

When the probe reports a playlist or channel, presets 3 and 4 sort to the top
and preselect.

## Config

`~/.config/yt-dlp-tui/config.toml`, shipped with the built-in presets and
user-editable. `e` opens `$EDITOR`.

This is what keeps the built-in list short: anything idiosyncratic becomes the
user's own preset rather than another permanent control in the UI.

The advanced drawer exposes exactly four things:

- output directory
- resolution cap
- audio format
- **extra flags**, free text, appended verbatim

Extra flags is the pressure valve. Anything the tool does not model can still be
typed, and it appears in the previewed command so its effect is visible.

## Data flow

1. URL entry, debounced, triggers `probe.py`: `yt-dlp -J` for title, duration,
   extractor, playlist count. Probe runs async; the UI stays responsive.
2. Probe failure is **non-blocking** — it degrades to "unknown" and still allows
   downloading. A probe that fails does not imply the download will.
3. Enter → `command.py` builds argv → `runner.py` spawns it.
4. Stdout lines matching `PROG:` parse as JSON into progress events. All other
   stdout and all stderr become log lines. Exit code determines success.

## Error handling

- yt-dlp missing or broken: detected at startup, clear message, no crash.
- ffmpeg missing: warn and disable the presets requiring it (audio extraction,
  merge) rather than failing mid-download.
- Probe failure: non-blocking, as above.
- Download failure: exit code plus trailing stderr shown in the log, and a copy
  command key so the exact invocation can be rerun outside the TUI to debug.
- Ctrl-C: SIGINT to the child so yt-dlp cleans up its own partial files.
- Malformed `PROG:` line: skipped, logged at debug, never crashes the UI.

## Testing

TDD. Tests first, per project rules.

- `command.py`: table-driven, every preset × URL-shape → exact expected argv.
  This is the bulk of the suite and requires no network or subprocess.
- `runner.py` parser: canned stdout fixtures fed through the event parser,
  including the bare-`NA` line and a malformed line.
- `probe.py`: canned `-J` JSON fixtures.
- Screens: Textual `App.run_test()` headless pilot.
- One integration test against Big Buck Bunny (`aqz-KE-bpKQ`, Creative Commons,
  verified working 2026-07-31), marked `network`, skipped by default.

## Known future work

Cookies/auth is the most likely thing to be needed next. YouTube increasingly
gates content behind bot checks, and the fix is `--cookies-from-browser`. It
belongs in the advanced drawer when that happens, not before.
