"""
API Routes - Zellu IA Empresa

Exporta todos os routers de endpoints.
Os routers são registrados no app.py principal com prefixo /api.

Routers disponíveis:
    - negotiation_router: /negotiation/* - Endpoints do Sender Agent (ZelinhU)
    - company_response_router: /api/webhooks/company/* - Company Response AI
    - external_chat_router: /api/external/chat/* - External Chat (Chat Isca)
"""

# Router de negociação (Sender Agent)
from .negotiation import router as negotiation_router

# Router do Company Response AI
from .company_response import router as company_response_router

# Router do External Chat
from .external_chat import router as external_chat_router

__all__ = [
    "negotiation_router",
    "company_response_router",
    "external_chat_router",
]
