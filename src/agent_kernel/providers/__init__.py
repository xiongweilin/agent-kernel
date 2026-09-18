"""Provider adapters shipped with the agent kernel."""

from .fake import EchoProvider, FailingProvider

__all__ = ["EchoProvider", "FailingProvider"]
