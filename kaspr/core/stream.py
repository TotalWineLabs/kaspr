import faust
import asyncio

from asyncio import CancelledError
from typing import AsyncIterable, List, Optional, Sequence, cast, Tuple
from mode import Seconds, want_seconds
from mode.utils.aiter import aiter
from mode.utils.futures import notify
from faust.types import EventT
from faust.types.streams import (
    T,
    T_co,
)
from faust.types.topics import ChannelT


class KasprStream(faust.Stream):
    """
    Adds customization to faust.Stream.

    Unlike `stream.take()`, which returns values, this implementation
    adds `stream.take_events()` iterator that returns events.
    """

    async def take_events(
        self, max_: int, within: Seconds
    ) -> AsyncIterable[Sequence[Tuple[T_co, EventT]]]:
        """Buffer n events at a time and yield a list of buffered value/events pairs.

        Arguments:
            max_: Max number of messages to receive. When more than this
                number of messages are received within the specified number of
                seconds then we flush the buffer immediately.
            within: Timeout for when we give up waiting for another event,
                and process the events we have.
                Warning: If there's no timeout (i.e. `timeout=None`),
                the agent is likely to stall and block buffered events for an
                unreasonable length of time(!).
        """
        buffer: List[T_co] = []
        events: List[EventT] = []
        buffer_add = buffer.append
        event_add = events.append
        buffer_size = buffer.__len__
        buffer_full = asyncio.Event()
        buffer_consumed = asyncio.Event()
        timeout = want_seconds(within) if within else None
        stream_enable_acks: bool = self.enable_acks

        buffer_consuming: Optional[asyncio.Future] = None

        channel_it = aiter(self.channel)

        # We add this processor to populate the buffer, and the stream
        # is passively consumed in the background (enable_passive below).
        async def add_to_buffer(value: T) -> T:
            try:
                # buffer_consuming is set when consuming buffer after timeout.
                nonlocal buffer_consuming
                if buffer_consuming is not None:
                    try:
                        await buffer_consuming
                    finally:
                        buffer_consuming = None
                event = self.current_event
                if event is None:
                    raise RuntimeError("Take buffer found current_event is None")
                buffer_add(cast(T_co, value))
                event_add(event)
                if buffer_size() >= max_:
                    # signal that the buffer is full and should be emptied.
                    buffer_full.set()
                    # strict wait for buffer to be consumed after buffer full.
                    # If max is 1000, we are not allowed to return 1001 values.
                    buffer_consumed.clear()
                    await self.wait(buffer_consumed)
            except CancelledError:  # pragma: no cover
                raise
            except Exception as exc:
                self.log.exception("Error adding to take buffer: %r", exc)
                await self.crash(exc)
            return value

        # Disable acks to ensure this method acks manually
        # events only after they are consumed by the user
        self.enable_acks = False

        self.add_processor(add_to_buffer)
        self._enable_passive(cast(ChannelT, channel_it))
        try:
            while not self.should_stop:
                # wait until buffer full, or timeout
                await self.wait_for_stopped(buffer_full, timeout=timeout)
                if buffer:
                    # make sure background thread does not add new items to
                    # buffer while we read.
                    buffer_consuming = self.loop.create_future()
                    try:
                        yield list((buffer, events))
                    finally:
                        buffer.clear()
                        for event in events:
                            await self.ack(event)
                        events.clear()
                        # allow writing to buffer again
                        notify(buffer_consuming)
                        buffer_full.clear()
                        buffer_consumed.set()
                else:  # pragma: no cover
                    pass
            else:  # pragma: no cover
                pass

        finally:
            # Restore last behaviour of "enable_acks"
            self.enable_acks = stream_enable_acks
            self._processors.remove(add_to_buffer)
