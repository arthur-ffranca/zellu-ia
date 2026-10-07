from typing import TypedDict, Optional, Dict, List, Any
from datetime import datetime
import copy

class ConversationState(TypedDict, total=False):
    """Estado da conversa com todos os campos marcados como opcionais para compatibilidade com native runtime.

    Campos de identificação:
        id: UUID da conversa
        chat_id: UUID da sessão de chat
        user_id: UUID do usuário

    Campos de conteúdo:
        messages: Lista de mensagens trocadas
        message_type: Tipo da última mensagem ('text', 'audio', 'file', 'mixed')
        body_message: Texto da última mensagem

    Campos de dados coletados (intake agent):
        first_name: Primeiro nome do cliente (visitantes)
        last_name: Sobrenome do cliente (visitantes)
        client_name: Nome completo do cliente (composto de first_name + last_name para visitantes)
        client_cpf: DEPRECATED - Não mais coletado. Mantido por compatibilidade
        problem_type: DEPRECATED - Mantido por compatibilidade. Use legal_category
        problem_description: Descrição do problema

    Campos de validação de email (visitantes):
        email_validated: Se o email foi validado no backend
        email_exists: Se o email já existe na base (usuário já cadastrado)
        email_invalid: Se o email foi rejeitado pela validação externa (400)
        email_validation_error: Mensagem de erro da validação de email

    Campos de pré-registro (visitantes) - POST /api/ai/create-pre-registered-user:
        pre_registration_completed: Se o pré-registro já foi criado (ou dispensado)
        pre_registered_user_id: ID do usuário pré-registrado
        phone_invalid_attempts: Quantas vezes o telefone voltou 400 (emenda 04/09)
        phone_in_use_attempts: Quantas vezes o telefone voltou 409 phoneInUse
        pre_registration_skipped_reason: Por que desistimos do pré-registro

    Campos de classificação:
        legal_category: Categoria jurídica identificada
        urgency: Urgência do caso
        client_rights: Direitos do cliente identificados
        potential_gain: Ganho potencial estimado

    Campos de análise (analyst_agent):
        relevant_documents: Documentos RAG relevantes
        suggested_next_steps: Próximos passos sugeridos
        required_documents: Documentos necessários

    Campos de controle de fluxo:
        current_agent: Agente atual processando
        step: Passo atual do fluxo
        completed: Se o fluxo foi completado
        validated: Se os dados foram validados
        ready_for_classification: Se está pronto para classificação

    Campos de controle de validação (loop intake <-> classifier):
        description_complete: Se descrição foi aprovada pelo classifier
        missing_context: Feedback do classifier sobre o que falta
        follow_up_attempts: Contador de tentativas (max 3)
        previous_descriptions: Histórico de descrições parciais

    Campos de anexos:
        audio: URLs de áudios anexados (ou None)
        files: URLs de arquivos anexados

    Campos de documentos gerados (writer_agent):
        generated_documents: Documentos gerados (HTML/PDF)
        document_generation_date: Data de geração dos documentos
        document_generation_error: Erro na geração (se houver)
        minio_urls: URLs dos documentos no MinIO

    Campos de continuacao (Continuar Conversando):
        continuation_mode: Se esta em modo continuacao (is_finished=false recebido)
        continuation_rounds: Rodada atual da continuacao
        max_continuation_rounds: Limite de rodadas na continuacao (15)

    Campos de negociação (sender_agent):
        negotiation_round: Rodada atual da negociação
        negotiation_history: Histórico de propostas e respostas
        current_proposal: Proposta atual em negociação
        last_company_response: Última resposta da empresa
        last_company_response_date: Data da última resposta da empresa
        response_analysis: Análise da última resposta
        negotiation_completed: Se negociação foi finalizada
        negotiation_result: Resultado (agreement/no_agreement)
        agreement_details: Detalhes do acordo se houver
        negotiation_start_date: Data de início da negociação
        last_sender_message_date: Data da última mensagem enviada
        last_sender_message: Última mensagem enviada
        escalated_to_external: Se foi escalado para canais externos
        escalation_date: Data da escalação
        escalation_status: Status da escalação
        awaiting_client_decision: Se aguarda decisão do cliente
        auto_closed: Se foi encerrado automaticamente
        auto_close_reason: Razão do encerramento automático
    """

    id: str
    chat_id: str
    user_id: str

    # Observabilidade de custos de IA: eventId que correlaciona todas as
    # chamadas de LLM de um mesmo fluxo de negocio (ver ai_usage_tracker).
    event_id: Optional[str]

    messages: List[Dict[str, str]]
    message_type: str
    body_message: str

    # Campos de identificação do cliente
    first_name: Optional[str]
    last_name: Optional[str]
    client_name: Optional[str]
    client_cpf: Optional[str]
    client_email: Optional[str]
    client_phone: Optional[str]

    # Validação de email (visitantes)
    email_validated: bool
    email_exists: bool
    email_invalid: bool
    email_validation_error: Optional[str]

    # Pré-registro (visitantes) - ver user-exists-webhook-spec.md
    pre_registration_completed: bool
    pre_registered_user_id: Optional[str]
    phone_invalid_attempts: int
    phone_in_use_attempts: int
    pre_registration_skipped_reason: Optional[str]

    problem_type: Optional[str]
    problem_description: Optional[str]
    problem_description_original: Optional[str]
    staged_intake: Optional[Dict[str, Any]]
    case_summary: Optional[str]
    case_confirmed: bool
    legal_decision: Optional[Dict[str, Any]]
    intake_temporal_context: Optional[str]
    intake_reference_timestamp: Optional[str]
    intake_last_input_key: Optional[str]
    case_date_mentions: List[Dict[str, Any]]
    _company_to_persist: Optional[Dict[str, Any]]
    company_lookup_source: Optional[str]
    company_selection_saved_to_mongo: bool

    # Dados da parte contrária (empresa reclamada)
    opposing_party_name: Optional[str]
    opposing_party_trade_name: Optional[str]  # Nome fantasia
    opposing_party_razao_social: Optional[str]  # Razão social
    opposing_party_cnpj: Optional[str]
    opposing_party_website: Optional[str]  # Site da empresa (se encontrado)
    opposing_party_email: Optional[str]  # Canal de contato p/ os documentos (achados 4.3/5.1)
    opposing_party_found_in_db: bool  # True se empresa foi encontrada na base Zellu

    legal_category: Optional[str]
    urgency: Optional[str]
    client_rights: List[str]
    potential_gain: Optional[float]

    relevant_documents: List[Dict[str, Any]]
    suggested_next_steps: List[str]
    required_documents: List[str]

    current_agent: str
    step: int
    completed: bool
    validated: bool
    ready_for_classification: bool

    description_complete: bool
    missing_context: Optional[str]
    follow_up_attempts: int
    previous_descriptions: List[str]

    case_evidence: List[Dict[str, Any]]
    evidence_ledger: Dict[str, Any]
    evidence_conflict_pending: bool
    client_details_confirmed: bool
    documents_requested: bool
    selected_company_record: Dict[str, Any]
    evidence_storage_error: bool
    audio: Optional[List[str]]
    files: List[str]

    generated_documents: Optional[Dict[str, Any]]
    document_generation_date: Optional[str]
    document_generation_error: Optional[str]
    minio_urls: Optional[Dict[str, Any]]

    # Negotiation fields
    negotiation_round: int
    negotiation_history: List[Dict[str, Any]]
    current_proposal: Optional[Dict[str, Any]]
    last_company_response: Optional[Dict[str, Any]]
    last_company_response_date: Optional[str]
    response_analysis: Optional[Dict[str, Any]]
    negotiation_completed: bool
    negotiation_result: Optional[str]
    agreement_details: Optional[Dict[str, Any]]

    # Deadline tracking fields
    negotiation_start_date: Optional[str]
    last_sender_message_date: Optional[str]
    last_sender_message: Optional[str]

    # Escalation fields
    escalated_to_external: bool
    escalation_date: Optional[str]
    escalation_status: Optional[str]
    awaiting_client_decision: bool

    # Auto-close fields
    auto_closed: bool
    auto_close_reason: Optional[str]

    # Continuation fields (Continuar Conversando)
    continuation_mode: bool
    continuation_rounds: int
    max_continuation_rounds: int

    # API error tracking (circuit breaker para evitar loop quando OpenAI está fora)
    api_error_count: int  # Contador de erros consecutivos de API (429, quota, etc)
    pending_reprocess_message: Optional[str]  # Mensagem do usuário que não foi processada por erro de API

    # Dynamic form fields
    pending_form: Optional[str]  # Nome do form pendente (ex: "personal_data", "company_select", "cnpj_input")
    pending_form_fields: Optional[List[str]]  # Campos do form pendente
    pending_form_event_type: Optional[Dict[str, Any]]  # event_type a ser enviado na resposta
    pending_form_snapshot: Optional[Dict[str, Any]]  # copia privada para validar o submit apos o envio
    pending_form_misses: int  # respostas seguidas que nao casaram com o form pendente
    incoming_messagedata: Optional[Dict[str, Any]]  # messagedata recebido do request

    # Campos para busca avancada de empresa
    company_search_results: Optional[List[Dict[str, Any]]]  # Resultados da busca avancada
    # Escada de busca da empresa (1: relato; 2: web profunda; 3: referencia do cliente)
    company_search_attempt: int
    company_rejected_cnpjs: List[str]  # candidatos recusados: nunca reoferecidos
    company_search_queries: List[str]  # consultas ja feitas: a busca profunda nao repete
    company_awaiting_reference: bool  # proximo texto do cliente e a referencia pedida
    company_reference_asks: int  # pedidos de referencia ja feitos (frase varia; teto)
    company_reference_hints: List[str]  # referencias que o cliente ja deu
    opposing_party_company_id: Optional[str]  # company_id no Zellu (do create-company)
    opposing_party_situacao_cadastral: Optional[str]  # Situacao cadastral na Receita
    opposing_party_situacao_cadastral_source: Optional[str]  # "mongo" | "unavailable" | None (quem respondeu a consulta)
    # Numero do pedido, do contrato ou do protocolo que o cliente tem COM A
    # EMPRESA. Vai no slot `referenciaInterna` da amigavel e da notificacao
    # (contrato de documentos). Nao e perguntado: so entra quando o cliente
    # menciona por conta propria no relato.
    opposing_party_reference: Optional[str]

    # Confirmacao de empresa
    pending_company_confirm: Optional[Dict[str, Any]]  # Dados da empresa aguardando confirmacao do usuario
    company_confirmed: bool  # Se o usuario confirmou a empresa encontrada
    original_opposing_party_name: Optional[str]  # Nome original informado pelo usuario (para cross-validacao)
    contact_search_pending: bool  # Sinaliza main.py para disparar busca de contatos em background

    # Filial/Matriz detection
    opposing_party_is_filial: bool  # True se CNPJ e filial (ordem != 0001)
    opposing_party_filial_cnpj: Optional[str]  # CNPJ original da filial informada pelo usuario
    opposing_party_matriz_cnpj: Optional[str]  # CNPJ da matriz identificada
    company_parent_lookup_pending: bool  # filial escolhida, matriz ainda nao localizada
    company_parent_lookup_tries: int
    opposing_party_matriz_name: Optional[str]  # Nome fantasia/razao social da matriz

    # Fluxo de CNPJ e busca de empresa
    cnpj_knows: Optional[bool]  # Se usuario sabe o CNPJ (form cnpj_knows)
    cnpj_retry_choice: Optional[str]  # Escolha do form cnpj_or_retry (provide_cnpj/retry_search/skip_cnpj)
    company_segment: Optional[str]  # Segmento/ramo da empresa (form company_refine)
    company_location_input: Optional[str]  # Localizacao informada pelo usuario (form company_refine)
    firecrawl_search_step: Optional[str]  # None | "waiting_address" | "waiting_segment" - fluxo conversacional Firecrawl

    # Localizacao do problema (para desambiguacao e enriquecimento da descricao)
    problem_location_uf: Optional[str]  # Estado onde ocorreu o problema (ex: "SP")
    problem_location_city: Optional[str]  # Cidade onde ocorreu (ex: "Sao Paulo")
    problem_is_online: Optional[bool]  # True se foi compra/servico online

