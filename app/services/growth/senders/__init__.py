"""Customer-facing channel senders for the growth platform.

One implementation of ``ChannelSender`` per channel, and one registry the
dispatcher asks for a channel by name. The dispatcher looks for
``container.growth_senders`` and no-ops when it is absent — that is the seam
Stage 1 left, and this package is what fills it.

The registry is deliberately tiny. Its only real job is to make "which channels
can this platform send on" a single readable list, so that adding SMS later is
one module and one line here rather than a search for every place a channel name
was spelled out.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ValidationError


class SenderRegistry:
    """Maps a ``growth_messages.channel`` value to the sender that can deliver it."""

    def __init__(self, container: Any) -> None:
        self._container = container
        self._senders: dict[str, Any] = {}

    def for_channel(self, channel: str) -> Any:
        """The sender for one channel.

        Raises rather than returning None: the dispatcher's per-message
        try/except turns that into one failed row and a loud log line, which is
        the correct outcome for a message queued on a channel nothing can send.
        Returning None would instead produce an ``AttributeError`` two frames
        later, attributed to the wrong thing.
        """
        channel = (channel or "").strip().lower()
        if channel in self._senders:
            return self._senders[channel]

        if channel == "email":
            from app.services.growth.senders.email_sender import GrowthEmailSender

            sender: Any = GrowthEmailSender(self._container)
        elif channel == "whatsapp":
            from app.services.growth.senders.whatsapp_sender import GrowthWhatsAppSender

            sender = GrowthWhatsAppSender(self._container)
        else:
            raise ValidationError(f"no sender for channel {channel!r}")

        self._senders[channel] = sender
        return sender
