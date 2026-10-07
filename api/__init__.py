"""API package for Zellinho Chat."""

from api.schemas import (
    MessageRequest,
    MessageResponse,
    ConversationStateResponse,
    ErrorResponse,
    HealthCheckResponse
)

__all__ = [
    "MessageRequest",
    "MessageResponse",
    "ConversationStateResponse",
    "ErrorResponse",
    "HealthCheckResponse"
]
