# -*- coding: utf-8 -*-
"""
Schemas das Bases de Conhecimento - Zellu IA Empresa

Define a estrutura dos documentos para cada tipo de base.
Estes schemas sao usados para:
1. Validar documentos antes de indexar no Pinecone
2. Definir metadados para filtragem nas buscas
3. Garantir consistencia dos dados

ESTRUTURA NO PINECONE:
- Index: zellu-empresa-docs
- Namespace para empresas: company_{id}
- Namespace compartilhado: global

METADATA PADRAO:
- knowledge_base: ID da base (ex: "jurisprudencia", "politicas_gerais")
- document_type: Tipo do documento dentro da base
- created_at: Data de criacao
- source: Origem do documento
"""

from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from pydantic import BaseModel, Field
from enum import Enum


# =============================================================================
# ENUMS PARA JURISPRUDENCIA
# =============================================================================

class TribunalType(str, Enum):
    """Tipos de tribunais."""
    STF = "STF"           # Supremo Tribunal Federal
    STJ = "STJ"           # Superior Tribunal de Justica
    TJ = "TJ"             # Tribunal de Justica (estadual)
    TRF = "TRF"           # Tribunal Regional Federal
    JEC = "JEC"           # Juizado Especial Civel
    PROCON = "PROCON"     # Decisoes do PROCON
    OTHER = "OTHER"       # Outros


class DecisionType(str, Enum):
    """Tipos de decisao."""
    FAVORAVEL_CONSUMIDOR = "favoravel_consumidor"
    FAVORAVEL_EMPRESA = "favoravel_empresa"
    PARCIALMENTE_PROCEDENTE = "parcialmente_procedente"
    ACORDO = "acordo"


class ConsumerRightCategory(str, Enum):
    """Categorias de direitos do consumidor."""
    COBRANCA_INDEVIDA = "cobranca_indevida"
    VICIO_PRODUTO = "vicio_produto"
    VICIO_SERVICO = "vicio_servico"
    PROPAGANDA_ENGANOSA = "propaganda_enganosa"
    NEGATIVACAO_INDEVIDA = "negativacao_indevida"
    ATRASO_ENTREGA = "atraso_entrega"
    CANCELAMENTO = "cancelamento"
    GARANTIA = "garantia"
    DANOS_MORAIS = "danos_morais"
    OUTROS = "outros"


# =============================================================================
# ENUMS PARA LEGISLACAO
# =============================================================================

class LegislationType(str, Enum):
    """Tipos de legislacao."""
    LEI = "lei"
    DECRETO = "decreto"
    RESOLUCAO = "resolucao"
    PORTARIA = "portaria"
    SUMULA = "sumula"
    CODIGO = "codigo"


# =============================================================================
# SCHEMA BASE
# =============================================================================

class KnowledgeBaseDocument(BaseModel):
    """Schema base para documentos de qualquer base de conhecimento."""

    # Identificacao
    id: str = Field(..., description="ID unico do documento")
    knowledge_base: str = Field(..., description="ID da base de conhecimento")

    # Conteudo
    title: str = Field(..., description="Titulo do documento")
    content: str = Field(..., description="Conteudo principal para embedding")
    summary: Optional[str] = Field(None, description="Resumo do documento")

    # Metadata
    source: Optional[str] = Field(None, description="Origem do documento")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)

    # Controle
    is_active: bool = Field(True, description="Se o documento esta ativo")

    def to_pinecone_metadata(self) -> Dict[str, Any]:
        """Converte para metadata do Pinecone."""
        return {
            "id": self.id,
            "knowledge_base": self.knowledge_base,
            "title": self.title,
            "summary": self.summary or "",
            "source": self.source or "",
            "created_at": self.created_at.isoformat(),
            "tags": ",".join(self.tags),
            "is_active": self.is_active,
        }


# =============================================================================
# SCHEMA: JURISPRUDENCIA
# =============================================================================

class JurisprudenciaDocument(KnowledgeBaseDocument):
    """
    Schema para documentos de jurisprudencia.

    Exemplo de uso:
        doc = JurisprudenciaDocument(
            id="jurisprudencia_stj_123",
            knowledge_base="jurisprudencia",
            title="REsp 1.234.567/SP",
            content="EMENTA: Direito do consumidor. Cobranca indevida...",
            tribunal=TribunalType.STJ,
            numero_processo="REsp 1.234.567/SP",
            data_julgamento=datetime(2024, 5, 15),
            categoria=ConsumerRightCategory.COBRANCA_INDEVIDA,
            decisao=DecisionType.FAVORAVEL_CONSUMIDOR,
            valor_indenizacao=5000.00,
        )
    """

    knowledge_base: Literal["jurisprudencia"] = "jurisprudencia"

    # Dados do processo
    tribunal: TribunalType = Field(..., description="Tribunal que proferiu a decisao")
    numero_processo: str = Field(..., description="Numero do processo")
    data_julgamento: datetime = Field(..., description="Data do julgamento")
    relator: Optional[str] = Field(None, description="Nome do relator")

    # Classificacao
    categoria: ConsumerRightCategory = Field(..., description="Categoria do direito")
    subcategoria: Optional[str] = Field(None, description="Subcategoria especifica")
    decisao: DecisionType = Field(..., description="Tipo de decisao")

    # Valores (para referencia em negociacoes)
    valor_causa: Optional[float] = Field(None, description="Valor da causa")
    valor_indenizacao: Optional[float] = Field(None, description="Valor da indenizacao concedida")
    valor_danos_morais: Optional[float] = Field(None, description="Valor de danos morais")

    # Contexto
    tese_juridica: Optional[str] = Field(None, description="Tese juridica aplicada")
    precedentes_citados: List[str] = Field(default_factory=list)

    def to_pinecone_metadata(self) -> Dict[str, Any]:
        """Converte para metadata do Pinecone com campos especificos."""
        base = super().to_pinecone_metadata()
        base.update({
            "document_type": "jurisprudencia",
            "tribunal": self.tribunal.value,
            "numero_processo": self.numero_processo,
            "data_julgamento": self.data_julgamento.isoformat(),
            "relator": self.relator or "",
            "categoria": self.categoria.value,
            "subcategoria": self.subcategoria or "",
            "decisao": self.decisao.value,
            "valor_causa": self.valor_causa or 0,
            "valor_indenizacao": self.valor_indenizacao or 0,
            "valor_danos_morais": self.valor_danos_morais or 0,
            "tese_juridica": self.tese_juridica or "",
        })
        return base


# =============================================================================
# SCHEMA: LEGISLACAO
# =============================================================================

class LegislacaoDocument(KnowledgeBaseDocument):
    """
    Schema para documentos de legislacao.

    Exemplo de uso:
        doc = LegislacaoDocument(
            id="legislacao_cdc_art_18",
            knowledge_base="legislacao",
            title="CDC - Art. 18 - Vicio do Produto",
            content="Art. 18. Os fornecedores de produtos de consumo duraveis...",
            tipo=LegislationType.CODIGO,
            numero="8.078",
            ano=1990,
            artigo="18",
            categoria=ConsumerRightCategory.VICIO_PRODUTO,
        )
    """

    knowledge_base: Literal["legislacao"] = "legislacao"

    # Dados da legislacao
    tipo: LegislationType = Field(..., description="Tipo de legislacao")
    numero: str = Field(..., description="Numero da lei/decreto/etc")
    ano: int = Field(..., description="Ano de publicacao")

    # Estrutura
    artigo: Optional[str] = Field(None, description="Numero do artigo")
    paragrafo: Optional[str] = Field(None, description="Paragrafo especifico")
    inciso: Optional[str] = Field(None, description="Inciso especifico")

    # Classificacao
    categoria: Optional[ConsumerRightCategory] = Field(None, description="Categoria relacionada")
    palavras_chave: List[str] = Field(default_factory=list)

    # Vigencia
    data_publicacao: Optional[datetime] = Field(None, description="Data de publicacao")
    em_vigor: bool = Field(True, description="Se esta em vigor")

    def to_pinecone_metadata(self) -> Dict[str, Any]:
        """Converte para metadata do Pinecone com campos especificos."""
        base = super().to_pinecone_metadata()
        base.update({
            "document_type": "legislacao",
            "tipo": self.tipo.value,
            "numero": self.numero,
            "ano": self.ano,
            "artigo": self.artigo or "",
            "paragrafo": self.paragrafo or "",
            "inciso": self.inciso or "",
            "categoria": self.categoria.value if self.categoria else "",
            "palavras_chave": ",".join(self.palavras_chave),
            "em_vigor": self.em_vigor,
        })
        return base


# =============================================================================
# SCHEMA: DOCUMENTO GENERICO DA EMPRESA
# =============================================================================

class CompanyDocument(KnowledgeBaseDocument):
    """
    Schema para documentos genericos de empresas.

    Usado para bases como: politicas_gerais, produtos_servicos, faq, etc.

    Exemplo:
        doc = CompanyDocument(
            id="doc_123",
            knowledge_base="politicas_gerais",
            title="Politica de Troca",
            content="A empresa oferece troca em ate 30 dias...",
            company_id="company_456",
        )
    """

    # Identificacao da empresa
    company_id: str = Field(..., description="ID da empresa dona do documento")

    # Metadata adicional
    file_name: Optional[str] = Field(None, description="Nome do arquivo original")
    file_type: Optional[str] = Field(None, description="Tipo do arquivo (pdf, docx, txt)")
    file_url: Optional[str] = Field(None, description="URL do arquivo no MinIO")

    def to_pinecone_metadata(self) -> Dict[str, Any]:
        """Converte para metadata do Pinecone."""
        base = super().to_pinecone_metadata()
        base.update({
            "document_type": "company_document",
            "company_id": self.company_id,
            "file_name": self.file_name or "",
            "file_type": self.file_type or "",
        })
        return base
