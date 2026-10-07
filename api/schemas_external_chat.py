# -*- coding: utf-8 -*-
"""
Schemas para External Chat (Chat Isca).

Usa o MESMO formato de payload do Company Response (chat interno),
conforme especificacao da Zellu.

Para visitantes:
- Sem autenticacao
- Usa usuario zellu_chat
- Campos obrigatorios: name, email, phone
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

# Reutilizar schemas do company response (mesmo formato)
from api.schemas_company_response import (
    TicketContext,
    ClientContext,
    CompanyContext,
    PayloadContext,
    MessageHistoryItem,
    KnowledgeBase,
    TemporaryRule,
    AIConfig,
    CompanyResponsePayload
)


# ===== REQUEST (Zellu -> IA) - MESMO FORMATO DO INTERNAL =====

class ExternalChatRequest(CompanyResponsePayload):
    """
    Request do External Chat - MESMO FORMATO do Company Response.

    Herda de CompanyResponsePayload para garantir compatibilidade.

    Campos obrigatorios para visitante:
    - context.client.name
    - context.client.email
    - context.client.phone
    """

    class Config:
        json_schema_extra = {
            "example": {
                "sessionId": "ext_clx1234567890",
                "lastMessageId": "msg_001",
                "context": {
                    "ticket": {
                        "id": "ticket_ext_001",
                        "title": "Nova reclamacao",
                        "description": "Problema com produto"
                    },
                    "client": {
                        "name": "Joao da Silva",
                        "email": "joao@email.com",
                        "phone": "(11) 99999-9999"
                    },
                    "company": {
                        "id": "zellu_chat",
                        "name": "Zellu"
                    }
                },
                "messageHistory": [
                    {
                        "id": "msg_001",
                        "origin": "zelinhu",
                        "type": "text",
                        "body": "Oi, quero fazer uma reclamacao"
                    }
                ],
                "config": {
                    "model": "gpt-4o-mini",
                    "maxTokens": 1500,
                    "temperature": 0.7
                }
            }
        }


# ===== RESPONSE (IA -> Zellu) =====

class ExternalChatResponseMessage(BaseModel):
    """Mensagem de resposta da IA para o external chat."""
    role: str = Field("assistant", description="Sempre 'assistant'")
    content: str = Field(..., description="Texto da resposta")
    isFinished: bool = Field(False, description="True quando coleta completa")
    awaitingContinuation: bool = Field(False, description="True se vai enviar mais mensagens")
    messagedata: Optional[Dict[str, Any]] = Field(
        None,
        description="Dados estruturados por mensagem: analysisData e handType "
                    "(maozinha animada do avatar: heart | shake | snap | tchau)"
    )


class ExternalChatResponse(BaseModel):
    """
    Response do External Chat.

    Formato de resposta para o frontend.
    Inclui chat_id para o backend usar nas operacoes subsequentes.
    """
    chat_id: Optional[str] = Field(None, description="ID do chat (mesmo enviado pelo backend)")
    messages: List[ExternalChatResponseMessage] = Field(..., description="Mensagens de resposta")

    class Config:
        json_schema_extra = {
            "example": {
                "chat_id": "uuid-do-chat",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "Ola! Sou a assistente da Zellu.",
                        "isFinished": False,
                        "awaitingContinuation": False
                    }
                ]
            }
        }


# ===== ANALYSIS DATA (payload-spec.md) =====

class UserInfo(BaseModel):
    """Informacoes do usuario (obrigatorio no external chat)."""
    first_name: Optional[str] = Field(None, description="Primeiro nome")
    last_name: Optional[str] = Field(None, description="Sobrenome")
    name: str = Field(..., description="Nome completo")
    email: str = Field(..., description="Email (OBRIGATORIO)")
    phone: str = Field(..., description="Telefone com DDD (OBRIGATORIO)")
    cpf: Optional[str] = Field(None, description="CPF (opcional)")
    address: Optional[str] = Field(None, description="Endereco (opcional)")


class OpposingParty(BaseModel):
    """Parte contraria (empresa reclamada)."""
    type: str = Field("pj", description="Tipo: pf ou pj")
    name: str = Field(..., description="Nome da empresa/pessoa")
    document: str = Field(..., description="CNPJ (OBRIGATORIO) ou CPF")
    email: Optional[str] = Field(None, description="Email")
    phone: Optional[str] = Field(None, description="Telefone")
    address: Optional[str] = Field(None, description="Endereco")


class CaseDetails(BaseModel):
    """Detalhes do caso."""
    title: str = Field(..., description="Titulo curto para o ticket")
    description: str = Field(..., description="Descricao detalhada")
    expectedSolution: Optional[str] = Field(None, description="Solucao esperada")
    documents: Optional[List[str]] = Field(default_factory=list, description="URLs de documentos")


class Recommendation(BaseModel):
    """Recomendacao de solucao."""
    type: str = Field(..., description="amigavel, extrajudicial ou judicial")
    score: float = Field(..., ge=0, le=10, description="Score de 0 a 10")
    reason: str = Field(..., description="Justificativa")


class AnalysisData(BaseModel):
    """
    Dados da analise completa conforme payload-spec.md.

    Retornado quando isFinished=True.
    """
    problem: str = Field(..., description="Descricao do problema")
    rights: Optional[List[str]] = Field(default_factory=list, description="Direitos identificados")
    estimatedValue: float = Field(..., description="Valor estimado (OBRIGATORIO)")
    legal_category: Optional[str] = Field("Geral", description="Categoria juridica do caso")
    recommendations: List[Recommendation] = Field(..., description="Recomendacoes de solucao")
    userInfo: UserInfo = Field(..., description="Dados do usuario")
    opposingParty: OpposingParty = Field(..., description="Parte contraria")
    caseDetails: CaseDetails = Field(..., description="Detalhes do caso")

    class Config:
        json_schema_extra = {
            "example": {
                "problem": "Cliente comprou celular com defeito e loja recusa troca",
                "rights": [
                    "Direito a troca em ate 30 dias (CDC Art. 18)",
                    "Direito a informacao clara"
                ],
                "estimatedValue": 2500.00,
                "recommendations": [
                    {"type": "amigavel", "score": 8.5, "reason": "Valor baixo, empresa costuma resolver"},
                    {"type": "extrajudicial", "score": 6.0, "reason": "Se nao resolver amigavelmente"},
                    {"type": "judicial", "score": 3.0, "reason": "Ultimo recurso"}
                ],
                "userInfo": {
                    "name": "Joao da Silva",
                    "email": "joao@email.com",
                    "phone": "(11) 99999-9999"
                },
                "opposingParty": {
                    "type": "pj",
                    "name": "Loja XYZ",
                    "document": "12345678000199"
                },
                "caseDetails": {
                    "title": "Troca de celular com defeito",
                    "description": "Comprei celular em 15/11 e veio com defeito"
                }
            }
        }


# ===== ESTADO DA COLETA =====

class ExternalChatCollectionState(BaseModel):
    """Estado da coleta de dados no external chat."""
    session_id: str
    case_evidence: List[Dict[str, Any]] = Field(default_factory=list)
    evidence_storage_error: bool = False
    evidence_notice: str = ""
    chat_id: Optional[str] = None  # chat_id enviado pelo backend Zellu (usar nas respostas)

    # Dados coletados (ordem obrigatória: email -> first_name -> last_name -> phone)
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    client_name: Optional[str] = None  # Composto de first_name + last_name
    client_email: Optional[str] = None
    client_phone: Optional[str] = None
    client_cpf: Optional[str] = None

    opposing_party_name: Optional[str] = None
    opposing_party_cnpj: Optional[str] = None

    problem_description: Optional[str] = None
    estimated_value: Optional[float] = None

    # Validacao de email
    email_validated: bool = False  # Se o email ja foi validado no backend
    email_exists: bool = False  # Se o email ja existe na base (usuario ja cadastrado)
    existing_user_id: Optional[str] = None  # ID do usuario se ja existir

    # Controle de fluxo
    current_step: str = "greeting"  # greeting, problem, name, email, phone, cnpj, value, confirm
    messages_history: List[Dict[str, str]] = Field(default_factory=list)
    is_finished: bool = False

    # Metadados
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def get_missing_fields(self) -> List[str]:
        """
        Retorna lista de campos obrigatorios faltando.

        ORDEM OBRIGATÓRIA:
        1. client_email (email)
        2. first_name (primeiro nome)
        3. last_name (sobrenome)
        4. client_phone (telefone)
        5. opposing_party_name (nome da empresa)
        6. opposing_party_cnpj (CNPJ)
        7. problem_description (descrição)
        8. estimated_value (valor)
        """
        missing = []
        # Ordem obrigatória: email -> first_name -> last_name -> phone -> empresa -> cnpj -> problema -> valor
        if not self.client_email:
            missing.append("client_email")
        if not self.first_name:
            missing.append("first_name")
        if not self.last_name:
            missing.append("last_name")
        if not self.client_phone:
            missing.append("client_phone")
        if not self.opposing_party_name:
            missing.append("opposing_party_name")
        if not self.opposing_party_cnpj:
            missing.append("opposing_party_cnpj")
        if not self.problem_description:
            missing.append("problem_description")
        if not self.estimated_value:
            missing.append("estimated_value")
        return missing

    def is_collection_complete(self) -> bool:
        """Verifica se todos os campos obrigatorios foram coletados."""
        return len(self.get_missing_fields()) == 0

    def to_analysis_data(self) -> Dict[str, Any]:
        """Converte estado para formato analysisData."""
        # Gerar rights baseado no problema (obrigatorio pelo backend)
        rights = self._generate_rights()

        # Garantir que rights nunca seja vazio (exigido pelo backend)
        if not rights:
            rights = [
                "Direito a protecao e defesa do consumidor (Art. 6 CDC)",
                "Direito a reparacao de danos patrimoniais e morais (Art. 6 CDC)"
            ]

        # Garantir que estimatedValue seja > 0 (exigido pelo backend)
        estimated_value = self.estimated_value or 0
        if estimated_value <= 0:
            # Usar valor default mínimo se não foi coletado
            estimated_value = 100.0

        return {
            "problem": self.problem_description or "Problema a ser detalhado",
            "rights": rights,
            "estimatedValue": estimated_value,
            "legal_category": self._classify_legal_category(),
            "recommendations": self._score_recommendations(estimated_value),
            "userInfo": {
                "first_name": self.first_name or "",
                "last_name": self.last_name or "",
                "name": self.client_name or f"{self.first_name or ''} {self.last_name or ''}".strip(),
                "email": self.client_email or "",
                "phone": self.client_phone or ""
            },
            "opposingParty": {
                "type": "pj",
                "name": self.opposing_party_name or "",
                "document": self.opposing_party_cnpj or ""
            },
            "caseDetails": {
                "title": self._generate_title(),
                "description": self.problem_description or ""
            }
        }

    def _score_recommendations(self, estimated_value: float) -> List[Dict[str, Any]]:
        """Calcula scores dinamicos usando o RecommendationScorer."""
        from src.services.recommendation_scorer import score_recommendations
        scoring_state = {
            "legal_category": self._classify_legal_category(),
            "estimated_value": estimated_value,
            "problem_description": self.problem_description or "",
            "opposing_party_name": self.opposing_party_name or "",
        }
        return score_recommendations(scoring_state)

    def _generate_rights(self) -> List[str]:
        """Gera lista de direitos baseado no problema descrito."""
        rights = []
        problem = (self.problem_description or "").lower()

        # Detectar palavras-chave e adicionar direitos relevantes
        if any(word in problem for word in ["produto", "compra", "comprei", "defeito", "quebrado", "danificado"]):
            rights.append("Direito a troca ou devolucao em ate 7 dias (Art. 49 CDC)")
            rights.append("Direito a produto sem vicio ou defeito (Art. 18 CDC)")

        if any(word in problem for word in ["entrega", "nao chegou", "atrasou", "atraso", "prazo"]):
            rights.append("Direito ao cumprimento do prazo de entrega (Art. 35 CDC)")

        if any(word in problem for word in ["cobranca", "cobrado", "valor", "preco", "errado"]):
            rights.append("Direito a informacao clara sobre precos (Art. 6 CDC)")
            rights.append("Direito a devolucao em dobro de valores cobrados indevidamente (Art. 42 CDC)")

        if any(word in problem for word in ["cancelamento", "cancelar", "desistir", "desisti"]):
            rights.append("Direito de arrependimento em 7 dias para compras online (Art. 49 CDC)")

        if any(word in problem for word in ["garantia", "conserto", "reparo"]):
            rights.append("Direito a garantia legal de 90 dias (Art. 26 CDC)")

        if any(word in problem for word in ["propaganda", "anuncio", "enganosa", "falsa"]):
            rights.append("Direito a protecao contra publicidade enganosa (Art. 37 CDC)")

        if any(word in problem for word in ["servico", "prestacao", "atendimento"]):
            rights.append("Direito a servico adequado e eficiente (Art. 20 CDC)")

        # Discriminacao e Racismo
        if any(word in problem for word in ["racismo", "racista", "discriminacao", "preconceito", "cor da pele", "raca", "injuria racial", "xenofobia", "homofobia", "intolerancia religiosa"]):
            rights.append("Crime de racismo (Lei 7.716/89)")
            rights.append("Injuria racial (Art. 140 par. 3 CP)")
            rights.append("Indenizacao por danos morais (CF Art. 5, V e X)")

        # Assedio
        if any(word in problem for word in ["assedio", "assedio moral", "assedio sexual", "perseguicao", "intimidacao", "stalking"]):
            rights.append("Protecao contra assedio (CLT/CP)")
            rights.append("Indenizacao por danos morais")

        # Violencia e Agressao
        if any(word in problem for word in ["violencia", "agressao", "agredido", "agrediu", "bateu", "espancou", "lesao corporal", "ameaca de morte"]):
            rights.append("Protecao da integridade fisica (CF Art. 5)")
            rights.append("Indenizacao por danos morais e materiais")
            rights.append("Boletim de Ocorrencia")

        # Crimes gerais
        if any(word in problem for word in ["crime", "golpe", "fraude", "estelionato", "roubo", "furto"]):
            rights.append("Protecao contra praticas criminosas")
            rights.append("Reparacao de danos (Art. 927 CC)")
            rights.append("Boletim de Ocorrencia")

        # Se nenhum direito especifico foi detectado, adicionar direito generico
        if not rights:
            rights.append("Direito a protecao e defesa do consumidor (Art. 6 CDC)")
            rights.append("Direito a reparacao de danos patrimoniais e morais (Art. 6 CDC)")

        return rights

    def _classify_legal_category(self) -> str:
        """Classifica categoria juridica baseado no problema descrito.

        Usado como fallback no external chat (sem ClassifierAgent).
        """
        problem = (self.problem_description or "").lower()

        # Direito do Consumidor
        if any(word in problem for word in [
            "produto", "compra", "comprei", "defeito", "loja", "servico",
            "cobranca", "cobrado", "cancelamento", "cancelar", "entrega",
            "garantia", "propaganda", "enganosa", "negativacao", "nome sujo"
        ]):
            return "Direito do Consumidor"

        # Direito Trabalhista
        if any(word in problem for word in [
            "trabalho", "salario", "demissao", "demitido", "clt",
            "carteira assinada", "chefe", "hora extra", "rescisao",
            "fgts", "assedio", "empregador"
        ]):
            return "Direito Trabalhista"

        # Direito Imobiliario
        if any(word in problem for word in [
            "aluguel", "inquilino", "despejo", "imovel", "construtora",
            "condominio", "sindico", "vizinho", "barulho", "goteira"
        ]):
            return "Direito Imobiliário"

        # Direito de Familia
        if any(word in problem for word in [
            "divorcio", "pensao alimenticia", "guarda", "separacao",
            "partilha", "filho", "conjugal", "casamento"
        ]):
            return "Direito de Família"

        # Direito Previdenciario
        if any(word in problem for word in [
            "inss", "aposentadoria", "auxilio doenca", "beneficio",
            "pensao por morte", "previdencia", "contribuicao"
        ]):
            return "Direito Previdenciário"

        # Direito Tributario
        if any(word in problem for word in [
            "imposto", "iptu", "receita federal", "multa fiscal",
            "tributo", "taxa", "icms", "parcelamento"
        ]):
            return "Direito Tributário"

        # Direito Empresarial
        if any(word in problem for word in [
            "sociedade", "socio", "empresa contra empresa",
            "contrato comercial", "recuperacao judicial", "falencia"
        ]):
            return "Direito Empresarial"

        # Direito Penal
        if any(word in problem for word in [
            "racismo", "racista", "discriminacao", "preconceito", "crime",
            "agressao", "agredido", "violencia", "ameaca",
            "lesao corporal", "injuria racial", "furto", "roubo",
            "estelionato", "assedio sexual", "stalking"
        ]):
            return "Direito Penal"

        # Direito Civil
        if any(word in problem for word in [
            "contrato", "acidente", "dano", "colisao",
            "indenizacao", "responsabilidade"
        ]):
            return "Direito Civil"

        return "Geral"

    def _generate_title(self) -> str:
        """Gera titulo baseado no problema."""
        if self.problem_description:
            # Pegar primeiras 50 chars
            return self.problem_description[:50] + ("..." if len(self.problem_description) > 50 else "")
        return "Nova reclamacao"
