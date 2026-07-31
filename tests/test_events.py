import pytest

from yt_dlp_tui.events import (
  DoneEvent,
  LogEvent,
  PostProcessEvent,
  ProgressEvent,
  parse_line,
)

GOOD = 'PROG:{"b":523264,"t":3871021,"s":6477039.98,"e":4,"i":14,"n":87,"title":"Lofi"}'
BARE_NA = 'PROG:{"b":523264,"t":3871021,"s":6477039.98,"e":NA,"i":0,"n":0,"title":"x"}'
PP = 'PP:{"st":"started","pp":"ExtractAudio"}'


def test_parses_progress_line():
  ev = parse_line(GOOD)
  assert isinstance(ev, ProgressEvent)
  assert ev.downloaded == 523264
  assert ev.total == 3871021
  assert ev.index == 14
  assert ev.count == 87
  assert ev.title == "Lofi"


def test_progress_fraction():
  assert parse_line(GOOD).fraction == 523264 / 3871021


def test_fraction_is_zero_when_total_unknown():
  ev = parse_line('PROG:{"b":10,"t":0,"s":0,"e":0,"i":0,"n":0,"title":""}')
  assert ev.fraction == 0.0


def test_bare_na_degrades_to_log_and_does_not_raise():
  ev = parse_line(BARE_NA)
  assert isinstance(ev, LogEvent)


def test_parses_postprocess_line():
  ev = parse_line(PP)
  assert isinstance(ev, PostProcessEvent)
  assert ev.status == "started"
  assert ev.processor == "ExtractAudio"


def test_ordinary_output_becomes_a_log_line():
  ev = parse_line("[download] Destination: video.mp4")
  assert isinstance(ev, LogEvent)
  assert ev.text == "[download] Destination: video.mp4"
  assert ev.is_error is False


def test_stderr_is_flagged_as_error():
  assert parse_line("ERROR: unavailable", is_error=True).is_error is True


def test_blank_lines_are_dropped():
  assert parse_line("") is None
  assert parse_line("   \n") is None


def test_trailing_whitespace_is_tolerated():
  assert isinstance(parse_line(GOOD + "\n"), ProgressEvent)


def test_done_event_reports_success():
  assert DoneEvent(returncode=0).ok is True
  assert DoneEvent(returncode=1).ok is False


@pytest.mark.parametrize(
  "line",
  [
    "PROG:5",
    "PROG:[1,2]",
    'PROG:"x"',
    "PROG:true",
    "PROG:null",
  ],
)
def test_non_object_progress_payload_degrades_to_log_and_does_not_raise(line):
  ev = parse_line(line)
  assert isinstance(ev, LogEvent)


@pytest.mark.parametrize(
  "line",
  [
    "PP:5",
    "PP:[1,2]",
    'PP:"x"',
    "PP:true",
    "PP:null",
  ],
)
def test_non_object_postprocess_payload_degrades_to_log_and_does_not_raise(line):
  ev = parse_line(line)
  assert isinstance(ev, LogEvent)


PATHOLOGICAL_LINES = [
  BARE_NA,
  "PROG:{}",
  'PROG:{"b":',
  "PROG:5",
  "PROG:[1,2]",
  'PROG:"x"',
  "PROG:true",
  "PROG:null",
  "PP:5",
  "PP:[1,2]",
  'PP:"x"',
  "PP:true",
  "PP:null",
]


def test_parse_line_never_raises_on_pathological_input():
  for line in PATHOLOGICAL_LINES:
    parse_line(line)  # must not raise
