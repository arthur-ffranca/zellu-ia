from typing import Any, Dict, Literal, Optional
from pydantic import BaseModel, Field

class ActionRequest(BaseModel):
    """The stable envelope used before a side-effecting action executes."""

    request_id: str
    thread_id: str
    bot_id: str
    tool: str
    action: str
    intent: str
    target: Dict[str, Any] = Field(default_factory=dict)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    preview: str
    risk: Literal["read", "write", "external"] = "read"
    requires_approval: bool = True
    state: Literal["pending_approval", "approved"] = "pending_approval"
    created_at: str

class ActionResult(BaseModel):
    """The normalized result returned after an action is decided and run."""

    request_id: str
    status: Literal["completed", "failed", "denied", "expired"]
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str
