"""sources: the one registry of recording sources.

Every place that used to branch on a recording's source (listing, id prefix →
source, list label, audio fetch, phone prefetch, the runner's credentials) asks
this module instead. A recording is a dict with at least `id`, `source`,
`filename`, `start_time` (epoch ms) and `duration` (ms); its id carries the
source as a prefix (`wispr:<uuid>`), except Plaud's opaque ids, which have none.

    name      prefix     listed by                       audio
    plaud     (none)     Plaud API (needs the token)     Plaud download
    wispr     wispr:     Wispr Flow's sqlite             none: the transcript is local
    recorder  recorder:  adb, MediaStore /sdcard/Recordings   adb pull
    call      call:      adb, MediaStore Download + call log   adb pull
    local     local:     not listable (content hash)     the file itself

An id with a prefix that isn't registered is an error (UnknownSource), never
"probably Plaud": a Plaud guess asked for a Plaud token and hit the Plaud API
for a recording Plaud never had.

Adding a source:
  1. write its module with `list_recordings(config, days)` returning recording
     dicts (id `<prefix><native id>`, `source` = the name) and, if its audio lives
     elsewhere, `pull(recording, config, dest_dir) -> Path`;
  2. add one `Source(...)` to SOURCES below (and its prefix to the table above);
  3. if it has config, give the Source its config_key / config_example /
     config_help: `init` and `plaud-transcribe --help` pick them up.
Nothing else branches on the source name (Wispr's ready-made transcript is
still read by Wispr-specific code in plaud_transcribe).

Recipes:
  which source is an id?   python3 -c 'import sources; print(sources.source_for_id("call:20261126-090907").name)'
  list one source only     sources.list_recordings(host, config, 30, only={"call"})
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

import google_recorder
import phone_calls
import wispr_flow


class UnknownSource(ValueError):
    """A recording id whose prefix no registered source owns."""


@dataclass(frozen=True)
class Source:
    name: str                     # recording["source"]
    prefix: Optional[str]         # "wispr:"; None for Plaud's opaque ids
    label: str                    # the list column (<= 5 chars)
    title: str                    # in notes: "<title> not readable"
    origin: str                   # header line of a run ("Google Recorder")
    lister: Optional[Callable] = None   # (host, config, days, token) -> [recording]
    pull: Optional[Callable] = None     # (recording, config, dest_dir) -> Path
    on_phone: bool = False        # audio comes off the phone: prefetch copies it first
    has_transcript: bool = False  # transcript already exists: no ElevenLabs, no audio
    env: tuple = ()               # credentials a job with this source needs
    env_alt: dict = field(default_factory=dict)   # env var -> config key that also satisfies it
    id_key: str = ""              # front matter key for the native id
    path_of: Optional[Callable] = None  # recording -> source file path (front matter)
    unlisted_hint: str = ""       # why an id of an unlistable source can't be resolved
    context: Optional[Callable] = None  # (recording, tz) -> text for the summarizer, or ""
    config_key: str = ""          # its section in config.json
    config_example: Optional[dict] = None   # written by `init`
    config_help: str = ""         # appended to `plaud-transcribe --help`

    def owns(self, rec_id):
        return bool(self.prefix) and rec_id.startswith(self.prefix)


ELEVENLABS = "ELEVENLABS_API_KEY"
PLAUD_VAR = "PLAUD_BEARER_TOKEN"

SOURCES = (
    Source("plaud", "plaud:", "Plaud", "Plaud", "Plaud",
           lister=lambda host, config, days, token: host.list_recordings(token, config, days),
           env=(ELEVENLABS, PLAUD_VAR), env_alt={PLAUD_VAR: "token"}, id_key="plaud_id"),
    Source("wispr", "wispr:", "Wispr", "Wispr", "Wispr Flow",
           lister=lambda host, config, days, token: wispr_flow.list_meetings(config, days),
           has_transcript=True, id_key="wispr_id"),
    Source("recorder", "recorder:", "Phone", "Recorder", "Google Recorder",
           lister=lambda host, config, days, token: google_recorder.list_recordings(config, days),
           pull=lambda rec, config, dest: google_recorder.pull(rec, config, dest),
           on_phone=True, env=(ELEVENLABS,), id_key="recorder_id",
           path_of=lambda rec: rec["_recorder"]["path"],
           config_key="recorder", config_example={"enabled": False, "adb": "adb"},
           config_help=google_recorder.CONFIG_HELP),
    Source("call", "call:", "Call", "Calls", "Phone call",
           lister=lambda host, config, days, token: phone_calls.list_recordings(config, days),
           pull=lambda rec, config, dest: phone_calls.pull(rec, config, dest),
           on_phone=True, env=(ELEVENLABS,), id_key="call_id",
           path_of=lambda rec: rec["_call"]["path"],
           context=lambda rec, tz: phone_calls.summary_context(rec, tz),
           config_key="calls", config_help=phone_calls.CONFIG_HELP,
           config_example={"enabled": False, "dir": phone_calls.DEFAULT_DIR,
                           "match_slack_s": phone_calls.DEFAULT_SLACK_S}),
    Source("local", "local:", "Local", "Local", "Local file",
           env=(ELEVENLABS,), id_key="local_id", path_of=lambda rec: rec["_local_path"],
           unlisted_hint="local files are not listed; run `plaud-transcribe file <audio>` instead"),
)
BY_NAME = {s.name: s for s in SOURCES}
PLAUD = BY_NAME["plaud"]


def get(name):
    """The source called `name`; a recording without a source is Plaud's."""
    try:
        return BY_NAME[name or "plaud"]
    except KeyError:
        raise UnknownSource(f"unknown recording source {name!r} "
                            f"(known: {', '.join(BY_NAME)})") from None


def source_for_id(rec_id):
    """The source of a recording id. Ids without a colon are Plaud's; any other
    prefix must be registered."""
    for s in SOURCES:
        if s.owns(rec_id):
            return s
    if ":" not in rec_id:
        return PLAUD
    raise UnknownSource(f"unknown recording source {rec_id.split(':', 1)[0] + ':'!r} in "
                        f"{rec_id!r} (known: {', '.join(s.prefix for s in SOURCES if s.prefix)} "
                        "or a bare Plaud id)")


def of(recording):
    return get(recording.get("source"))


def label(recording):
    return of(recording).label


def needs_env(ids):
    """[(env var, config key that may stand in or None)] the ids' sources need, in
    registry order, without repeats. Raises UnknownSource for an unknown prefix."""
    out = {}
    for s in {source_for_id(i).name: source_for_id(i) for i in ids}.values():
        for var in s.env:
            out.setdefault(var, s.env_alt.get(var))
    order = [v for s in SOURCES for v in s.env]
    return sorted(out.items(), key=lambda kv: order.index(kv[0]))


def summary_context(recording, tz):
    """What the summarizer should be told about this recording's source, or ''."""
    src = of(recording)
    return src.context(recording, tz) if src.context else ""


def example_config():
    """{config key: starter section} for `init`."""
    return {s.config_key: dict(s.config_example) for s in SOURCES if s.config_key and s.config_example}


def config_help():
    """Every source's config help, for the `--help` epilog."""
    return "\n".join(s.config_help for s in SOURCES if s.config_help)


def all_env():
    """Every credential any source may need, in registry order."""
    seen = []
    for s in SOURCES:
        seen += [v for v in s.env if v not in seen]
    return tuple(seen)


def list_recordings(host, config, days, token=None, *, only=None, notes=None):
    """Recordings from every listable source (or the names in `only`), newest first.

    `host` is the plaud_transcribe module (or a stand-in): Plaud is listed through
    `host.list_recordings(token, config, days)`. With `notes=None` a failing source
    stops the caller, as `list`/`process` always did when Plaud failed. With a list,
    a failure costs only that source's rows and appends a note: the plan screen
    and the runner keep going on a train."""
    recs = []
    for s in SOURCES:
        if s.lister is None or (only is not None and s.name not in only):
            continue
        if notes is None:
            recs += s.lister(host, config, days, token)
            continue
        try:
            recs += s.lister(host, config, days, token)
        except (Exception, SystemExit) as e:   # plaud_get exits on a 401
            notes.append(f"Plaud not reachable ({type(e).__name__}: {e}); showing local recordings only"
                         if s is PLAUD else f"{s.title} not readable ({type(e).__name__}: {e})")
    for r in recs:
        r.setdefault("source", "plaud")
    recs.sort(key=lambda r: r.get("start_time", 0), reverse=True)
    return recs
