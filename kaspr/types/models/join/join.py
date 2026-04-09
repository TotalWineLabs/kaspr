"""JoinSpec model for key joins between tables."""

from typing import Optional
from kaspr.types.models.base import SpecComponent
from kaspr.types.models.pycode import PyCode
from kaspr.types.app import KasprAppT
from kaspr.types import KasprJoinT, KasprChannelT, KasprTableT


class JoinSpec(SpecComponent):
    """Specification for a key join between two tables."""

    name: str
    left_table: str
    right_table: str
    extractor: PyCode
    join_type: Optional[str]
    output_channel: Optional[str]

    app: KasprAppT = None

    _join: KasprJoinT = None

    def _prepare_join(self) -> KasprChannelT:
        left_table: KasprTableT = self.app.tables[self.left_table]
        right_table: KasprTableT = self.app.tables[self.right_table]
        extractor = self.extractor.func
        inner = (self.join_type or "inner") == "inner"
        channel = left_table.key_join(
            right_table, extractor=extractor, inner=inner
        )
        channel_name = self.output_channel or f"{self.name}-channel"
        self.app.register_named_channel(channel_name, channel)
        return channel

    @property
    def join(self) -> KasprChannelT:
        if self._join is None:
            self._join = self._prepare_join()
        return self._join
                
    @property
    def label(self) -> str:
        """Return description, used in graphs and logs."""
        return f"{type(self).__name__}: {self.name}"

    @property
    def shortlabel(self) -> str:
        """Return short description."""
        return self.label
