from __future__ import annotations

from typing import Protocol

from agent_kernel.core.models import Work


class Trigger(Protocol):
    id: str

    async def receive(self) -> list[Work]: ...
