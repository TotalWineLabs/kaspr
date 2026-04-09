import abc
import typing
from faust.types import TableT
from faust.types.tuples import MessageSentCallback
from typing import Any
from mode import SignalT

if typing.TYPE_CHECKING:
    from .app import KasprAppT as _KasprAppT
else:

    class _KasprAppT:
        ...  # noqa


class KasprTableT(TableT):
    """Type for custom table."""

    app: _KasprAppT

    on_table_recovery_completed: SignalT

    @abc.abstractmethod
    def update_for_partition(self, 
                             *args: Any, 
                             partition: int, 
                             callback: MessageSentCallback = None,  # type: ignore
                             **kwargs: Any) -> None:
        """Update a specific partition of table"""
        ...

    @abc.abstractmethod
    def get_for_partition(self, key, partition: int):
        """Get key in specific partition of table"""
        ...

    @abc.abstractmethod
    def del_for_partition(self, 
                          key, 
                          partition: int, 
                          callback: MessageSentCallback = None): # type: ignore
        """Delete key in specific partition of table"""
        ...

    @abc.abstractmethod
    def key_join(self, 
                 other_table: 'KasprTableT', 
                 extractor: Any, 
                 inner: bool = True) -> Any:
        """Perform key join with another table, return channel of joined values."""
        ...

class KasprGlobalTableT(KasprTableT):
    ...