import asyncio
import importlib
import logging
import os
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest
from faust.stores.memory import Store as MemoryStore
from faust.stores.rocksdb import Store as RocksDBStore
from faust.types import TP

from kaspr.core.app import KasprApp
from kaspr.types import settings
from kaspr.utils import stale_tables
from kaspr.utils.stale_tables import StaleDB, disk_usage, find_stale_table_dbs, purge_stale_table_dbs

MiB = 2**20


class FakeRocksDBStore(RocksDBStore):
    def __init__(self, path, basename, open_partitions=()):
        self._path = path
        self._basename = basename
        self._dbs = {partition: object() for partition in open_partitions}

    @property
    def path(self):
        return self._path

    @property
    def basename(self):
        return Path(self._basename)


def make_table(name, store, *, is_global=False):
    return SimpleNamespace(
        name=name,
        is_global=is_global,
        changelog_topic_name=f"app-{name}-changelog",
        data=store,
    )


class FakeApp(SimpleNamespace):
    def __init__(self, tabledir, tables, actives=(), standbys=()):
        super().__init__(
            conf=SimpleNamespace(tabledir=tabledir),
            tables={table.name: table for table in tables},
            assignor=SimpleNamespace(
                assigned_actives=lambda: set(self.actives),
                assigned_standbys=lambda: set(self.standbys),
            ),
        )
        self.actives = set(actives)
        self.standbys = set(standbys)


def make_db(tabledir, name, *, size=1 * MiB, age=0):
    """Create a fake database directory; `age` is how many seconds ago it was last written."""
    path = tabledir / name
    path.mkdir()
    (path / "000001.sst").write_bytes(b"x" * size)
    (path / "CURRENT").write_text("MANIFEST-000002\n")
    mtime = os.stat(path).st_mtime - age
    for entry in (path / "000001.sst", path / "CURRENT", path):
        os.utime(entry, (mtime, mtime))
    return path


def names(dbs):
    return [db.path.name for db in dbs]


# --- disk usage ---------------------------------------------------------------


def test_disk_usage_is_used_over_used_plus_available(monkeypatch):
    usage = namedtuple("usage", "total used free")
    # ext4 reserves blocks for root: free + used < total, `df` ignores them
    monkeypatch.setattr(stale_tables.shutil, "disk_usage", lambda path: usage(100, 76, 19))

    assert disk_usage("/tables") == pytest.approx(0.8)


# --- finding stale databases ----------------------------------------------------------


def test_find_stale_table_dbs_ignores_assigned_standby_and_open_partitions(tmp_path):
    for partition in range(6):
        make_db(tmp_path, f"orders-{partition}.db", age=100 - partition)
    store = FakeRocksDBStore(tmp_path, "orders", open_partitions=[4])
    table = make_table("orders", store)
    app = FakeApp(
        tmp_path,
        [table],
        actives={TP(table.changelog_topic_name, 1), TP(table.changelog_topic_name, 2)},
        standbys={TP(table.changelog_topic_name, 3)},
    )

    stale = find_stale_table_dbs(app)

    assert names(stale) == ["orders-0.db", "orders-5.db"]
    assert [db.partition for db in stale] == [0, 5]
    assert all(db.table is table and db.size > MiB for db in stale)


def test_find_stale_table_dbs_orders_oldest_first(tmp_path):
    make_db(tmp_path, "orders-1.db", age=10)
    make_db(tmp_path, "orders-2.db", age=500)
    make_db(tmp_path, "orders-3.db", age=50)
    app = FakeApp(tmp_path, [make_table("orders", FakeRocksDBStore(tmp_path, "orders"))])

    assert names(find_stale_table_dbs(app)) == ["orders-2.db", "orders-3.db", "orders-1.db"]


def test_find_stale_table_dbs_assignment_of_other_topics_does_not_count(tmp_path):
    make_db(tmp_path, "orders-1.db")
    table = make_table("orders", FakeRocksDBStore(tmp_path, "orders"))
    # same partition number, but of a source topic
    app = FakeApp(tmp_path, [table], actives={TP("orders-source", 1)})

    assert names(find_stale_table_dbs(app)) == ["orders-1.db"]


def test_find_stale_table_dbs_tables_with_similar_names(tmp_path):
    make_db(tmp_path, "item-1.db")
    make_db(tmp_path, "item-store-1.db")
    item = make_table("item", FakeRocksDBStore(tmp_path, "item"))
    item_store = make_table("item-store", FakeRocksDBStore(tmp_path, "item-store"))
    app = FakeApp(tmp_path, [item, item_store], actives={TP(item.changelog_topic_name, 1)})

    # item-1 belongs to the first table and is assigned, item-store-1 is not
    assert names(find_stale_table_dbs(app)) == ["item-store-1.db"]


def test_find_stale_table_dbs_ignores_unrelated_entries(tmp_path):
    make_db(tmp_path, "orders-1.db")
    (tmp_path / "orders-backups").mkdir()
    (tmp_path / "lost+found").mkdir()
    (tmp_path / "orders-2.db.tmp").mkdir()
    (tmp_path / "orders-3.db").write_text("a file, not a database")
    make_db(tmp_path, "unknown-1.db")
    app = FakeApp(tmp_path, [make_table("orders", FakeRocksDBStore(tmp_path, "orders"))])

    assert names(find_stale_table_dbs(app)) == ["orders-1.db"]


def test_find_stale_table_dbs_skips_global_and_non_rocksdb_tables(tmp_path):
    make_db(tmp_path, "rules-0.db")
    make_db(tmp_path, "rules-1.db")
    make_db(tmp_path, "cache-3.db")
    global_table = make_table("rules", FakeRocksDBStore(tmp_path, "rules"), is_global=True)
    memory_table = make_table("cache", object.__new__(MemoryStore))
    app = FakeApp(tmp_path, [global_table, memory_table])

    assert find_stale_table_dbs(app) == []


# --- purging ---------------------------------------------------------------------------


class FakeDisk:
    """Disk whose usage drops by `step` every time a database is deleted."""

    def __init__(self, usage, step):
        self.current = usage
        self.step = step
        self.deleted = []

    def usage(self, path):
        return self.current

    def rmtree(self, path):
        self.deleted.append(Path(path).name)
        self.current -= self.step


@pytest.fixture
def stale_app(tmp_path):
    for partition, age in [(1, 400), (2, 300), (3, 200), (4, 100)]:
        make_db(tmp_path, f"orders-{partition}.db", age=age)
    table = make_table("orders", FakeRocksDBStore(tmp_path, "orders"))
    return FakeApp(tmp_path, [table])


def test_purge_does_nothing_below_threshold(stale_app):
    disk = FakeDisk(0.79, 0.05)

    purged = asyncio.run(purge_stale_table_dbs(stale_app, 0.8, usage=disk.usage, rmtree=disk.rmtree))

    assert purged == [] and disk.deleted == []


def test_purge_deletes_oldest_first_until_below_threshold(stale_app):
    disk = FakeDisk(0.90, 0.05)

    purged = asyncio.run(purge_stale_table_dbs(stale_app, 0.8, usage=disk.usage, rmtree=disk.rmtree))

    # 0.90 -> 0.85 -> 0.80 (not above the threshold anymore)
    assert disk.deleted == ["orders-1.db", "orders-2.db"]
    assert names(purged) == disk.deleted


def test_purge_deletes_every_stale_db_if_usage_stays_high(stale_app, caplog):
    disk = FakeDisk(0.95, 0.01)

    with caplog.at_level(logging.WARNING):
        purged = asyncio.run(purge_stale_table_dbs(stale_app, 0.8, usage=disk.usage, rmtree=disk.rmtree))

    assert len(purged) == 4
    assert "still above" in caplog.text


def test_purge_never_deletes_assigned_partitions(stale_app):
    table = stale_app.tables["orders"]
    stale_app.actives = {TP(table.changelog_topic_name, 1)}
    stale_app.standbys = {TP(table.changelog_topic_name, 4)}
    disk = FakeDisk(0.99, 0.01)

    asyncio.run(purge_stale_table_dbs(stale_app, 0.8, usage=disk.usage, rmtree=disk.rmtree))

    assert disk.deleted == ["orders-2.db", "orders-3.db"]


def test_purge_skips_partition_assigned_while_purging(stale_app):
    table = stale_app.tables["orders"]
    disk = FakeDisk(0.99, 0.01)
    rmtree = disk.rmtree

    def rmtree_then_rebalance(path):
        rmtree(path)
        # a rebalance assigns orders-3 right after orders-1 was deleted
        stale_app.actives = {TP(table.changelog_topic_name, 3)}

    asyncio.run(purge_stale_table_dbs(stale_app, 0.8, usage=disk.usage, rmtree=rmtree_then_rebalance))

    assert disk.deleted == ["orders-1.db", "orders-2.db", "orders-4.db"]


def test_purge_continues_after_a_deletion_fails(stale_app, caplog):
    disk = FakeDisk(0.99, 0.01)

    def rmtree(path):
        if Path(path).name == "orders-1.db":
            raise PermissionError("read-only file system")
        disk.rmtree(path)

    with caplog.at_level(logging.WARNING):
        purged = asyncio.run(purge_stale_table_dbs(stale_app, 0.8, usage=disk.usage, rmtree=rmtree))

    assert names(purged) == ["orders-2.db", "orders-3.db", "orders-4.db"]
    assert "Could not delete stale table database" in caplog.text


def test_purge_removes_directories_from_disk(stale_app, tmp_path):
    usage = iter([0.9, 0.9, 0.7, 0.7])  # check, loop, loop (after 1st delete), summary

    purged = asyncio.run(purge_stale_table_dbs(stale_app, 0.8, usage=lambda path: next(usage)))

    assert names(purged) == ["orders-1.db"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["orders-2.db", "orders-3.db", "orders-4.db"]


# --- hook ------------------------------------------------------------------------------


class FakeKasprApp(SimpleNamespace):
    def __init__(self, threshold=0.8):
        super().__init__(
            conf=SimpleNamespace(table_stale_purge_disk_usage_threshold=threshold),
            _stale_tables_purge_scheduled=False,
            scheduled=[],
            log=SimpleNamespace(exception=lambda *args, **kwargs: self.logged.append(args)),
            logged=[],
        )
        self.add_future = self.scheduled.append

    async def _purge_stale_tables(self, threshold):
        ...


def assign(app, assigned):
    asyncio.run(KasprApp._purge_stale_tables_once(app, app, assigned))


def test_purge_is_scheduled_once_on_the_first_assignment():
    app = FakeKasprApp()
    partitions = {TP("topic", 0)}

    assign(app, partitions)
    assign(app, partitions)

    assert len(app.scheduled) == 1
    app.scheduled[0].close()  # never awaited


def test_purge_waits_for_a_non_empty_assignment():
    app = FakeKasprApp()

    assign(app, set())
    assert app.scheduled == []

    assign(app, {TP("topic", 0)})
    assert len(app.scheduled) == 1
    app.scheduled[0].close()


@pytest.mark.parametrize("threshold", [0, 0.0, -1])
def test_purge_can_be_disabled(threshold):
    app = FakeKasprApp(threshold=threshold)

    assign(app, {TP("topic", 0)})

    assert app.scheduled == []


def test_purge_failure_is_logged_and_does_not_crash(monkeypatch):
    app = FakeKasprApp()

    async def fail(app, threshold):
        raise RuntimeError("boom")

    monkeypatch.setattr("kaspr.core.app.purge_stale_table_dbs", fail)

    asyncio.run(KasprApp._purge_stale_tables(app, 0.8))

    assert len(app.logged) == 1


def test_real_app_purges_on_the_first_non_empty_assignment(monkeypatch, tmp_path):
    purged = []

    async def purge(app, threshold):
        purged.append(threshold)

    monkeypatch.setattr("kaspr.core.app.purge_stale_table_dbs", purge)
    app = KasprApp("purge-test", store="memory://", datadir=str(tmp_path))

    async def assignments():
        await app.on_partitions_assigned.send(set())
        await app.on_partitions_assigned.send({TP("topic", 0)})
        await app.on_partitions_assigned.send({TP("topic", 1)})
        await asyncio.sleep(0)  # let the scheduled purge run

    asyncio.run(assignments())

    assert purged == [0.8]


# --- setting ------------------------------------------------------------------------


@pytest.fixture
def reload_settings(monkeypatch):
    for prefix in settings.PREFICES:
        monkeypatch.delenv(prefix + "TABLE_STALE_PURGE_DISK_USAGE_THRESHOLD", raising=False)

    def reload(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(settings)

    yield reload
    monkeypatch.undo()
    importlib.reload(settings)


def test_threshold_defaults_to_80_percent(reload_settings):
    assert reload_settings().TABLE_STALE_PURGE_DISK_USAGE_THRESHOLD == 0.8


@pytest.mark.parametrize("prefix", ["K_", "KASPR_"])
def test_threshold_is_read_from_the_environment(reload_settings, prefix):
    loaded = reload_settings(**{prefix + "TABLE_STALE_PURGE_DISK_USAGE_THRESHOLD": "0.9"})

    assert loaded.TABLE_STALE_PURGE_DISK_USAGE_THRESHOLD == 0.9
