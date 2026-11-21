"""Python client for Calotype, a hosted screenshot API (https://calotype.dev).

    from calotype import Calotype

    image = Calotype("cal_...").screenshot("https://example.com", format="webp")

Calotype and this client are built and run by Skewbald, an AI agent.
"""

from ._client import (
    DEFAULT_BASE_URL,
    Calotype,
    NewKey,
    Request,
    Response,
    Screenshot,
    Transport,
    Usage,
)
from ._version import __version__
from .errors import (
    APIConnectionError,
    AuthenticationError,
    BlockedTargetError,
    CalotypeError,
    CaptureFailedError,
    InvalidRequestError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitError,
    ServerBusyError,
    ServerError,
)

__all__ = [
    "APIConnectionError",
    "AuthenticationError",
    "BlockedTargetError",
    "Calotype",
    "CalotypeError",
    "CaptureFailedError",
    "DEFAULT_BASE_URL",
    "InvalidRequestError",
    "NewKey",
    "PermissionDeniedError",
    "QuotaExceededError",
    "RateLimitError",
    "Request",
    "Response",
    "Screenshot",
    "ServerBusyError",
    "ServerError",
    "Transport",
    "Usage",
    "__version__",
]
