# Upgrading Textual

`pyproject.toml` pins `textual>=8.2.8`. Every claim below was checked against
the installed 8.2.8 source under `.venv/lib/python3.14/site-packages/textual/`;
line numbers refer to that copy.

The floor is the exact version this was built against, not the earliest version
that works. Nobody could confirm offline when `Static.content`, the `markup=`
constructor argument, or the Content/Visual rendering rewrite landed, so the
one verified number was used rather than a guessed lower one. It can be
loosened by anyone who checks the history.

The dangerous findings here are the ones that change behaviour **silently**:
markup parsing, key consumption, `ListView` index handling, worker-cancel
ordering. None of them raise. A green suite after an upgrade is not evidence
that they still hold.

## Checklist

Work through this after any Textual bump. The "catches it" column is the test
that fails if the behaviour flips; where it is blank, nothing in the suite will
tell you.

| Check | Failure if it changes | Catches it |
|---|---|---|
| `Static` still parses markup by default | Bracketed titles/preset names render mangled or raise `MarkupError` from a watcher | `test_bracketed_text_survives_the_meta_and_preview_statics`, `test_a_title_full_of_brackets_is_shown_literally`, `test_bracketed_preset_names_survive_the_preset_list` |
| `App.notify` still parses markup by default | Toast text mangled; `[/b]` in a value kills the app | `test_notify_echoes_a_bracketed_value_verbatim`, `test_notify_with_an_unbalanced_bracket_does_not_kill_the_app` (both need `run_test(notifications=True)`) |
| `Input.check_consume_key` still claims every printable char | `a`/`q`/`v` start firing bindings while the user types a URL; or stop working after `escape` | `test_letter_keys_type_into_focused_url_input`, `test_letter_keys_and_q_still_fire_with_the_preset_list_focused` |
| `ListView` still claims `enter` | `⏎ download` in the blurred legend goes dead, or fires twice | `test_enter_after_escape_starts_the_download` |
| `ListView.Selected` still fires on mouse click | If `Selected` were ever wired to download, every click would start one | `test_clicking_a_preset_row_selects_it_without_starting_a_download` |
| `ListView.clear()` still needs awaiting, still sets `index = None` | `DuplicateIds` on the second rebuild, or no row highlighted at all | `test_app_starts_and_shows_presets`, `test_playlist_probe_reorders_presets_and_steers_selection` |
| `WorkerManager.cancel_all()` still fires before `Unmount` under `run_async` | The run driver could be moved back onto the worker manager and orphan downloads again | `test_quitting_a_real_app_run_leaves_no_orphaned_download`, `test_cancelling_then_quitting_leaves_no_orphaned_download` — **not** the `run_test` orphan test, which passes either way |
| `App._handle_exception` / `_fatal_error` still read `sys.exc_info()` | A crash in the run driver stops ending the app; raw stderr paints over the live TUI | `test_a_crash_in_the_driver_ends_the_app_and_kills_the_child` |
| Screen children still pruned before `Unmount` | Either the `is_running` guard becomes dead code, or (if reversed) late events crash the driver | `test_events_arriving_after_the_screen_is_popped_do_not_crash` |
| `App.query_one` still ignores pushed screens | Tests silently query the wrong screen | Nothing. Tests use `app.screen.query_one`; if the API changed they would keep passing |
| `AUTO_FOCUS = "*"` still lands on `#url-input`, not `MainBody` | Legend starts in the wrong state; the whole keymap ruling breaks | ~10 tests fail, starting with `test_app_starts_and_shows_presets` |
| `height: auto` still refuses to collapse an empty `Static` | A blank row above `#url-input` when there are no warnings | `test_no_preflight_warning_when_tooling_is_complete` (asserts `display is False`) |
| Base `App.BINDINGS` still ships a priority `ctrl+q` | `ctrl+q` stops quitting; the explicit entry in `YtDlpTuiApp.BINDINGS` does not actually cover it | `test_ctrl_q_quits_while_url_input_is_focused`, `test_ctrl_q_quits_after_escape` |

Also run the suite with `-W error::RuntimeWarning` and `PYTHONASYNCIODEBUG=1`.
Textual ordering changes tend to show up first as unretrieved-task noise.

## Focus and key dispatch

**`Input` eats every printable key, and the chain strips it from ancestors.**
`Input.check_consume_key` (`widgets/_input.py:498-510`) returns `True` for any
printable character. `Screen._binding_chain` (`screen.py:408-435`) walks from
the focused widget up to the App and deletes any key an earlier namespace
claims from every later namespace's binding map, before `App._check_bindings`
(`app.py:3966`) looks at it. With `App.AUTO_FOCUS = "*"` focusing the URL
input at mount, `a`, `q` and `v` are dead there.

Two consequences beyond the obvious one. `escape` produces no printable
character, is never claimed, and is therefore the only reliable way out
(`screens/main.py:320-334`). And the auto-generated `Footer` rendered *empty*:
it draws `screen.active_bindings` where `show=True`, and those keys never
reach it. That is why `MainScreen` uses a hand-written `#key-legend` Static
keyed to real focus state (`screens/main.py:31-47, 291-300`) rather than a
`Footer`.

The owner's ruling was that typing wins: no `priority=True` anywhere. Accepted
cost — pressing `q` at first launch types a letter instead of quitting.

**`ctrl+q` already works without help.** Base `App.BINDINGS` (`app.py:454-461`)
ships `Binding("ctrl+q", "quit", show=False, priority=True)`. Priority bindings
are resolved in an earlier pass over `reversed(_binding_chain)`, which is not
subject to the consume filter. The explicit `("ctrl+q", "quit", "")` in
`YtDlpTuiApp.BINDINGS` (`app.py:34`) is never reached. It is kept because the
approved keymap named it; removing it changes nothing.

**`ListView` owns `enter`.** `_check_bindings` stops at the first namespace in
the chain with a matching key, so a Screen-level `enter` binding is unreachable
while the list has focus. `PresetList` (`screens/main.py:77-99`) subclasses
`ListView` and rebinds `enter` to `screen.download`; the `screen` namespace
resolves through `App._action_targets` (`app.py:652`).

Handling `ListView.Selected` on the screen would have been shorter and is
wrong: `_on_list_item__child_clicked` (`widgets/_list_view.py:388-392`) posts
`Selected` for a **mouse click** too, so a click would start a download and a
mouse user could never just change preset.

**`ScrollableContainer` is focusable.** `VerticalScroll` declares
`can_focus=True`, so with `AUTO_FOCUS = "*"` it takes the focus that belongs to
`#url-input`. Swapping the content `Vertical` for a plain `VerticalScroll`
fails ten tests. `MainBody` (`screens/main.py:50-74`) is declared
`can_focus=False`; the cost is that the body scrolls with the mouse but not the
keyboard.

## Rendering

**`Static` parses content markup by default** (`markup: bool = True`,
`widgets/_static.py`), in the constructor and in `update()`. This app renders
yt-dlp titles, `shlex.join`ed argv, and preset names out of the user's
`config.toml` — all full of brackets. Measured: `[MV] Title` renders as
`" Title"`, and `"[/b]"` raises `MarkupError` from inside `Static.update`,
which with `exit_on_error=True` takes the app down mid-download. Every `Static`
in this app is constructed with `markup=False` (`screens/main.py:154, 165, 167,
185, 190, 237`; `screens/run.py:111-114`).

**A `.content` assertion does not test this.** `Static.content`
(`widgets/_static.py:66-68`) returns the original object, markup and all. A
test asserting on `.content` passes whether markup is on or off. Assert on
`render_line(0).text`. For a `ListItem`, assert on the inner `Static` — a
`ListItem` renders its children through the compositor, so its own
`render_line` is blank either way.

**`App.notify` defaults to `markup=True`** (`app.py:4628`). All three call
sites pass `markup=False` (`screens/main.py:379`, two in
`screens/advanced.py`).

**`run_test` defaults to `notifications=False`** (`app.py:2139`), so no `Toast`
ever renders and the notify half of the markup problem is invisible to the
suite. The two tests that cover it pass `notifications=True`
(`tests/test_advanced_screen.py`).

**`height: auto` does not collapse an empty `Static` to zero rows.** Measured:
with complete tooling the empty warning banner still reserved a row and pushed
`#url-input` from y=2 to y=4. `display` is the switch —
`banner.display = bool(warnings)` (`screens/main.py:194-201`).

**`height: 1` truncates rather than wraps.** At 46 columns the elision note was
cut mid-phrase; at 30 columns it disappeared entirely. The three text widgets
use `height: auto` in `app.tcss`.

## Screens and queries

**`App.query_one` never sees a pushed screen.** `App._get_dom_base`
(`app.py:941`) returns `default_screen`, which is `_compose_screen` if set
(`app.py:954-956`). `_compose_screen` is assigned exactly once, in
`App._on_compose` (`app.py:3546`), to whatever `self.screen` was before
`on_mount` ran — the auto-created `Screen(id="_default")` — and is never reset.
After `push_screen(MainScreen())`, `app.query_one(...)` keeps querying the
empty default screen while `app.screen` correctly returns `MainScreen`. Tests
use `app.screen.query_one(...)`. `Screen.query_one` called from inside a screen
is unaffected: it queries `self`.

**`ListView` composed empty never highlights a row.** `ListView._on_mount`
(`widgets/_list_view.py:159-170`) sets `index` only if `self.children` is
non-empty *at mount time*, and `clear()` (`:261-270`) explicitly sets
`index = None`. A list composed empty and populated later has no selection and
never steers `selected_preset`. `refresh_presets` sets `listing.index` on every
rebuild (`screens/main.py:258-261`).

**`clear()` / `append()` / `extend()` must be awaited if two calls can overlap.**
They return optionally-awaitable objects. `clear()` only *posts* a `Prune`; it
does not detach children synchronously. Mounting does register children
synchronously. So an un-awaited `clear()` followed by a later rebuild raises
`DuplicateIds` on the reused row ids. `refresh_presets` is `async` and awaits
both; `watch_probe_result` is `async` for the same reason (Textual schedules an
awaitable-returning watcher via `call_next`).

**Widget ids must be valid Python identifiers.** A preset `id = "flac hq"` from
a hand-written `config.toml` raised `BadIdentifier: 'preset-flac hq' is an
invalid id` out of `on_mount`. Rows are keyed by position (`preset-<index>`)
and `_preset_at()` maps a row back to its preset via `_rendered_presets`
(`screens/main.py:348-360`). Sanitising was rejected because it can collide
into `DuplicateIds`; rejecting the preset silently drops the user's work.

## Shutdown, workers, and crash reporting

**`run_test` and `run_async` tear down in opposite orders.** `run_async`
awaits `_process_messages`, whose `finally` calls `self.workers.cancel_all()`
(`app.py:3465`), and only then `await asyncio.shield(app._shutdown())`
dispatches `Unmount` (`app.py:2294-2300`). `run_test` awaits `app._shutdown()`
first (`app.py:2214`). A suite that only drives `run_test` therefore cannot see
a bug that depends on workers being cancelled before unmount — and one lived
here for two review rounds. `tests/test_run_screen_teardown.py` drives
`app.run_async(headless=True, auto_pilot=...)` for exactly this reason.

**`WorkerManager.cancel_all()` cancels every registered worker
unconditionally** (`worker_manager.py:134-137`). There is no opt-out. Since
this app's process-kill escalation runs *inside* the driving task's own
cancellation, a second cancel aborts it and orphans the download. The driver is
therefore a plain `asyncio.Task`, invisible to the manager
(`screens/run.py:118-124` and the module docstring). If a future version gives
workers an opt-out, that decision can be revisited — the two `run_async` tests
are what would have to keep passing.

**`RunScreen.on_unmount` runs before the worker manager sees the screen.**
`MessagePump._get_dispatch_methods` (`message_pump.py:743`) walks the MRO
subclass-first, so `RunScreen.on_unmount` is dispatched ahead of
`Widget._on_unmount`, which is what calls `WorkerManager.cancel_node`.

**Children are pruned before `Unmount` is dispatched.**
`Widget._message_loop_exit` (`widget.py:4514-4525`) posts `Prune` to the
children, gathers their tasks, and only then dispatches `Unmount`. So there is
a window where the driver is alive and the widgets it renders into are gone,
and `query_one` raises `NoMatches` inside it. `_running` is set `False` earlier
still (`message_pump.py:575`, before `_message_loop_exit`), which is why
`apply_event`'s `if not self.is_running: return` guard (`screens/run.py:199`)
covers the whole window.

**`App._handle_exception` needs a live exception context — private API,
undocumented precondition.** `_handle_exception` (`app.py:3263`) →
`_fatal_error` (`app.py:3285`) builds `rich.traceback.Traceback` with no
`trace` argument, and `rich/traceback.py:315-320` raises
`ValueError: Value for 'trace' required if not called in except: block` when
`sys.exc_info()` is empty. `Worker._run` satisfies that incidentally by calling
from inside `except Exception as error:`; an asyncio done callback does not.
The `ValueError` escapes *before* `_fatal_error` reaches
`_close_messages_no_wait()`, so the app never shuts down and asyncio's default
handler paints a raw traceback over the live TUI. Reproduced against a real
`run_async`; a `run_test` version of the same test cannot see it, because the
harness sets `_return_code` and `_exception` either way and re-raises over a
hung app.

`screens/run.py:_on_drive_done` (`:126-156`) re-raises the driver's exception
to establish `sys.exc_info()` before calling `_handle_exception`. This is the
single deliberate use of a private Textual method in the project and the most
likely thing to break on an upgrade. It breaks silently.
