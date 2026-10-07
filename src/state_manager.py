"""State management for conversation sessions."""

from typing import Dict, Optional
from uuid import uuid4
from agents.state import ConversationState
from datetime import datetime


class StateManager:
    """In-memory state manager for conversation sessions."""

    def __init__(self):
        """Initialize the state manager."""
        self._states: Dict[str, ConversationState] = {}

    def get_state(self, chat_id: str) -> Optional[ConversationState]:
        """Get the conversation state for a chat session.

        Args:
            chat_id: The chat session ID

        Returns:
            The conversation state or None if not found
        """
        return self._states.get(chat_id)

    def create_state(
        self,
        chat_id: str,
        user_id: str,
        message_type: str = "text",
        body_message: str = "",
        audio: Optional[list] = None,
        files: Optional[list] = None
    ) -> ConversationState:
        """Create a new conversation state.

        Args:
            chat_id: The chat session ID
            user_id: The user ID
            message_type: Type of the message
            body_message: The message content
            audio: Optional audio URLs
            files: Optional file URLs

        Returns:
            The newly created conversation state
        """
        state: ConversationState = {
            "id": str(uuid4()),
            "chat_id": chat_id,
            "user_id": user_id,
            "messages": [],
            "message_type": message_type,
            "body_message": body_message,
            "client_name": None,
            "client_cpf": None,
            "problem_type": None,
            "problem_description": None,
            "legal_category": None,
            "urgency": None,
            "client_rights": [],
            "potential_gain": None,
            "relevant_documents": [],
            "suggested_next_steps": [],
            "required_documents": [],
            "case_evidence": [],
            "evidence_ledger": {"total_documents": 0, "by_type": {}, "documents": [], "conflicts": [], "pending_conflicts": []},
            "evidence_conflict_pending": False,
            "current_agent": "intake",
            "step": 0,
            "completed": False,
            "validated": False,
            "ready_for_classification": False,
            "audio": audio,
            "files": files or [],
            "api_error_count": 0,
            "pending_reprocess_message": None
        }
        self._states[chat_id] = state
        return state

    def update_state(self, chat_id: str, updates: Dict) -> Optional[ConversationState]:
        """Update an existing conversation state.

        Args:
            chat_id: The chat session ID
            updates: Dictionary of fields to update

        Returns:
            The updated conversation state or None if not found
        """
        state = self._states.get(chat_id)
        if not state:
            return None

        # Update the state with new values
        for key, value in updates.items():
            state[key] = value  # type: ignore

        self._states[chat_id] = state
        return state

    def delete_state(self, chat_id: str) -> bool:
        """Delete a conversation state.

        Args:
            chat_id: The chat session ID

        Returns:
            True if deleted, False if not found
        """
        if chat_id in self._states:
            del self._states[chat_id]
            return True
        return False

    def list_states(self) -> Dict[str, ConversationState]:
        """List all conversation states.

        Returns:
            Dictionary of all states keyed by chat_id
        """
        return self._states.copy()

    def clear_completed(self) -> int:
        """Clear all completed conversation states.

        Returns:
            Number of states cleared
        """
        to_delete = [
            chat_id for chat_id, state in self._states.items()
            if state.get("completed", False)
        ]
        for chat_id in to_delete:
            del self._states[chat_id]
        return len(to_delete)
