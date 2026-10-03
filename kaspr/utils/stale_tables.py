import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, Set, Tuple

from faust.stores.rocksdb import Store as RocksDBStore

from kaspr.utils.logging import get_logger

logger = get_logger(__name__)

MiB = 2**20


@dataclass(frozen=True)
class StaleDB:
    """The on-disk RocksDB database of a table partition this worker no longer serves."""

    table: Any
    partition: int
    path: Path
    size: int
    mtime: float


def disk_usage(path: Any) -> float:
    """Return the used fraction (0.0 - 1.0) of the file system of `path`, as reported by `df`."""
    usage = shutil.disk_usage(path)
    return usage.used / (usage.used + usage.free)


def _needed_partitions(app: Any, table: Any) -> Optional[Set[int]]:
    """Return the partitions of `table` that are, or may soon be, served by this worker.

    None means all of them (global tables).
    """
    if table.is_global:
        return None
    topic = table.changelog_topic_name
    assigned = set(app.assignor.assigned_actives()) | set(app.assignor.assigned_standbys())
    needed = {tp.partition for tp in assigned if tp.topic == topic}
    needed.update(getattr(table.data, "_dbs", ()))  # databases that are open right now
    return needed


def _dir_stats(path: str) -> Tuple[int, float]:
    """Return the size and last modification time of a database directory."""
    size, mtime = 0, os.stat(path).st_mtime
    with os.scandir(path) as entries:
        for entry in entries:
            stat = entry.stat(follow_symlinks=False)
            size += stat.st_size
            mtime = max(mtime, stat.st_mtime)
    return size, mtime


def find_stale_table_dbs(app: Any) -> List[StaleDB]:
    """Return the on-disk databases of table partitions not served by this worker, oldest first."""
    stale = []
    for table in app.tables.values():
        store = table.data
        if not isinstance(store, RocksDBStore):
            continue
        needed = _needed_partitions(app, table)
        if needed is None:
            continue
        pattern = re.compile(rf"^{re.escape(str(store.basename))}-(\d+)\.db$")
        try:
            entries = list(os.scandir(store.path))
        except OSError:
            continue
        for entry in entries:
            match = pattern.match(entry.name)
            if not match or not entry.is_dir(follow_symlinks=False):
                continue
            partition = int(match.group(1))
            if partition in needed:
                continue
            try:
                size, mtime = _dir_stats(entry.path)
            except OSError:
                continue
            stale.append(StaleDB(table, partition, Path(entry.path), size, mtime))
    return sorted(stale, key=lambda db: db.mtime)


async def purge_stale_table_dbs(
    app: Any,
    threshold: float,
    *,
    usage: Callable[[Any], float] = disk_usage,
    rmtree: Callable[[Path], None] = shutil.rmtree,
) -> List[StaleDB]:
    """Delete stale table databases, oldest first, until disk usage is at most `threshold`.

    The state of a stale partition is rebuilt from its changelog topic if
    the partition is assigned to this worker again.
    """
    tabledir = app.conf.tabledir
    current = usage(tabledir)
    if current <= threshold:
        return []
    candidates = find_stale_table_dbs(app)
    logger.warning(
        "Disk usage of %s is %.0f%% (threshold %.0f%%): deleting stale table databases, "
        "oldest first (%d found, %.0f MiB)",
        tabledir,
        current * 100,
        threshold * 100,
        len(candidates),
        sum(db.size for db in candidates) / MiB,
    )
    purged = []
    for db in candidates:
        if usage(tabledir) <= threshold:
            break
        # a rebalance may have assigned the partition since the candidates were listed
        needed = _needed_partitions(app, db.table)
        if needed is None or db.partition in needed:
            continue
        try:
            # Inline on purpose: nothing can assign the partition between the check above and the delete.
            rmtree(db.path)
        except OSError as exc:
            logger.warning("Could not delete stale table database %s: %r", db.path, exc)
            continue
        purged.append(db)
        logger.info(
            "Deleted stale table database %s (%.0f MiB, last modified %s)",
            db.path.name,
            db.size / MiB,
            datetime.fromtimestamp(db.mtime, timezone.utc).isoformat(timespec="seconds"),
        )
        await asyncio.sleep(0)
    remaining = usage(tabledir)
    logger.info(
        "Deleted %d stale table database(s), freed %.0f MiB; disk usage of %s is now %.0f%%",
        len(purged),
        sum(db.size for db in purged) / MiB,
        tabledir,
        remaining * 100,
    )
    if remaining > threshold:
        logger.warning(
            "Disk usage of %s is still above %.0f%% and there are no more stale table databases to delete",
            tabledir,
            threshold * 100,
        )
    return purged
