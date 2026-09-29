"""The voice client the scribe connects with: discord.py's own, plus a record of who is in the call.

Discord often sends no SPEAKING (op 5) for people already in the call when the bot connects
(DESIGN §4.1). What it does send, while the voice websocket handshakes, is CLIENTS_CONNECT (op 11:
the ``user_ids`` with media in the call), then CLIENT_CONNECT (op 12) / CLIENT_DISCONNECT (op 13)
as people come and go. Hermes' receiver hooks the websocket only after ``connect()`` returns, too
late for op 11. ``channel.connect(cls=...)`` is discord.py's public way to pick the client class;
this one passes a websocket hook to its connection state from the start and keeps those opcodes,
so the receiver learns who may own an unannounced SSRC (they carry user ids, never SSRCs).
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable, Optional

VOICE_OPS = frozenset({11, 12, 13})
BACKLOG = 64


def scribe_voice_client_class(base: type, state_cls: type) -> type:
    """``base`` is ``discord.VoiceClient``; ``state_cls`` is ``discord.voice_state.VoiceConnectionState``."""

    class ScribeVoiceClient(base):  # type: ignore[misc, valid-type]
        def create_connection_state(self) -> Any:
            self.voice_ops: deque[tuple[int, dict[str, Any]]] = deque(maxlen=BACKLOG)
            self.voice_op_listener: Optional[Callable[[int, dict[str, Any]], None]] = None
            self.voice_membership_complete = False
            self._membership_ws: Any = None
            self._membership_gap = False
            return state_cls(self, hook=self._record_voice_op)

        async def _record_voice_op(self, ws: Any, msg: Any) -> None:
            if not isinstance(msg, dict) or msg.get("op") not in VOICE_OPS or not isinstance(msg.get("d"), dict):
                return
            op, data = int(msg["op"]), dict(msg["d"])
            if self._membership_ws is not None and self._membership_ws is not ws:
                self._membership_gap = True  # a reconnect may have lost membership events
            self._membership_ws = ws
            if self.voice_op_listener is None and len(self.voice_ops) == BACKLOG:
                self._membership_gap = True
            if op == 11 and not self.voice_ops and not self._membership_gap:
                self.voice_membership_complete = True
            if self._membership_gap:
                self.voice_membership_complete = False
            self.voice_ops.append((op, data))
            if self.voice_op_listener is not None:
                self.voice_op_listener(op, data)

    return ScribeVoiceClient
