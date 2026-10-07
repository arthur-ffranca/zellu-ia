# -*- coding: utf-8 -*-
"""RAG Service for legal rights consultation using native runtime and Pinecone."""

from typing import List, Dict, Any, Optional
from pathlib import Path
import time

from src.llm import ChatModel
from src.services.text_chunks import TextDocument as Document
from src.services.text_chunks import TextChunker
from pinecone import Pinecone, ServerlessSpec

from config import Settings
from src.services.ai_usage_tracker import track_usage
# Embeddings com cache transparente (sha256 -> vetor, memoria + Redis).
from src.services.embedding_cache import CachedOpenAIEmbeddings as OpenAIEmbeddings


class RAGService:
    """Service for Retrieval-Augmented Generation using legal documents with Pinecone."""

    def __init__(self, settings: Settings):
        """Initialize RAG service with Pinecone.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self.embeddings = OpenAIEmbeddings(
            openai_api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_EMBEDDING_MODEL
        )
        self.llm = ChatModel(
            openai_api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_MODEL,
            temperature=0.3
        )

        # Initialize Pinecone
        self.pc = None
        self.index = self.settings.PINECONE_INDEX_NAME
        self.dimension = self.settings.PINECONE_DIMENSION
        self.vector_store = None
        self._init_pinecone()

    def _init_pinecone(self):
        """Initialize Pinecone index."""
        if not self.settings.PINECONE_API_KEY:
            print("[WARNING] PINECONE_API_KEY nao configurado. Sistema RAG desabilitado.")
            return

        try:
            # Initialize Pinecone client
            self.pc = Pinecone(api_key=self.settings.PINECONE_API_KEY)

            # Check if index exists
            existing_indexes = self.pc.list_indexes().names()

            if self.settings.PINECONE_INDEX_NAME not in existing_indexes:
                print(f"Creating Pinecone index: {self.settings.PINECONE_INDEX_NAME}")

                # Create index with OpenAI embedding dimensions (1536 for text-embedding-3-small)
                self.pc.create_index(
                    name=self.settings.PINECONE_INDEX_NAME,
                    dimension=self.dimension,
                    metric="cosine",
                    spec=ServerlessSpec(cloud="aws", region="us-east-1")
                )

                # Wait for index to be ready
                while not self.pc.describe_index(self.settings.PINECONE_INDEX_NAME).status['ready']:
                    time.sleep(1)

                print(f"[OK] Pinecone index created: {self.settings.PINECONE_INDEX_NAME}")
            else:
                print(f"[OK] Using existing Pinecone index: {self.settings.PINECONE_INDEX_NAME}")

            # Get index
            self.index = self.pc.Index(self.settings.PINECONE_INDEX_NAME)

            # Mark vector store as available (simplified mode)
            self.vector_store = True

            # Get stats
            stats = self.index.describe_index_stats()
            print(f"[OK] Pinecone vector store ready - Total vectors: {stats.get('total_vector_count', 0)}")

        except Exception as e:
            print(f"[ERROR] Erro ao inicializar Pinecone: {e}")
            self.vector_store = None

    def add_documents(self, documents: List[Document]):
        """Add documents to Pinecone vector store.

        Args:
            documents: List of documents to add
        """
        if not self.vector_store:
            print("[WARNING] Vector store nao disponivel. Documentos nao foram adicionados.")
            return

        if not documents:
            return

        # Split documents into chunks
        text_splitter = TextChunker(
            chunk_size=self.settings.RAG_CHUNK_SIZE,
            chunk_overlap=self.settings.RAG_CHUNK_OVERLAP,
            separators=["\n\n", "\n", ". ", " ", ""]
        )

        splits = text_splitter.split_documents(documents)

        # Add to Pinecone using native API
        try:
            vectors = []
            for i, split in enumerate(splits):
                # Generate embedding
                embedding = self.embeddings.embed_query(split.page_content)

                # Create vector with metadata
                vector_id = f"{split.metadata.get('filename', 'doc')}_{i}"
                vectors.append({
                    "id": vector_id,
                    "values": embedding,
                    "metadata": {
                        "text": split.page_content,
                        **split.metadata
                    }
                })

            # Upsert in batches of 100
            batch_size = 100
            for i in range(0, len(vectors), batch_size):
                batch = vectors[i:i + batch_size]
                self.index.upsert(vectors=batch)

            print(f"[OK] Adicionados {len(splits)} chunks ao Pinecone")
        except Exception as e:
            print(f"[ERROR] Erro ao adicionar documentos ao Pinecone: {e}")

    def _extract_enriched_metadata(self, content: str, category: str, filename: str) -> Dict[str, Any]:
        """Extract enriched metadata from document content.

        Args:
            content: Document text content
            category: Document category
            filename: File name

        Returns:
            Dictionary with enriched metadata
        """
        metadata = {
            "source": filename,
            "filename": filename,
            "category": category,
            "subcategories": [],
            "keywords": [],
            "has_monetary_values": False,
            "has_deadlines": False,
            "urgency_level": "normal"
        }

        # Define category-specific enrichment
        category_enrichment = {
            "Trabalhista": {
                "subcategories": ["Demissão", "Jornada de Trabalho", "Férias", "FGTS", "Verbas Rescisórias",
                                 "Estabilidade", "Saúde e Segurança", "Maternidade", "Ações Trabalhistas"],
                "keywords": ["demissão", "justa causa", "aviso prévio", "FGTS", "13º salário", "férias",
                            "horas extras", "CLT", "rescisão", "salário", "seguro-desemprego", "estabilidade",
                            "gestante", "acidente de trabalho", "assédio", "intervalo"],
                "typical_values": "verbas rescisórias, multa 40% FGTS, indenizações por dano moral",
                "common_deadlines": "2 anos após fim do contrato, 5 anos para verbas",
                "urgency_keywords": ["demissão", "assédio", "acidente", "não pagamento"]
            },
            "Consumidor": {
                "subcategories": ["Direito de Arrependimento", "Vícios do Produto", "Garantia", "Serviços",
                                 "Práticas Abusivas", "Publicidade", "Contratos", "Cobrança de Dívidas",
                                 "Negativação", "Telefonia", "Planos de Saúde", "E-commerce", "Transporte Aéreo"],
                "keywords": ["CDC", "arrependimento", "garantia", "vício", "defeito", "abusiva", "negativa",
                            "reembolso", "troca", "devolução", "SPC", "Serasa", "Procon", "fraude",
                            "clonagem", "cobrança", "indenização"],
                "typical_values": "devolução em dobro, indenização por dano moral, restituição integral",
                "common_deadlines": "7 dias arrependimento, 30/90 dias garantia, 5 anos prescrição",
                "urgency_keywords": ["fraude", "clonagem", "cobrança vexatória", "negativa indevida"]
            },
            "Civil": {
                "subcategories": ["Contratos", "Responsabilidade Civil", "Família", "Sucessões", "Imóveis"],
                "keywords": ["contrato", "indenização", "dano moral", "dano material", "divórcio", "alimentos",
                            "herança", "inventário", "locação", "posse", "propriedade"],
                "typical_values": "indenizações, pensão alimentícia, partilha de bens",
                "common_deadlines": "diversos conforme ação, geralmente 3-10 anos",
                "urgency_keywords": ["despejo", "alimentos", "urgência"]
            },
            "Criminal": {
                "subcategories": ["Crimes contra Pessoa", "Crimes contra Patrimônio", "Direitos do Acusado",
                                 "Medidas Protetivas", "Processo Penal"],
                "keywords": ["crime", "denúncia", "acusação", "defesa", "prisão", "flagrante", "habeas corpus",
                            "medida protetiva", "boletim de ocorrência", "investigação", "processo"],
                "typical_values": "fiança, indenizações por dano moral",
                "common_deadlines": "6 meses para queixa-crime, prescrição conforme pena",
                "urgency_keywords": ["prisão", "flagrante", "violência", "medida protetiva", "ameaça"]
            },
            "Saúde": {
                "subcategories": ["Plano de Saúde", "SUS", "Medicamentos", "Tratamentos", "Responsabilidade Médica"],
                "keywords": ["ANS", "plano de saúde", "negativa", "cobertura", "cirurgia", "medicamento",
                            "SUS", "erro médico", "urgência", "emergência", "internação"],
                "typical_values": "cobertura de tratamentos, fornecimento de medicamentos, indenizações",
                "common_deadlines": "24h carência emergência, prazos ANS",
                "urgency_keywords": ["emergência", "urgência", "negativa de cobertura", "cirurgia", "risco de vida"]
            }
        }

        enrichment = category_enrichment.get(category, {})

        # Add subcategories found in content
        content_lower = content.lower()
        for subcat in enrichment.get("subcategories", []):
            if subcat.lower() in content_lower:
                metadata["subcategories"].append(subcat)

        # Add keywords found in content
        for keyword in enrichment.get("keywords", []):
            if keyword in content_lower:
                metadata["keywords"].append(keyword)

        # Check for monetary values
        if any(term in content_lower for term in ["r$", "reais", "salário", "indenização", "multa", "valor"]):
            metadata["has_monetary_values"] = True
            metadata["typical_values"] = enrichment.get("typical_values", "")

        # Check for deadlines
        if any(term in content_lower for term in ["prazo", "dias", "meses", "anos", "prescrição"]):
            metadata["has_deadlines"] = True
            metadata["common_deadlines"] = enrichment.get("common_deadlines", "")

        # Determine urgency level
        urgency_keywords = enrichment.get("urgency_keywords", [])
        if any(keyword in content_lower for keyword in urgency_keywords):
            metadata["urgency_level"] = "high"

        # Add category-specific helpful info
        metadata["category_description"] = enrichment.get("keywords", [])[:5]  # Top 5 keywords

        return metadata

    def load_documents_from_directory(self, directory: Optional[str] = None):
        """Load all text documents from a directory and add to Pinecone.

        Args:
            directory: Directory path (defaults to DOCUMENTS_PATH)
        """
        if directory is None:
            directory = self.settings.DOCUMENTS_PATH

        documents = []
        dir_path = Path(directory)

        if not dir_path.exists():
            print(f"[ERROR] Diretorio de documentos nao encontrado: {directory}")
            return

        # Load .txt files
        for file_path in dir_path.glob("*.txt"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                    # Extract category from filename
                    category = file_path.stem.replace("direito_", "").replace("_", " ").title()

                    # Extract enriched metadata
                    enriched_metadata = self._extract_enriched_metadata(
                        content,
                        category,
                        file_path.name
                    )

                    doc = Document(
                        page_content=content,
                        metadata=enriched_metadata
                    )
                    documents.append(doc)

                    print(f"[OK] Carregado: {file_path.name}")
                    print(f"    Categoria: {category}")
                    print(f"    Subcategorias: {len(enriched_metadata['subcategories'])}")
                    print(f"    Keywords: {len(enriched_metadata['keywords'])}")
                    print(f"    Urgência: {enriched_metadata['urgency_level']}")
            except Exception as e:
                print(f"[ERROR] Erro ao carregar {file_path}: {e}")

        if documents:
            print(f"\n[INFO] Total de documentos carregados: {len(documents)}")
            self.add_documents(documents)
        else:
            print(f"[WARNING] Nenhum documento .txt encontrado em {directory}")

    def query(self, question: str, legal_category: Optional[str] = None) -> Dict[str, Any]:
        """Query the RAG system with a question.

        Args:
            question: The question to ask
            legal_category: Optional legal category filter

        Returns:
            Dictionary with answer and source documents
        """
        if not self.settings.RAG_ENABLED or not self.vector_store:
            return {
                "answer": "Sistema RAG desabilitado ou não disponível.",
                "sources": []
            }

        try:
            # Search for relevant documents
            query_embedding = self.embeddings.embed_query(question)

            # Build filter if category provided
            filter_dict = {}
            if legal_category:
                filter_dict = {"category": legal_category}

            # Search in Pinecone
            results = self.index.query(
                vector=query_embedding,
                top_k=self.settings.RAG_TOP_K,
                include_metadata=True,
                filter=filter_dict if filter_dict else None
            )

            # Extract documents
            documents = []
            context_parts = []
            for match in results.get('matches', []):
                text = match.get('metadata', {}).get('text', '')
                if text:
                    context_parts.append(text)
                    documents.append({
                        "content": text[:200] + "...",
                        "metadata": match.get('metadata', {})
                    })

            if not context_parts:
                return {
                    "answer": "Não encontrei informações específicas sobre isso na base de conhecimento. Recomendo consultar um advogado especializado.",
                    "sources": []
                }

            # Build context
            context = "\n\n".join(context_parts)

            # Create prompt
            prompt = f"""Você é um assistente jurídico especializado em direitos brasileiros.
Use APENAS as informações fornecidas no contexto abaixo para responder à pergunta.
Se a informação não estiver no contexto, diga que não possui essa informação específica e recomende consultar um advogado.

Contexto:
{context}

Pergunta: {question}

Resposta (em português, clara, objetiva e acolhedora):"""

            # Get answer from LLM
            _t0 = time.perf_counter()
            response = self.llm.complete_sync(prompt)
            track_usage(
                module="kb_chat",
                model=self.settings.OPENAI_MODEL,
                response=response,
                event_type="rag_query",
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )

            return {
                "answer": response.content if hasattr(response, 'content') else str(response),
                "sources": documents
            }
        except Exception as e:
            print(f"[ERROR] Erro ao consultar RAG: {e}")
            return {
                "answer": "Desculpe, ocorreu um erro ao consultar o banco de conhecimento. Por favor, tente novamente.",
                "sources": []
            }

    async def search(self, query: str, top_k: int = 3) -> List[Dict]:
        """Search for similar documents in Pinecone using embeddings.

        Args:
            query: Search query
            top_k: Number of results to return

        Returns:
            List of similar documents with metadata and scores
        """
        if not self.vector_store or not self.index:
            return []

        try:
            # Generate embedding for query
            import asyncio
            query_embedding = await asyncio.to_thread(self.embeddings.embed_query, query)

            # Search in Pinecone
            results = await asyncio.to_thread(self.index.query,
                vector=query_embedding,
                top_k=top_k,
                namespace=self.settings.LEGAL_RAG_NAMESPACE,
                include_metadata=True
            )

            # Format results
            documents = []
            for match in results.get('matches', []):
                documents.append({
                    'id': match.get('id'),
                    'content': match.get('metadata', {}).get('text', ''),
                    'metadata': match.get('metadata', {}),
                    'score': match.get('score', 0.0)
                })

            return documents
        except Exception as e:
            print(f"[ERROR] Erro ao buscar documentos similares: {e}")
            return []

    def get_legal_info(self, category: str, description: str) -> str:
        """Get legal information for a specific category and problem description.

        Args:
            category: Legal category (Civil, Trabalhista, Criminal, Consumidor, Saude)
            description: Problem description

        Returns:
            Relevant legal information
        """
        # Normalize category name
        category_map = {
            "civil": "Civil",
            "trabalhista": "Trabalhista",
            "criminal": "Criminal",
            "consumidor": "Consumidor",
            "saude": "Saúde",
            "saúde": "Saúde"
        }

        normalized_category = category_map.get(category.lower(), category)

        query = f"Quais são os direitos relacionados a: {description}"
        result = self.query(query, legal_category=normalized_category)

        if result["sources"]:
            return result["answer"]
        else:
            return f"Entendo que você tem uma questão sobre {normalized_category}. " \
                   f"Recomendo consultar um advogado especializado para orientação específica sobre seu caso: {description}"

    def clear_index(self):
        """Clear all vectors from Pinecone index (use with caution!)."""
        if not self.index:
            print("[WARNING] Index nao disponivel")
            return

        try:
            self.index.delete(delete_all=True)
            print("[OK] Todos os vetores foram removidos do indice Pinecone")
        except Exception as e:
            print(f"[ERROR] Erro ao limpar indice: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get Pinecone index statistics.

        Returns:
            Dictionary with index stats
        """
        if not self.index:
            return {"error": "Index não disponível"}

        try:
            stats = self.index.describe_index_stats()
            return {
                "total_vectors": stats.get('total_vector_count', 0),
                "dimension": stats.get('dimension', 0),
                "index_fullness": stats.get('index_fullness', 0),
                "namespaces": stats.get('namespaces', {})
            }
        except Exception as e:
            return {"error": str(e)}
