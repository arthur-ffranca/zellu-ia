"""
Services - Zellu IA Empresa

Este módulo contém todos os serviços de infraestrutura da aplicação.
Os serviços são responsáveis pela comunicação com APIs externas e
processamento de dados.

Arquitetura de Serviços:
    ┌─────────────────────────────────────────────────────────────┐
    │                      RAGService                              │
    │  (Orquestrador principal - busca semântica + contexto)      │
    └─────────────────────┬───────────────────────────────────────┘
                          │
         ┌────────────────┼────────────────┐
         ▼                ▼                ▼
    ┌──────────┐   ┌────────────┐   ┌──────────────────┐
    │Embedding │   │  Pinecone  │   │    Document      │
    │ Service  │   │  Service   │   │    Processor     │
    └──────────┘   └────────────┘   └──────────────────┘
     (OpenAI)      (Vector DB)      (Chunking/Extração)

Serviços disponíveis:
    - EmbeddingService: Gera embeddings via OpenAI
    - PineconeService: Gerencia o vector store (Pinecone)
    - DocumentProcessor: Processa e divide documentos
    - RAGService: Orquestra todo o fluxo de RAG

Uso:
    >>> from src.services import rag_service
    >>> result = await rag_service.search("emp_123", "Qual o prazo?")

Instâncias Globais:
    Todos os serviços exportam instâncias singleton prontas para uso:
    - embedding_service
    - pinecone_service
    - document_processor
    - rag_service
"""

from .embedding_service import embedding_service, EmbeddingService
from .pinecone_service import pinecone_service, PineconeService
from .document_processor import document_processor, DocumentProcessor
from .document_analyzer import document_analyzer, DocumentAnalyzer
from .conversation_analyzer import conversation_analyzer, ConversationAnalyzer
from .rag_indexer import RAGIndexer
from .document_extractor import DocumentExtractor
from .firecrawl_service import firecrawl_service, FirecrawlService, get_firecrawl_service
from .recommendation_scorer import recommendation_scorer, RecommendationScorer, score_recommendations

__all__ = [
    # Instâncias singleton (uso recomendado)
    "embedding_service",
    "pinecone_service",
    "document_processor",
    "document_analyzer",
    "conversation_analyzer",
    "firecrawl_service",
    "recommendation_scorer",
    "score_recommendations",
    # Classes (para testes ou instâncias customizadas)
    "EmbeddingService",
    "PineconeService",
    "DocumentProcessor",
    "DocumentAnalyzer",
    "ConversationAnalyzer",
    "RAGIndexer",
    "DocumentExtractor",
    "FirecrawlService",
    "get_firecrawl_service",
    "RecommendationScorer",
]
