# -*- coding: utf-8 -*-
"""
Schemas para integração do Agente Sender via Webhook.

Define os modelos Pydantic para:
- Receber requisições do Backend Zellu (start negotiation, company response)
- Enviar respostas de status e mensagens
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from enum import Enum

from config import get_settings


# ===== ENUMS =====

class SolutionType(str, Enum):
    """Tipo de solução escolhida pelo cliente."""
    AMIGAVEL = "amigavel"
    EXTRAJUDICIAL = "extrajudicial"


class NegotiationStatus(str, Enum):
    """Status da negociação."""
    STARTED = "started"
    NEGOTIATING = "negotiating"
    AGREEMENT = "agreement"
    NO_AGREEMENT = "no_agreement"
    TIMEOUT = "timeout"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


class NegotiationEventType(str, Enum):
    """Tipos de eventos de negociação."""
    ROUND_COMPLETED = "round_completed"
    NEGOTIATION_COMPLETED = "negotiation_completed"
    DEADLINE_EXPIRED = "deadline_expired"
    MESSAGE_SENT = "message_sent"


# ===== MODELOS DE ENTRADA (Backend Zellu → Sender) =====

class TicketData(BaseModel):
    """Dados do ticket."""
    id: str = Field(..., description="ID único do ticket")
    number: Optional[str] = Field(None, description="Número do ticket (ex: 2024-001234)")
    title: Optional[str] = Field(None, description="Título do ticket")
    description: str = Field(..., description="Descrição completa do problema")
    category: Optional[str] = Field(None, description="Categoria do problema")
    priority: Optional[str] = Field("media", description="Prioridade (baixa, media, alta)")
    status: Optional[str] = Field("aguardando_negociacao", description="Status atual")
    estimated_value: Optional[float] = Field(None, description="Valor estimado do dano")
    solution_type: SolutionType = Field(..., description="Tipo de solução: amigavel ou extrajudicial")
    created_at: Optional[datetime] = Field(None, description="Data de criação do ticket")
    closed_at: Optional[datetime] = Field(None, description="Data de fechamento do ticket")
    # Direitos que o CLIENTE confirmou na criacao do chamado (contrato 20260709).
    # Contexto de abertura da negociacao (one-way Zellu -> Sender). Sempre presente no
    # payload novo; nunca null. [] = "sem contexto de direitos" (ticket legado/externo),
    # NUNCA tratar como erro. Nome igual a chave JSON (selectedRights) para ligar direto.
    selectedRights: List[str] = Field(
        default_factory=list,
        description="Direitos confirmados pelo cliente (>=1 quando presente; [] = sem contexto)",
    )


class ClientData(BaseModel):
    """Dados do cliente."""
    id: str = Field(..., description="ID único do cliente")
    name: str = Field(..., description="Nome completo do cliente")
    email: Optional[str] = Field(None, description="Email do cliente")
    cpf: Optional[str] = Field(None, description="CPF do cliente")
    phone: Optional[str] = Field(None, description="Telefone do cliente")


class CompanyData(BaseModel):
    """Dados da empresa."""
    id: str = Field(..., description="ID único da empresa")
    name: Optional[str] = Field(None, description="Nome da empresa")
    trade_name: Optional[str] = Field(None, description="Nome fantasia")
    cnpj: Optional[str] = Field(None, description="CNPJ da empresa")
    segment: Optional[str] = Field(None, description="Segmento de atuação")
    ai_enabled: bool = Field(True, description="Se a empresa tem IA habilitada")
    ai_webhook_url: Optional[str] = Field(None, description="URL do webhook da IA da empresa")


class PotentialGain(BaseModel):
    """Ganho potencial estimado."""
    min: Optional[float] = Field(None, description="Valor mínimo estimado")
    max: Optional[float] = Field(None, description="Valor máximo estimado")


class AnalysisData(BaseModel):
    """Dados da análise jurídica."""
    legal_category: Optional[str] = Field(None, description="Categoria jurídica identificada")
    client_rights: Optional[List[str]] = Field(default_factory=list, description="Direitos do cliente")
    suggested_resolution: Optional[str] = Field(None, description="Resolução sugerida")
    urgency: Optional[str] = Field("media", description="Urgência do caso")
    potential_gain: Optional[PotentialGain] = Field(None, description="Ganho potencial")


class DocumentRef(BaseModel):
    """Referência a um documento."""
    type: str = Field(..., description="Tipo do documento")
    url: str = Field(..., description="URL do documento")


class DocumentsData(BaseModel):
    """Documentos relacionados ao caso."""
    generated: Optional[List[DocumentRef]] = Field(default_factory=list, description="Documentos gerados")
    provided_by_client: Optional[List[DocumentRef]] = Field(default_factory=list, description="Documentos do cliente")


class NegotiationConfig(BaseModel):
    """Configurações da negociação."""
    # Default vem de config.NEGOTIATION_MAX_ROUNDS (fonte unica, achado Z-09).
    # default_factory adia a leitura para a instanciacao: importar este schema
    # nao passa a exigir as variaveis de ambiente do Settings.
    max_negotiation_rounds: int = Field(
        default_factory=lambda: get_settings().NEGOTIATION_MAX_ROUNDS,
        description="Máximo de rodadas de negociação"
    )
    initial_deadline_hours: int = Field(168, description="Prazo inicial em horas (7 dias)")
    escalation_deadline_hours: int = Field(168, description="Prazo de escalação em horas")
    auto_close_days: int = Field(50, description="Dias para fechamento automático")
    callback_url: str = Field(..., description="URL para enviar atualizações ao Backend Zellu")


class StartNegotiationRequest(BaseModel):
    """
    Request para iniciar negociação.

    Recebido do Backend Zellu quando um ticket é finalizado
    e o cliente escolhe uma solução (amigável ou extrajudicial).
    """
    ticket: TicketData = Field(..., description="Dados do ticket")
    client: ClientData = Field(..., description="Dados do cliente")
    company: CompanyData = Field(..., description="Dados da empresa")
    analysis: Optional[AnalysisData] = Field(None, description="Análise jurídica")
    documents: Optional[DocumentsData] = Field(None, description="Documentos do caso")
    config: NegotiationConfig = Field(..., description="Configurações da negociação")
    # sessionId do Zellu: chave canonica de idempotencia (contrato 20260626 §3.1/§4).
    # Opcional p/ retrocompat; se ausente, derivamos do callback_url/ticket.
    # Aceita "sessionId" (alias) e "session_id" (populate_by_name).
    session_id: Optional[str] = Field(
        None, alias="sessionId", description="ID da sessao no Zellu (chave canonica)"
    )

    class Config:
        populate_by_name = True
        json_schema_extra = {
            "example": {
                "ticket": {
                    "id": "ticket_abc123",
                    "number": "2024-001234",
                    "title": "Produto com defeito",
                    "description": "Comprei um fone que parou de funcionar após 2 dias",
                    "solution_type": "amigavel",
                    "estimated_value": 150.00
                },
                "client": {
                    "id": "client_xyz789",
                    "name": "João da Silva",
                    "email": "joao@email.com"
                },
                "company": {
                    "id": "company_456",
                    "name": "Loja TechBR",
                    "ai_enabled": True
                },
                "config": {
                    "callback_url": "https://api.zellu.com.br/webhooks/sender"
                }
            }
        }


class CompanyResponseMessage(BaseModel):
    """Mensagem de resposta da empresa."""
    id: Optional[str] = Field(None, description="ID da mensagem")
    type: str = Field("text", description="Tipo da mensagem")
    content: str = Field(..., description="Conteúdo da mensagem")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Metadados adicionais")


class CompanyResponseRequest(BaseModel):
    """
    Request para enviar resposta da empresa.

    Recebido do Backend Zellu quando a IA da empresa responde.
    """
    negotiation_id: str = Field(..., description="ID da negociação")
    ticket_id: str = Field(..., description="ID do ticket")
    company_id: str = Field(..., description="ID da empresa")
    response: CompanyResponseMessage = Field(..., description="Mensagem de resposta")
    timestamp: Optional[datetime] = Field(default_factory=datetime.now, description="Timestamp da resposta")

    class Config:
        json_schema_extra = {
            "example": {
                "negotiation_id": "neg_20240115_abc123",
                "ticket_id": "ticket_abc123",
                "company_id": "company_456",
                "response": {
                    "id": "resp_001",
                    "type": "text",
                    "content": "Prezado cliente, podemos oferecer reembolso de 80%..."
                }
            }
        }


# ===== MODELOS DE SAÍDA (Sender → Backend Zellu) =====

class StartNegotiationResponse(BaseModel):
    """Resposta ao iniciar negociação."""
    success: bool = Field(..., description="Se a operação foi bem-sucedida")
    negotiation_id: Optional[str] = Field(None, description="ID da negociação criada")
    status: Optional[NegotiationStatus] = Field(None, description="Status inicial")
    message: Optional[str] = Field(None, description="Mensagem de status")
    estimated_first_response: Optional[datetime] = Field(None, description="Estimativa de primeira resposta")
    error: Optional[Dict[str, Any]] = Field(None, description="Detalhes do erro (se houver)")


class CompanyResponseAck(BaseModel):
    """Acknowledgement de resposta da empresa recebida."""
    success: bool = Field(..., description="Se a resposta foi processada")
    message: Optional[str] = Field(None, description="Mensagem de confirmação")
    next_action: Optional[str] = Field(None, description="Próxima ação (analyzing, counter_proposal, etc)")


class NegotiationHistoryEntry(BaseModel):
    """Entrada no histórico de negociação."""
    round: int = Field(..., description="Número da rodada")
    sender: str = Field(..., description="Quem enviou (zelinhu ou company)")
    summary: str = Field(..., description="Resumo da mensagem")
    timestamp: datetime = Field(..., description="Data/hora da mensagem")


class NegotiationDeadline(BaseModel):
    """Prazo de negociação."""
    deadline: datetime = Field(..., description="Data/hora do prazo")
    status: str = Field(..., description="Status do prazo (pending, expired)")


class NegotiationStatusResponse(BaseModel):
    """Resposta de status da negociação."""
    negotiation_id: str = Field(..., description="ID da negociação")
    ticket_id: str = Field(..., description="ID do ticket")
    status: NegotiationStatus = Field(..., description="Status atual")
    current_round: int = Field(0, description="Rodada atual")
    created_at: datetime = Field(..., description="Data de criação")
    last_activity_at: Optional[datetime] = Field(None, description="Última atividade")
    deadlines: Optional[Dict[str, NegotiationDeadline]] = Field(None, description="Prazos")
    history: Optional[List[NegotiationHistoryEntry]] = Field(default_factory=list, description="Histórico")


class ErrorDetail(BaseModel):
    """Detalhes de erro."""
    code: str = Field(..., description="Código do erro")
    message: str = Field(..., description="Mensagem do erro")
    details: Optional[Dict[str, Any]] = Field(None, description="Detalhes adicionais")
    recommendation: Optional[str] = Field(None, description="Recomendação")


class ErrorResponse(BaseModel):
    """Resposta de erro."""
    success: bool = Field(False)
    error: ErrorDetail = Field(..., description="Detalhes do erro")


# ===== SCHEMAS PARA WEBHOOKS /api/webhooks/sender/* =====
# Conforme documentacao: automatic-negotiation-refactor/webhooks-sender.md

class WebhookSenderMessageRequest(BaseModel):
    """
    Request para POST /api/webhooks/sender/message.

    Webhook chamado pelo Zellu Backend para enviar a RESPOSTA DA IA EMPRESA
    para o ZelinhU processar e gerar a proxima mensagem da negociacao.

    Fluxo:
    1. Backend recebe msg do ZelinhU
    2. Backend envia para IA Empresa
    3. IA Empresa responde
    4. Backend chama ESTE WEBHOOK com a resposta da empresa
    5. ZelinhU analisa e retorna proxima mensagem
    """
    negotiationId: Optional[str] = Field(None, description="ID da negociacao no Sender (opcional na 1a msg)")
    sessionId: str = Field(..., description="ID da sessao no Zellu (OBRIGATORIO)")
    # Obrigatorio para mensagem de negociacao, mas NAO para toda action: o aviso
    # `company_ai_failed` (plano de 25/08/2026 §2.3) nao tem corpo de mensagem -
    # o turno da empresa falhou, nao ha fala nenhuma. Com o campo obrigatorio no
    # schema, esse aviso morreria em 422 antes de chegar ao handler. A exigencia
    # continua valendo em runtime para o caminho normal (400 messageBody is
    # required em _process_sender_message).
    messageBody: Optional[str] = Field(None, description="Resposta da IA da empresa (obrigatorio, exceto em actions sem mensagem)")
    # `round` ausente NAO e rodada 1 (auditoria QA-07, 24/08/2026). O default
    # antigo era 1, entao toda entrega sem o campo - o resume do backend, por
    # exemplo - se apresentava como rodada 1. O backend grava o round que
    # mandamos (`currentRound: round || ...`), e uma negociacao na rodada 8
    # retrocedia para 1 no banco dele. Com None, quem decide e o estado da
    # sessao.
    round: Optional[int] = Field(None, description="Rodada atual da negociacao (ausente = usar a rodada da sessao)")
    # Campos opcionais para contexto (evita lookup no backend)
    ticketId: Optional[str] = Field(None, description="ID do ticket")
    companyId: Optional[str] = Field(None, description="ID da empresa")
    negotiationComplete: Optional[bool] = Field(False, description="Se a IA empresa marcou como concluido")
    manualModeActive: Optional[bool] = Field(False, description="Se modo manual esta ativo (humano assumiu controle)")
    # Origem da primeira resposta da empresa no start_negotiation. Campo
    # opcional para auditoria/observabilidade: nao altera o fluxo e nao bloqueia
    # payload legado. Mantemos os valores fechados para nao virar taxonomia livre
    # nos logs e relatorios.
    firstReplyOrigin: Optional[Literal["company_ai", "company_manual"]] = Field(
        None,
        description="Origem da primeira fala da empresa: company_ai ou company_manual",
    )

    # ROTEAMENTO POR ACTION (contrato confirmado com o backend em 24/08/2026)
    # ------------------------------------------------------------------
    # O backend nao tem uma URL por operacao: todas as chamadas vao para a
    # SENDER_WEBHOOK_URL e quem distingue e o campo `action`. Sem ele no schema,
    # a retomada apos a recusa do cliente chegava aqui indistinguivel de uma
    # mensagem da empresa - e o guard de espera pela decisao do cliente a
    # bloqueava, congelando a negociacao (QA-01/02/06).
    action: Optional[str] = Field(None, description="Operacao pedida pelo backend, ex: resume_negotiation")
    clientFeedback: Optional[str] = Field(None, description="Motivo da recusa, quando action=resume_negotiation")
    # POR QUE a negociacao esta voltando (spec 20260901, secao 4.1). Ate agora a
    # unica retomada possivel era a recusa do cliente, e o texto que o ZelinhU
    # manda para a empresa dizia isso fixo. Com as solicitacoes, a mesma action
    # volta por tres motivos diferentes - e anunciar recusa numa retomada de
    # "documento entregue" faria a empresa reabrir o que ninguem contestou.
    # Ausente ou desconhecido cai no texto neutro, nunca no de recusa.
    resumeReason: Optional[str] = Field(
        None,
        description=(
            "Motivo da retomada: client_rejected, request_fulfilled, "
            "request_denied ou request_expired"
        ),
    )
    rejectionCount: Optional[int] = Field(None, description="Numero de recusas do cliente nesta negociacao")
    previousAgreement: Optional[Dict[str, Any]] = Field(
        None,
        description="Acordo recusado pelo cliente: value, terms, deadline"
    )

    # CONTEXTO EM TODA RETOMADA (spec 20260901, secao 4.2)
    # ----------------------------------------------------
    # O backend passou a mandar os mesmos blocos do start_negotiation junto da
    # retomada. Ate 01/09 vinham so clientFeedback/rejectionCount/
    # previousAgreement e, quando a sessao nao estava mais em memoria, a unica
    # saida era o endpoint de contexto - o que deu 404 nas tres tentativas de
    # 25/08, com o ZelinhU negociando cinco rodadas sem valor da causa, sem nome
    # do cliente e sem direitos.
    #
    # Dict cru, e NAO os models TicketData/ClientData/CompanyData que existem
    # neste arquivo: eles exigem id, description e solution_type, e um resume sem
    # qualquer um desses campos morreria em 422 antes de chegar ao handler. A
    # retomada e justamente o caminho onde falhar nao degrada a resposta - deixa
    # um cliente real esperando sem prazo. Aqui e melhor aceitar o que vier.
    ticket: Optional[Dict[str, Any]] = Field(
        None, description="Bloco do ticket, quando a retomada o traz"
    )
    client: Optional[Dict[str, Any]] = Field(
        None, description="Bloco do cliente, quando a retomada o traz"
    )
    company: Optional[Dict[str, Any]] = Field(
        None, description="Bloco da empresa, quando a retomada o traz"
    )
    selectedRights: Optional[List[str]] = Field(
        None, description="Direitos confirmados pelo cliente na abertura do chamado"
    )
    # ANEXOS DA SOLICITACAO ATENDIDA - dois campos, um proposito.
    # ----------------------------------------------------------
    # `files` e o que o backend manda de verdade (contrato 20260909): lista crua
    # de URLs presigned, o MESMO nome que ja recebemos no chat. `attachments` era
    # o formato em dict que a spec de 01/09 (§5.4) deixou em aberto - ele nunca
    # chegou a existir do lado deles, mas continua aceito aqui porque le texto ja
    # extraido, que a URL crua nao tem como trazer.
    #
    # Ate 09/09 declaravamos SO `attachments`. Como Pydantic descarta campo
    # desconhecido em silencio, o `files` chegava e evaporava: o ZelinhU pedia um
    # comprovante, recebia o arquivo e seguia negociando vendo so a frase
    # 'Solicitacao "X" atendida. 2 arquivo(s) anexado(s).' - sem nunca ter visto
    # nem o nome do arquivo. Um contrato inteiro perdido por um nome de campo.
    #
    # List[Any] nos dois, e nao List[str]/List[Dict]: a retomada e o caminho onde
    # falhar nao degrada a resposta - deixa um cliente real esperando sem prazo.
    # Melhor aceitar o que vier e marcar como nao lido do que 422 no payload
    # inteiro. Quem sabe ler cada formato e `ingerir_anexos`.
    files: Optional[List[Any]] = Field(
        None, description="URLs presigned dos anexos da solicitacao atendida (contrato 20260909)"
    )
    attachments: Optional[List[Any]] = Field(
        None, description="Anexos em formato de objeto, quando vierem assim"
    )

    # OS PEDIDOS DA RAJADA, UM A UM (contrato 20260911, Etapa 1)
    # ----------------------------------------------------------
    # Ate aqui a retomada trazia `files` - uma lista PLANA com as respostas de
    # ate 6 pedidos - e o ZelinhU comparava esses arquivos com o que ele lembrava
    # ter pedido. Duas consequencias, as duas medidas:
    #
    #   1. dois comprovantes de meses diferentes eram julgados contra o pedido
    #      errado, porque nada dizia qual arquivo respondia a qual pedido;
    #   2. o que foi pedido morava na memoria do processo. Uma solicitacao pode
    #      esperar DIAS (nao ha prazo), e um restart no intervalo apagava o
    #      pedido - o ZelinhU julgava o documento sem lembrar do que pediu.
    #
    # Com `requests` o payload passa a ser a fonte da verdade, com os arquivos
    # de CADA pedido dentro dele. Mesma regra que a secao 4.2 ja aplicava a
    # ticket/client/company: "assumam que nossa memoria pode estar vazia".
    #
    # Dict cru pelo motivo de sempre nesta rota: um 422 aqui nao degrada a
    # resposta - deixa um cliente real esperando sem prazo.
    requests: Optional[List[Dict[str, Any]]] = Field(
        None,
        description=(
            "Pedidos da rajada com status, answer, declineReason, files e "
            "verdict por pedido (contrato 20260911)"
        ),
    )

    # CONFERENCIA DE UM PEDIDO (contrato 20260911, §3.2)
    # ---------------------------------------------------
    # O backend recusou a rota nova que propusemos e pediu o mesmo desenho de
    # sempre: a URL unica, com `action: "verify_request"`. Motivo deles, e e bom:
    # `/api/webhooks/sender/...` e o prefixo dos webhooks que ELES chamam em NOS,
    # e uma rota nova ali deixaria impossivel saber pelo caminho quem chama quem.
    #
    # `request` (singular) tem dois usos controlados:
    #
    # - action=verify_request: e o pedido que esta sendo conferido; `requestId` e
    #   a chave que volta na resposta, e os arquivos vem no `files`;
    # - company_response comum: pedido estruturado feito pela empresa, para o
    #   ZelinhU validar e, se fizer sentido, repassar ao cliente como
    #   action=request. Este caminho e opcional e nao substitui a analise normal.
    requestId: Optional[str] = Field(
        None, description="Pedido a conferir, quando action=verify_request"
    )
    request: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "O que foi pedido (audience, type, responseType, title, description), "
            "quando action=verify_request ou quando a empresa pede mediacao no company_response"
        ),
    )

    # Campos de action=company_ai_failed (plano de 25/08/2026, §2.3)
    error: Optional[str] = Field(None, description="Mensagem do erro no turno da IA da empresa")
    code: Optional[str] = Field(None, description="Codigo do erro, ex: AI_TIMEOUT")
    retryable: Optional[bool] = Field(None, description="Se o backend considera o erro repetivel")
    source: Optional[str] = Field(None, description="Origem do aviso, ex: backend-callback")

    class Config:
        json_schema_extra = {
            "example": {
                "negotiationId": "neg_123abc",
                "sessionId": "sess_xyz789",
                "messageBody": "Prezado cliente, podemos oferecer reembolso integral do valor...",
                "round": 2,
                "negotiationComplete": False,
                "manualModeActive": False
            }
        }


class WebhookSenderMessageResponseData(BaseModel):
    """Dados da resposta do ZelinhU."""
    messageBody: str = Field(..., description="Proxima mensagem do ZelinhU")
    negotiationComplete: bool = Field(False, description="Se ZelinhU considera a negociacao concluida entre as IAs")
    action: Optional[str] = Field("counter_proposal", description="Acao: counter_proposal, send_to_client, escalate")


class WebhookSenderMessageResponse(BaseModel):
    """
    Response para POST /api/webhooks/sender/message.

    Retorna a PROXIMA MENSAGEM DO ZELINHU apos analisar a resposta da empresa.
    O Zellu Backend deve enviar esta mensagem para a IA Empresa continuar o loop.

    Campos top-level (zelinhuMessage, negotiationComplete, status) sao
    parseados diretamente pelo backend para salvar a mensagem no chat.
    """
    success: bool = Field(..., description="Se a operacao foi bem sucedida")
    companyMessageId: str = Field(..., description="ID da mensagem da empresa recebida")
    zelinhuMessageId: str = Field(..., description="ID da nova mensagem do ZelinhU gerada")
    response: WebhookSenderMessageResponseData = Field(..., description="Proxima mensagem do ZelinhU")
    round: int = Field(..., description="Rodada atual")
    # Campos top-level para o backend parsear diretamente
    zelinhuMessage: Optional[str] = Field(None, description="Mensagem do ZelinhU (top-level para backend)")
    negotiationComplete: Optional[bool] = Field(None, description="Se negociacao foi concluida")
    status: Optional[str] = Field(None, description="Status final: agreement, no_agreement, escalated")

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "companyMessageId": "msg_company_001",
                "zelinhuMessageId": "msg_zelinhu_002",
                "response": {
                    "messageBody": "Agradeco a proposta, mas o cliente esperava uma compensacao maior...",
                    "negotiationComplete": False,
                    "action": "counter_proposal"
                },
                "round": 2,
                "zelinhuMessage": None,
                "negotiationComplete": False,
                "status": None
            }
        }


class WebhookSenderResumeRequest(BaseModel):
    """
    Request para retomar negociacao apos recusa do cliente.
    """
    negotiationId: Optional[str] = Field(None, description="ID da negociacao no Sender")
    sessionId: str = Field(..., description="ID da sessao no Zellu")
    clientFeedback: str = Field(..., description="Motivo da recusa informado pelo cliente")
    rejectionCount: Optional[int] = Field(0, description="Numero de recusas nesta negociacao")
    previousAgreement: Optional[Dict[str, Any]] = Field(
        None,
        description="Detalhes do acordo recusado: value, terms, deadline"
    )

    # Contexto na retomada (spec 20260901, secao 4.2). Mesmos campos e mesmo
    # motivo do WebhookSenderMessageRequest acima - esta porta e a direta, usada
    # pelo app e pelos testes; a outra e a que o backend usa. As duas precisam
    # aceitar o contexto, senao a retomada depende de qual porta chegou.
    ticket: Optional[Dict[str, Any]] = Field(
        None, description="Bloco do ticket, quando a retomada o traz"
    )
    client: Optional[Dict[str, Any]] = Field(
        None, description="Bloco do cliente, quando a retomada o traz"
    )
    company: Optional[Dict[str, Any]] = Field(
        None, description="Bloco da empresa, quando a retomada o traz"
    )
    selectedRights: Optional[List[str]] = Field(
        None, description="Direitos confirmados pelo cliente na abertura do chamado"
    )
    # ANEXOS DA SOLICITACAO ATENDIDA - dois campos, um proposito.
    # ----------------------------------------------------------
    # `files` e o que o backend manda de verdade (contrato 20260909): lista crua
    # de URLs presigned, o MESMO nome que ja recebemos no chat. `attachments` era
    # o formato em dict que a spec de 01/09 (§5.4) deixou em aberto - ele nunca
    # chegou a existir do lado deles, mas continua aceito aqui porque le texto ja
    # extraido, que a URL crua nao tem como trazer.
    #
    # Ate 09/09 declaravamos SO `attachments`. Como Pydantic descarta campo
    # desconhecido em silencio, o `files` chegava e evaporava: o ZelinhU pedia um
    # comprovante, recebia o arquivo e seguia negociando vendo so a frase
    # 'Solicitacao "X" atendida. 2 arquivo(s) anexado(s).' - sem nunca ter visto
    # nem o nome do arquivo. Um contrato inteiro perdido por um nome de campo.
    #
    # List[Any] nos dois, e nao List[str]/List[Dict]: a retomada e o caminho onde
    # falhar nao degrada a resposta - deixa um cliente real esperando sem prazo.
    # Melhor aceitar o que vier e marcar como nao lido do que 422 no payload
    # inteiro. Quem sabe ler cada formato e `ingerir_anexos`.
    files: Optional[List[Any]] = Field(
        None, description="URLs presigned dos anexos da solicitacao atendida (contrato 20260909)"
    )
    attachments: Optional[List[Any]] = Field(
        None, description="Anexos em formato de objeto, quando vierem assim"
    )

    # OS PEDIDOS DA RAJADA, UM A UM (contrato 20260911, Etapa 1)
    # ----------------------------------------------------------
    # Ate aqui a retomada trazia `files` - uma lista PLANA com as respostas de
    # ate 6 pedidos - e o ZelinhU comparava esses arquivos com o que ele lembrava
    # ter pedido. Duas consequencias, as duas medidas:
    #
    #   1. dois comprovantes de meses diferentes eram julgados contra o pedido
    #      errado, porque nada dizia qual arquivo respondia a qual pedido;
    #   2. o que foi pedido morava na memoria do processo. Uma solicitacao pode
    #      esperar DIAS (nao ha prazo), e um restart no intervalo apagava o
    #      pedido - o ZelinhU julgava o documento sem lembrar do que pediu.
    #
    # Com `requests` o payload passa a ser a fonte da verdade, com os arquivos
    # de CADA pedido dentro dele. Mesma regra que a secao 4.2 ja aplicava a
    # ticket/client/company: "assumam que nossa memoria pode estar vazia".
    #
    # Dict cru pelo motivo de sempre nesta rota: um 422 aqui nao degrada a
    # resposta - deixa um cliente real esperando sem prazo.
    requests: Optional[List[Dict[str, Any]]] = Field(
        None,
        description=(
            "Pedidos da rajada com status, answer, declineReason, files e "
            "verdict por pedido (contrato 20260911)"
        ),
    )


class WebhookSenderResumeResponse(BaseModel):
    success: bool = Field(..., description="Se a operacao foi bem sucedida")
    message: str = Field(..., description="Mensagem de retorno do Sender")
    round: int = Field(..., description="Rodada que sera usada para retomar a negociacao")
    backendResponse: Optional[Dict[str, Any]] = Field(None, description="Resposta do backend Zellu, se disponivel")


class WebhookSenderStatusRequest(BaseModel):
    """
    Request para POST /api/webhooks/sender/status.

    Webhook chamado para notificar mudancas de status na negociacao.
    """
    negotiationId: Optional[str] = Field(None, description="ID da negociacao no Sender")
    sessionId: str = Field(..., description="ID da sessao no Zellu (OBRIGATORIO)")
    status: str = Field(..., description="Novo status da negociacao")
    finalRound: Optional[int] = Field(None, description="Rodada em que terminou")
    reason: Optional[str] = Field(None, description="Motivo do status")
    agreementDetails: Optional[Dict[str, Any]] = Field(None, description="Detalhes do acordo")
    error: Optional[str] = Field(None, description="Mensagem de erro")

    class Config:
        json_schema_extra = {
            "example": {
                "negotiationId": "neg_123abc",
                "sessionId": "sess_xyz789",
                "status": "agreement",
                "finalRound": 4,
                "agreementDetails": {
                    "value": 500.00,
                    "terms": "Desconto de R$ 500,00 aplicado na proxima fatura",
                    "deadline": "2025-01-31"
                }
            }
        }


class WebhookSenderStatusResponse(BaseModel):
    """
    Response para POST /api/webhooks/sender/status.

    Confirmacao de mudanca de status.
    """
    success: bool = Field(..., description="Se a operacao foi bem sucedida")
    sessionId: str = Field(..., description="ID da sessao")
    previousStatus: Optional[str] = Field(None, description="Status anterior")
    newStatus: str = Field(..., description="Novo status")
    isTerminal: bool = Field(..., description="Se e um status final (encerra negociacao)")

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "sessionId": "sess_xyz789",
                "previousStatus": "negotiating",
                "newStatus": "agreement",
                "isTerminal": True
            }
        }
