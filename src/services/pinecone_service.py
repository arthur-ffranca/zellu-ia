"""
Pinecone Service - Zellu IA Empresa

Serviço responsável pela comunicação com o Pinecone (vector database).
Gerencia a criação de índices, inserção e busca de vetores.

O que é Pinecone:
    Pinecone é um banco de dados vetorial na nuvem otimizado para
    busca por similaridade. Armazena embeddings e permite encontrar
    os vetores mais similares a uma query.

Arquitetura:
    Índice: zellu-empresa-docs (configurável)
    Namespaces: um por empresa (company_id)
    Vetores: chunks de documentos com embeddings

Configurações (via .env):
    PINECONE_API_KEY: Chave de API do Pinecone
    PINECONE_INDEX_NAME: Nome do índice (default: zellu-empresa-docs)
    PINECONE_DIMENSION: Dimensão dos vetores (default: 1536)
    PINECONE_ENVIRONMENT: Região AWS (default: us-east-1)

Classes:
    PineconeService: Gerencia operações no vector store

Uso:
    >>> from src.services import pinecone_service
    >>> results = pinecone_service.query(
    ...     vector=embedding,
    ...     namespace="empresa_123",
    ...     top_k=3
    ... )
"""

from typing import List, Dict, Any, Optional
from pinecone import Pinecone, ServerlessSpec
import logging

from config import get_settings

logger = logging.getLogger(__name__)


class PineconeService:
    """
    Serviço para interagir com o Pinecone Vector Database.

    Gerencia todas as operações de armazenamento e busca vetorial:
    - Criação/conexão com índice
    - Inserção de vetores (upsert)
    - Busca por similaridade (query)
    - Deleção de vetores

    Attributes:
        settings: Configurações da aplicação
        pc: Cliente Pinecone
        index_name: Nome do índice no Pinecone
        dimension: Dimensão dos vetores (1536 para text-embedding-3-small)
        _index: Referência ao índice (lazy loading)

    Example:
        >>> service = PineconeService()
        >>> service.upsert_vectors(
        ...     vectors=[{"id": "doc_1", "values": [...], "metadata": {...}}],
        ...     namespace="empresa_123"
        ... )
    """

    def __init__(self):
        """
        Inicializa o serviço com as configurações do Pinecone.

        Cria o cliente Pinecone mas não conecta ao índice ainda
        (lazy loading no primeiro uso).
        """
        self.settings = get_settings()
        self.pc = Pinecone(api_key=self.settings.pinecone_api_key)
        self.index_name = self.settings.pinecone_index_name
        self.dimension = self.settings.pinecone_dimension
        self._index = None

    def _get_or_create_index(self):
        """
        Obtém referência ao índice, criando-o se não existir.

        Utiliza lazy loading para não conectar até ser necessário.
        Se o índice não existir, cria automaticamente com as configurações
        definidas (serverless, AWS, região configurada).

        Returns:
            Referência ao índice Pinecone

        Note:
            O índice é criado como Serverless na AWS para reduzir custos
            e simplificar o gerenciamento.
        """
        if self._index is not None:
            return self._index

        # Verificar se índice existe
        existing_indexes = self.pc.list_indexes().names()

        if self.index_name not in existing_indexes:
            logger.info(f"Criando índice {self.index_name}...")
            self.pc.create_index(
                name=self.index_name,
                dimension=self.dimension,
                metric="cosine",
                spec=ServerlessSpec(
                    cloud="aws",
                    region=self.settings.pinecone_environment,
                ),
            )

        self._index = self.pc.Index(self.index_name)
        return self._index

    def upsert_vectors(
        self,
        vectors: List[Dict[str, Any]],
        namespace: str,
    ) -> int:
        """
        Insere ou atualiza vetores no Pinecone.

        Se um vetor com o mesmo ID já existir, ele é substituído.
        Vetores são agrupados em batches de 100 (limite do Pinecone).

        Args:
            vectors: Lista de dicionários, cada um contendo:
                - id: Identificador único do vetor
                - values: Lista de floats (embedding)
                - metadata: Dicionário com metadados (título, conteúdo, etc)
            namespace: Namespace para isolar dados por empresa (company_id)

        Returns:
            Quantidade total de vetores inseridos/atualizados

        Raises:
            Exception: Se houver erro na comunicação com Pinecone
        """
        try:
            index = self._get_or_create_index()

            # Pinecone aceita batches de até 100
            batch_size = 100
            total_upserted = 0

            for i in range(0, len(vectors), batch_size):
                batch = vectors[i:i + batch_size]
                index.upsert(vectors=batch, namespace=namespace)
                total_upserted += len(batch)

            logger.info(f"Upserted {total_upserted} vetores no namespace {namespace}")
            return total_upserted

        except Exception as e:
            logger.error(f"Erro ao inserir vetores: {e}")
            raise

    def query(
        self,
        vector: List[float],
        namespace: str,
        top_k: int = 3,
        filter: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """
        Busca os vetores mais similares a um vetor de query.

        Realiza busca por similaridade de cosseno no namespace especificado.
        Retorna os top_k vetores mais similares com seus scores e metadados.

        Args:
            vector: Vetor de embedding da query (1536 dimensões)
            namespace: Namespace da empresa (company_id)
            top_k: Número máximo de resultados a retornar (default: 3)
            filter: Filtro opcional de metadata, ex:
                {"category": {"$eq": "faq"}}

        Returns:
            Lista de dicionários com resultados:
                - id: ID do vetor
                - score: Similaridade (0.0 a 1.0, maior = mais similar)
                - metadata: Metadados do vetor (título, conteúdo, etc)

        Raises:
            Exception: Se houver erro na comunicação com Pinecone

        Example:
            >>> results = service.query(
            ...     vector=embedding,
            ...     namespace="empresa_123",
            ...     top_k=5
            ... )
            >>> for r in results:
            ...     print(f"{r['score']:.2f}: {r['metadata']['title']}")
        """
        try:
            index = self._get_or_create_index()

            results = index.query(
                vector=vector,
                namespace=namespace,
                top_k=top_k,
                include_metadata=True,
                filter=filter,
            )

            return [
                {
                    "id": match.id,
                    "score": match.score,
                    "metadata": match.metadata or {},
                }
                for match in results.matches
            ]

        except Exception as e:
            logger.error(f"Erro na busca: {e}")
            raise

    def delete_by_document(self, document_id: str, namespace: str) -> None:
        """
        Deleta todos os vetores (chunks) de um documento específico.

        Remove todos os chunks de um documento usando filtro de metadata.
        Útil quando um documento é atualizado ou removido da base.

        Args:
            document_id: ID do documento cujos chunks serão removidos
            namespace: Namespace da empresa (company_id)

        Raises:
            Exception: Se houver erro na comunicação com Pinecone

        Example:
            >>> service.delete_by_document("doc_123", "empresa_456")
        """
        try:
            index = self._get_or_create_index()

            # Deletar por filtro de metadata
            index.delete(
                filter={"document_id": {"$eq": document_id}},
                namespace=namespace,
            )

            logger.info(f"Deletados vetores do documento {document_id}")

        except Exception as e:
            logger.error(f"Erro ao deletar vetores: {e}")
            raise

    def delete_namespace(self, namespace: str) -> None:
        """
        Deleta todos os vetores de um namespace (empresa).

        Remove completamente todos os dados de uma empresa do índice.
        CUIDADO: Esta operação é irreversível!

        Args:
            namespace: Namespace da empresa a ser removido (company_id)

        Raises:
            Exception: Se houver erro na comunicação com Pinecone
        """
        try:
            index = self._get_or_create_index()
            index.delete(delete_all=True, namespace=namespace)
            logger.info(f"Namespace {namespace} deletado")

        except Exception as e:
            logger.error(f"Erro ao deletar namespace: {e}")
            raise

    def get_stats(self, namespace: Optional[str] = None) -> Dict[str, Any]:
        """
        Retorna estatísticas do índice ou de um namespace específico.

        Útil para monitoramento e debugging. Mostra quantidade de
        vetores por namespace.

        Args:
            namespace: Se informado, retorna stats apenas deste namespace.
                Se None, retorna stats de todos os namespaces.

        Returns:
            Dicionário com estatísticas:
                - Se namespace informado: {"namespace": str, "vector_count": int}
                - Se namespace None: {"total_vector_count": int, "namespaces": {...}}

        Raises:
            Exception: Se houver erro na comunicação com Pinecone
        """
        try:
            index = self._get_or_create_index()
            stats = index.describe_index_stats()

            if namespace:
                ns_stats = stats.namespaces.get(namespace, {})
                return {
                    "namespace": namespace,
                    "vector_count": getattr(ns_stats, "vector_count", 0),
                }

            return {
                "total_vector_count": stats.total_vector_count,
                "namespaces": {
                    ns: {"vector_count": data.vector_count}
                    for ns, data in stats.namespaces.items()
                },
            }

        except Exception as e:
            logger.error(f"Erro ao obter stats: {e}")
            raise


# =============================================================================
# INSTÂNCIA GLOBAL
# =============================================================================
# Singleton para uso em toda a aplicação
# Import: from src.services import pinecone_service
pinecone_service = PineconeService()
