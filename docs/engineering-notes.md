# Engineering notes

Findings that cost real time to establish and are not obvious from the code.
The code comments say what each line does; this says why the obvious approach
was tried first and failed.

Textual-specific findings live in [upgrading-textual.md](upgrading-textual.md).
The `--progress-template` gotchas, the subprocess-not-library decision, and the
pinned-yt-dlp reasoning are in
[the design spec](superpowers/specs/2026-07-31-yt-dlp-tui-design.md) and are
not repeated here.

Everything below is POSIX-only. `os.killpg` and `start_new_session` do not
exist on Windows; that is now a hard platform assumption, not an incidental
one.

## Subprocesses

`runner.py` and `probe.py` are the only modules that spawn anything. Both were
written the obvious way first, and the obvious way is wrong in the same place.

### `proc.wait()` and `proc.communicate()` do not mean "the child exited"

They resolve once the child has exited **and** every pipe transport has
disconnected. In CPython's `asyncio/base_subprocess.py`, `_try_finish`
(`:255-262`) returns early unless `all(p.disconnected for p in
self._pipes.values())`, and `_call_connection_lost` (`:264-270`) is the only
thing that wakes the futures waiting on `wait()`. `proc.returncode` is set
independently, in `_process_exited` (`:235`), the moment the child is reaped.

yt-dlp hands its stdout and stderr to ffmpeg and aria2c, so a process that
outlives the child while holding the pipes is the *normal* shape of a merge or
a fragmented download, not an edge case. Reproduced: the child was reaped in
3 ms and `proc.wait()` was still pending 3 s later because a `sleep 30`
grandchild held the inherited pipes.

Nothing in this project awaits `proc.wait()`. Every wait polls
`proc.returncode` on a 20 ms timer through `runner._poll_until`
(`runner.py:38-46`) or `probe._wait_for_exit` (`probe.py:76-90`). That is a
deliberate busy-ish wait: asyncio offers no pipe-independent "the child was
reaped" callback.

Verified by mutation — replacing `_stop`'s wait with
`asyncio.wait_for(proc.wait(), ...)` makes
`test_aclose_is_prompt_when_a_grandchild_holds_the_pipes` fail after 5.08 s.

### Pipe EOF does not mean "the run is over" either

A grandchild holding the pipes keeps them open indefinitely, so a stream that
ends only at EOF never ends. Reproduced: a child that printed one line, spawned
a pipe-inheriting grandchild and exited 0 yielded zero `DoneEvent`s and hung
forever — a breach of the module's one guarantee.

`runner._watchdog` (`runner.py:137-155`) backstops it: once the child is gone,
give the readers `_EOF_GRACE` (1.0 s) to finish on their own, then post the
end-of-output sentinel regardless. EOF stays the normal path so output is still
captured in full.

The cost is honest: lines still in flight more than a second after the child
exited are dropped. Nothing legitimate writes to yt-dlp's stdout that late.

### Cancellation has to reach the whole process group

Signalling the child alone orphans ffmpeg and aria2c. The child is spawned with
`start_new_session=True` and every signal goes through `_signal_group`
(`runner.py:71-86`) to `os.killpg`, escalating SIGTERM → SIGKILL with a bounded
wait at each step (`_stop`, `runner.py:158-167`).

The pgid is derived as `proc.pid` rather than read with `os.getpgid(proc.pid)`.
`setsid()` guarantees the equality, and the derived value stays usable after
the child has been reaped — which is exactly the moment a surviving ffmpeg
still needs the signal, and the moment `os.getpgid()` would raise
`ProcessLookupError`.

Verified by mutation — `start_new_session=False` fails seven tests, including
`test_aclose_kills_the_whole_process_group` with
`AssertionError: child pid ... is still alive`.

`probe.py` deliberately does **not** do this. `yt-dlp -J --flat-playlist` is
metadata-only and never spawns ffmpeg or aria2c, so there is one process to
manage and no group. That reasoning is written on `probe._kill`
(`probe.py:93-111`) and needs revisiting if a probe flag is ever added that
could make an extractor spawn a subprocess (`--check-formats`, `--exec`).

### `StreamReader.readline()` raises on an over-long line, after discarding it

For a line longer than the stream limit, `readline()` raises
`ValueError("Separator is not found, and chunk exceed the limit")` — and clears
its buffer first, so the bytes are unrecoverable. Left uncaught it kills the
reader, and the child then fills the 64 KiB kernel pipe buffer, blocks in
`write()`, and never exits. Reproduced with a child printing one 200 KB line
followed by 20 000 normal ones: zero events, permanent hang, no error.

`_pump` (`runner.py:99-119`) catches it, emits
`LogEvent("[dropped an over-long output line]")` and keeps reading. The limit
is raised to 1 MiB (`_STREAM_LIMIT`). Leaving the pipe unread is what wedges
the child; continuing to read is the whole fix.

The tail of the dropped line still arrives as an ordinary line, so a single
`LogEvent` can carry ~1 MiB. The runner does not truncate — that would silently
alter yt-dlp's output — so `RunScreen` caps instead, per line and per run
(`screens/run.py:69-73, 218-223`).

### `Task.cancel()` suppresses the warning you were going to assert on

```python
# CPython Lib/asyncio/tasks.py, Task.cancel (line 205 in 3.14.0)
self._log_traceback = False
if self.done():
    return False
```

`_log_traceback` is cleared *before* the `done()` check, so cancelling an
already-failed task permanently suppresses its "exception was never retrieved"
warning. `_shutdown` (`runner.py:177-186`) cancels before it gathers, so no
noise can ever reach an `asyncio` exception handler on that path — a test built
on `loop.set_exception_handler` cannot detect a missing `gather`, and one was
written and sailed straight through the mutant.

What the `gather` actually buys is deterministic teardown: helpers are
*finished*, not merely `cancel()`-ed, when `aclose()` returns. Assert that
instead — `asyncio.all_tasks()` listing only unfinished tasks
(`test_aclose_leaves_no_live_helper_tasks`).

Two details were needed to make even that bite: `await agen.aclose()` must be
awaited bare (wrapping it in `wait_for` runs it as its own task, handing the
loop the scheduling turn the cancelled helpers need to finish), and the pipe
holder must survive the group kill, or the readers end on their own and
`cancel()` becomes a no-op.

### `create_subprocess_exec` raises more than `OSError`

A NUL byte in a user-pasted URL raises `ValueError: embedded null byte`, not
`OSError`. Both spawn sites catch `(OSError, ValueError, TypeError)`
(`runner.py:204`, `probe.py:127`), which is also what keeps `run([])` from
raising `IndexError` on `argv[0]`.

### Cancelling a probe must not await anything

`MainScreen` runs `probe()` under `run_worker(..., exclusive=True)`, so every
keystroke cancels the in-flight probe. Before the fix each cancellation
stranded a live `yt-dlp` holding a network connection open, and the 20 s
timeout never got the chance to clean it up. Reproduced with a leaked pid.

The `except asyncio.CancelledError` branch (`probe.py:137-148`) sends one
non-awaiting `proc.kill()` and re-raises. It deliberately does not call
`_kill()`: a second cancellation can land on any `await` inside a cancellation
handler. Reaping happens via the loop's own SIGCHLD bookkeeping.

## yt-dlp

**`--download-archive` does not create its parent directory.**
`record_download_archive` opens the file with `locked_file(fn, "a")`, which
does not create parents, and the call site has no guard. So on a fresh machine
both playlist presets die with `FileNotFoundError` — and only *after* the first
playlist item has already downloaded, because the read path
(`in_download_archive`) tolerates a missing file while the write path does not.
This app never passes `--ignore-errors`, so it is fatal to the run.

Reproduced in-process against the pinned yt-dlp before fixing. `mkdir` lives in
`YtDlpTuiApp._ensure_data_dir` (`app.py:129-155`), called from `on_mount`; a
failure warns instead of crashing, since the user may never run a playlist
preset. It is not in `config.py`, whose stated contract is pure path
computation.

This was missed for the whole build because the one network test uses the
`data-saver` preset, which is not a playlist.

**`json.loads` accepts scalars, lists and `null`.** A syntactically valid but
non-object `PROG:`/`PP:` payload reached `.get()` on an `int` and raised
`AttributeError`, which is not in the caught tuple, out of a parser whose
docstring promises it never raises. `events.py` guards with
`isinstance(d, dict)` and raises `TypeError` so the existing `except` routes it
to a `LogEvent`. Widening the caught tuple with `AttributeError` was rejected:
it would also swallow a genuine typo added later.

## Testing

### Tests that pass against broken code

This happened repeatedly, and never showed up as anything but a green run.

- **The orphan-teardown test, twice.** The first version used an ordinary
  sleeping child: deleting `on_unmount` entirely still passed, because
  Textual's own `cancel_node` plus the incidental loop turns during shutdown
  are more than enough for a SIGTERM to land on a child that dies in ~1 ms. The
  version that bites uses a child with `SIGTERM` set to `SIG_IGN` (so the
  runner must sit out its grace period and escalate) and asserts with
  `time.sleep` only, never `await`, so the loop gets no further turns and a
  kill that was merely *requested* shows up as a live pid.
- **The `#meta` staleness test.** The fake probe resolved with no `await`, so
  the transient window it claimed to pin was unobservable — by assertion time
  the probe had overwritten `#meta` either way. Gate the fake on an
  `asyncio.Event` the test controls rather than reaching for `sleep(0)`.
- **Both `elide_machinery` tests.** They recomputed the expected result with
  the same membership rule the implementation used, one of them claiming
  independent recomputation in its docstring. They could not see a real defect
  where a user's `extra_args` containing `--no-colors` had its value silently
  dropped from the displayed command.
- **`test_blank_fields_clear_overrides`** starts from a default `Overrides()`,
  so it passes even if `action_save` never reads the fields. A seeded variant
  is what proves "blank clears".

The pattern: a test that asserts an end state reachable by accident is not a
test. Before believing a new test, break the line it covers and watch it fail.

### `run_test` hides production bugs

`run_test` and `run_async` tear down in opposite orders — details in
[upgrading-textual.md](upgrading-textual.md). Two real bugs (orphaned downloads
on quit, and a driver crash that hung the app instead of ending it) were
invisible to `run_test` and visible immediately under `run_async`.

Drive `App.run_async(headless=True, auto_pilot=...)` for anything about process
lifetime, app shutdown, or crash handling; `run_test` is fine for rendering.
`tests/test_run_screen_teardown.py` is the split, and its docstring explains
the boundary. One mutation demonstrates the hole rather than asserting it:
putting the driver back on the worker manager fails both `run_async` tests and
*passes* the `run_test` one.

### Keep the suite offline structurally, not by discipline

Typing into `#url-input` fires `Input.Changed` → `_probe` → a real `yt-dlp -J`
subprocess. A test that presses `a` then `q` to check key handling spawned two
real ones, contradicting its own file's docstring. Per-test discipline closes
the instance, not the class: every test file that mounts a screen carries an
`autouse` `_stub_probe` fixture, and `tests/test_run_screen*.py` add an
`autouse` `_guarded_run` that only delegates to the real runner when
`argv[0] == sys.executable`.

`tests/conftest.py` redirects `XDG_CONFIG_HOME` / `XDG_DATA_HOME` into a tmp
directory for the same reason: once `on_mount` started creating the archive
directory, every `run_test` app wrote into the developer's real `$HOME`.

The guarantee is re-checked by putting a logging `yt-dlp` shim first on `PATH`
and running the whole suite against it. Zero spawns.

### Circular oracles

An oracle that recomputes the expected value with the implementation's own rule
proves nothing. `tests/conftest.py::dropped_tokens` returns the tokens the shown
argv is missing relative to the real one and asserts the shown argv is a
*subsequence* of it — eliding may only drop, never reorder or rewrite. It says
nothing about *which* tokens should go; the caller names those from literals in
the test file. The UI-level test re-parses the rendered preview with
`shlex.split` and diffs it against `app.current_command()`.

### Mutation testing

Mutate a committed file and restore with `git checkout --`. Restoring from a
`cp` snapshot cost an hour once: the file was still untracked, the snapshot was
itself already mutated, and `git status` could not warn about it.

Do not mutate a copy of the repo without checking how the copy imports the
package. This checkout installs `yt_dlp_tui` editable, and
`.venv/lib/python3.14/site-packages/_editable_impl_yt_dlp_tui.pth` holds an
absolute path to *this* directory's `src/`. Copy the tree, `.venv` included,
into a scratch directory and the tests there still import the original,
unmutated source and pass — a silent false negative. (The mechanism is verified
in this checkout; no report records it actually biting.)

A mutant that hangs kills pytest at its timeout, and a killed pytest never runs
the `finally` that reaps the test's child. Check for leaked processes after a
mutation run.

## Accepted limitations

Recorded so nobody rediscovers them and files them as bugs.

**A SIGKILLed TUI orphans the download.** `start_new_session=True` puts yt-dlp
in its own session by design, so no exit path runs and nothing in userspace can
fix it. Ctrl-C at the terminal is fine — Textual runs in raw mode and turns it
into a key event, so shutdown runs normally. `kill -INT` on the TUI (not
Ctrl-C) would probably let `asyncio.run`'s cancellation truncate the
escalation; that is an untested hypothesis, and the exposure is unchanged by
anything here.

**Quitting can pause for up to ~10 s.** `on_unmount` awaits the kill, and the
runner's escalation is SIGTERM + 5 s + SIGKILL + 5 s. Deliberate: a slow quit
beats an orphaned download. Only reachable when something ignores SIGTERM.

**Single-letter keys yield to typing.** No `priority=True` anywhere, by owner
ruling. Pressing `q` at first launch types a letter; `escape` is the way out
and the legend advertises it. `ctrl+q` always quits.

**No probe debounce.** The spec asks for one. A probe fires per keystroke, so a
typed 40-character URL spawns and kills ~40 `yt-dlp -J` processes. Pasting is a
single event, which is the common case. This is also what made the preset list
reset under the user mid-selection: `refresh_presets` now forces row 0 only
when the row *ordering* changed (`screens/main.py:242-261`).

Residual, and spec-required: a preset picked while a probe that will reveal a
playlist is in flight is still overridden once by the reorder. That is the
spec's playlist preselect. The rule is symmetric, so a probe flipping
playlist → non-playlist discards the pick too.

**`App._handle_exception` is private API** with an undocumented precondition.
See [upgrading-textual.md](upgrading-textual.md); it is the project's single
largest upgrade hazard and it fails silently.

**The ffmpeg preflight warns but does not disable.** The spec says "warn and
disable". Nothing disables. The warning says the presets will fail partway
through the download, which is the truth
(`probe.preflight_warnings`, `probe.py:159-178`). Disabling is a follow-up.

**`height_cap` and `audio_format` are silent no-ops on a preset that lacks the
flag they edit.** They rewrite an existing `-f` / `--audio-format`; every
built-in carries the one its kind needs, so this only reaches a hand-written
preset. `command.py` is pure and has nothing to warn through, so it is
documented in `build_command`'s docstring and the README rather than notified
at runtime.

**Elision fails toward honesty.** `elide_machinery` (`command.py:141-158`)
matches the exact six-token block `build_command` emits at indices 1..6 and
returns the argv untouched with a count of 0 if it does not match. Showing four
flags that could have been hidden costs a line; hiding a token that is really
running breaks the invariant the preview exists for.

**No keyboard scrolling of the main body** below about 32 columns. A focusable
scroll container would steal `#url-input`'s startup focus and break the keymap
ruling. Mouse-scrollable, and strictly better than the previous behaviour of
clipping with no scrollbar.

**Reported but not chased:** `_UNKNOWN_RETURNCODE = -1` (`runner.py:35`) is
indistinguishable from "killed by SIGHUP" under `DoneEvent`'s
negative-means-signal convention.
