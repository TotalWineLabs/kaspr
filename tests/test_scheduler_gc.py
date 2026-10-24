"""The scheduler's dispatchers and janitors must not force a garbage collection on every pass.

A full collection walks every object the interpreter tracks: 17 ms in an app with nothing but Kaspr
loaded, 33 ms in one with 171,000 objects. Each dispatcher passes over its Timetable partition every
0.25 s and each janitor every 3 s, so collecting on every pass took eleven collections a second with
three partitions, and kept an app that had nothing to schedule busy half of the time, all of it in
its event loop. The interpreter's own collector still frees what reference cycles there are.
"""

import asyncio
import gc
from types import SimpleNamespace

from mode.utils.locks import Event

from kaspr.scheduler.dispatcher import Dispatcher
from kaspr.scheduler.janitor import Janitor
from kaspr.scheduler.utils import current_timekey
from kaspr.types import TTLocation

#: Passes each loop makes before the test ends it.
PASSES = 5
#: How far back both loops start scanning.
BACKLOG_SECONDS = 30
#: When the one scheduled message is due, before now.
DUE_SECONDS_AGO = 10


class EndOfTest(Exception):
    """Raised from a loop's sleep once it has made its passes."""


class AnyMonitor:
    """A monitor that accepts every call."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Channel:
    def __init__(self):
        self.items = []

    async def put(self, item):
        self.items.append(item)


class Timetable:
    """One message scheduled at ``time_key``; every other time key is empty."""

    def __init__(self, time_key):
        self.rows = {str(time_key): 1, f"{time_key}-0": {"k": "key", "v": "value"}}

    def get_for_partition(self, key, partition):
        return self.rows.get(key)


class Checkpoints:
    def __init__(self, locations):
        self.locations = dict(locations)
        self.dispatcher_checkpointed = Event()
        self.dispatcher_checkpointed.set()

    def get(self, pt, default=None):
        return self.locations.get(pt, default)

    def update(self, pt, location):
        self.locations[pt] = location


def make_app(now):
    """An app whose Timetable partition 0 holds one due message, and where its loops start."""
    topics_created = Event()
    topics_created.set()
    started = TTLocation(0, now - BACKLOG_SECONDS)
    checkpoints = Checkpoints({})
    app = SimpleNamespace(
        conf=SimpleNamespace(
            scheduler_dispatcher_default_checkpoint_lookback_days=0,
            scheduler_janitor_clean_interval_seconds=3.0,
            scheduler_janitor_highwater_offset_seconds=0,
        ),
        channel=lambda **kwargs: Channel(),
        scheduler=SimpleNamespace(
            checkpoints=checkpoints,
            timetable=Timetable(now - DUE_SECONDS_AGO),
            topics_created=topics_created,
        ),
    )
    return app, started


def run_passes(make_service, loop_name, monkeypatch):
    """Run a scheduler loop for PASSES passes; return the service, the full collections it
    forced and the intervals it slept."""
    collections = []
    monkeypatch.setattr(gc, "collect", lambda *args, **kwargs: collections.append(args) or 0)
    sleeps = []

    async def sleep(seconds, *args, **kwargs):
        sleeps.append(seconds)
        if len(sleeps) == PASSES:
            raise EndOfTest()
        await asyncio.sleep(0)

    async def run():
        service = make_service()
        service.sleep = sleep
        service.resume()
        try:
            await getattr(type(service), loop_name).fun(service)
        except EndOfTest:
            pass
        return service

    service = asyncio.run(run())
    return service, collections, sleeps


def test_a_dispatcher_passes_over_its_partition_without_collecting_garbage(monkeypatch):
    now = current_timekey()

    def make_dispatcher():
        app, started = make_app(now)
        dispatcher = Dispatcher(app, partition=0, monitor=AnyMonitor())
        app.scheduler.checkpoints.locations[dispatcher.pt] = started
        return dispatcher

    dispatcher, collections, sleeps = run_passes(make_dispatcher, "_dispatch", monkeypatch)

    assert sleeps == [0.25] * PASSES
    # It still found the due message, once, and scanned up to the present.
    assert [delivery.message for delivery in dispatcher.pending_deliveries.items] == [
        {"k": "key", "v": "value"}
    ]
    assert dispatcher.last_location.time_key >= now - 1
    assert collections == []


def test_a_janitor_cleans_without_collecting_garbage(monkeypatch):
    now = current_timekey()

    def make_janitor():
        app, started = make_app(now)
        janitor = Janitor(app, partition=0, monitor=AnyMonitor())
        app.scheduler.checkpoints.locations[janitor.pt] = started
        # The dispatcher has delivered everything due before now.
        app.scheduler.checkpoints.locations[janitor.dpt] = TTLocation(0, now)
        return janitor

    janitor, collections, sleeps = run_passes(make_janitor, "_clean", monkeypatch)

    assert sleeps == [3.0] * PASSES
    # It still removed the delivered message and its time key.
    due = now - DUE_SECONDS_AGO
    assert janitor.pending_removals.items == [TTLocation(0, due, 0), TTLocation(0, due)]
    assert collections == []
