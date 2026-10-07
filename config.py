""""Configuração da aplicação."""

from pydantic_settings import BaseSettings
import os
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).parent

class Settings(BaseSettings):
    """Configurações da aplicação."""

    # LLM OpenAI
    OPENAI_API_KEY: str
    OPENAI_MODEL: str = "gpt-5"
    # Modelo "rapido/barato" para tarefas de extracao/classificacao (intake,
    # classifier, external_chat). Redacao/analise (writer, analyst, sender)
    # continuam no OPENAI_MODEL forte. Reversivel via env: setar OPENAI_MODEL_FAST
    # igual ao OPENAI_MODEL restaura o comportamento anterior (contrato 20260629 item 2).
    OPENAI_MODEL_FAST: str = "gpt-4o-mini"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"

    # Modelo e temperatura das DUAS pontas da negociacao IA x IA (ZelinhU e IA
    # da Empresa). Motivacao: auditoria de 22/08/2026 - a IA da Empresa negociava
    # em gpt-4o-mini (fallback de src/main.py e default do backend Zellu) uma
    # tarefa que exige raciocinio adversarial, aritmetica e classificacao item a
    # item. NEGOTIATION_MODEL tem PRECEDENCIA sobre o config.model do payload.
    # Reversivel via env: NEGOTIATION_MODEL="" volta ao comportamento anterior
    # (payload do backend para a Empresa, OPENAI_MODEL para o ZelinhU).
    NEGOTIATION_MODEL: str = "gpt-5"
    NEGOTIATION_TEMPERATURE: float = 0.2

    # Fonte unica do limite de rodadas da negociacao (achado Z-09). Antes havia
    # tres valores em desacordo: 6 no loop do webhook, 10 no SenderAgent e 10 no
    # default do schema - e o gatilho de "oferta final" do prompt so disparava a
    # partir da rodada 7, que nunca chegava. O payload do backend Zellu, quando
    # informa max_negotiation_rounds explicitamente, continua tendo prioridade.
    NEGOTIATION_MAX_ROUNDS: int = 6

    # Rodadas que o ZelinhU ganha depois que o CLIENTE recusa a proposta. A
    # recusa chega quase sempre na rodada 6 ou depois - sem essa folga a primeira
    # resposta da empresa ja caia no teto e ia direto ao cliente, sem nenhuma
    # contraproposta. Cada recusa estende o teto da sessao a partir da rodada em
    # que ela chegou.
    NEGOTIATION_ROUNDS_AFTER_REJECTION: int = 3

    # Recusas do cliente depois das quais a negociacao automatica para. Na
    # recusa de numero N o ZelinhU nao volta a negociar: encerra com
    # no_agreement e o caso fica com o cliente, que decide os proximos passos.
    NEGOTIATION_MAX_CLIENT_REJECTIONS: int = 3

    # Livro-razao da negociacao (Fase 2). Quando ligado, a IA da Empresa faz uma
    # chamada extra por rodada, com saida estruturada, so para atualizar o estado
    # dos itens antes de redigir. E o que impede contradicao entre rodadas.
    # Desligar (false) volta ao turno de uma chamada so.
    # No lado do ZelinhU nao ha custo extra: a atualizacao vem junto da analise
    # que ja existia.
    NEGOTIATION_LEDGER_ENABLED: bool = True

    # Modelo do passo 1 (atualizar o livro-razao). E extracao estruturada, nao
    # negociacao - cabe no modelo rapido. Tirar essa chamada do gpt-5 encurta o
    # caminho critico da rodada sem perder qualidade (diagnostico A1, 22/08/2026).
    # Vazio = usa o mesmo modelo da negociacao.
    NEGOTIATION_LEDGER_MODEL: str = "gpt-4o-mini"

    # Rodadas de consulta a base por resposta. Cada uma e um roundtrip inteiro de
    # LLM: 5 (o default do handler) e generoso demais para negociacao e foi um dos
    # fatores do timeout de 60s em homolog.
    NEGOTIATION_MAX_TOOL_CALLS: int = 2

    # Limite de cada chamada ao modelo do ZelinhU (leitura e redacao). O gpt-5
    # passa de 60 s com frequencia; o POST reverso do backend espera ate 120 s.
    # O minimo que o cliente aceita nao e mais uma fracao fixa do valor da causa:
    # vem do cliente (src/open_dots/negotiation/zelinhu.py, FLOOR_KEYS); sem ele,
    # o ZelinhU nao reduz o pedido.
    NEGOTIATION_LLM_TIMEOUT_S: float = 55.0

    # Segundo callback, o do DOCX. Cada callback cria uma mensagem no chat, e
    # _send_success_callback carrega UM arquivo por chamada - entao anunciar PDF e
    # DOCX significava o mesmo documento aparecendo duas vezes para o cliente.
    #
    # Desligado: o DOCX continua sendo gerado e subindo para o storage (fica
    # disponivel e a URL vai para o log), mas quem aparece no chat e so o PDF.
    # Ligar de volta restaura o comportamento antigo sem deploy.
    DOCUMENT_DOCX_CALLBACK_ENABLED: bool = False

    # Despacho terminal: POST reverso tambem no send_to_client, com
    # action="send_to_client" (plano de 25/08/2026, §2.2). Antes disso, a rodada
    # final tinha o corpo do callback como canal UNICO - e como toda negociacao
    # termina em send_to_client, toda conclusao evaporava quando o corpo era
    # cortado pelo Cloudflare.
    #
    # Depende do backend ler o discriminador (passo 3 do plano, entregue em
    # 26/08). Se aquele lado voltar atras, desligar aqui evita a rodada fantasma
    # sem precisar de deploy: sem a leitura, o POST terminal faz a IA da empresa
    # responder a uma proposta ja encerrada.
    NEGOTIATION_TERMINAL_DISPATCH_ENABLED: bool = True

    # Solicitacoes na negociacao: o ZelinhU pedir um documento, uma reuniao ou um
    # dado no meio da negociacao (spec 20260901-spec-solicitacoes-na-negociacao.md).
    #
    # LIGADA EM 08/09/2026, depois de MEDIR que a rota deles esta no ar.
    # ------------------------------------------------------------------
    # Ela nasceu desligada porque o schema do backend usa .passthrough(): com a
    # rota ausente, um action=request desconhecido NAO daria erro - entraria como
    # rodada comum e a IA da empresa RESPONDERIA ao pedido, sem nada quebrar em
    # lugar nenhum. Esse silencio era o unico motivo do freio.
    #
    # O aviso do passo 4 da secao 8 nunca chegou, entao sondamos
    # POST /api/webhooks/sender/message em producao (app.zellu.tec.br). A rota
    # valida o objeto `request` e devolve os codigos da spec, nao passthrough:
    #
    #   responseType invalido ou ausente -> 400 REQUEST_MALFORMED
    #   title com 250 chars              -> 400 REQUEST_TITLE_TOO_LONG
    #   request junto de negotiationComplete -> 400 REQUEST_AND_TERMINAL_CONFLICT
    #   request bem formado              -> passa a validacao (404 so por sessao
    #                                       sintetica, que era o esperado)
    #
    # Um backend com .passthrough() teria aceitado os tres primeiros. Nao aceitou:
    # o ciclo 1 deles esta deployado.
    #
    # A flag continua existindo como FREIO - desligar aqui volta ao comportamento
    # anterior sem deploy, e o campo `requests` da analise passa a ser ignorado
    # antes de virar POST.
    NEGOTIATION_REQUEST_ENABLED: bool = True

    # LEITURA DE IMAGEM NOS ANEXOS DA SOLICITACAO (fase 2, 10/09/2026)
    # ----------------------------------------------------------------
    # Comprovante fotografado e RG sao o formato mais comum do mundo real, e ate
    # 10/09 chegavam ao ZelinhU como "NAO LIDO - imagem". A leitura usa o
    # DocumentAnalyzer (GPT-4o Vision) que ja existia no lado da empresa.
    #
    # Ligada por default: um pedido de documento que ninguem le nao serve para
    # nada. A flag e o freio de CUSTO - e a resposta ao item 3 da secao 5.4 da
    # spec de 01/09 ("qual a ordem de custo por analise"): uma chamada de visao
    # por imagem recebida, so na retomada, nunca em rodada de negociacao comum.
    # Desligar volta ao comportamento anterior, sem deploy.
    NEGOTIATION_VISION_ENABLED: bool = True

    # Guarda de despacho persistida (sugestao 8 da spec de 01/09).
    # -----------------------------------------------------------
    # `_mark_dispatched` vive num dict da sessao, em memoria. No restart do
    # processo a memoria do despacho some, o ZelinhU redespacha o mesmo turno e a
    # IA da empresa roda de novo. Nenhuma chave do lado do backend pega isso: no
    # restart nem o `round` nem o corpo da NOSSA mensagem sobrevivem (secao 4.3).
    #
    # O endpoint /api/ai/idempotency/check-or-create e persistido, atomico e tem
    # TTL de 24h. Sondado em 08/09/2026: corpo {requestId, endpoint}, devolve
    # hit=false na primeira e hit=true depois; a identidade e SO o requestId.
    #
    # LIGADA por default, ao contrario da flag das solicitacoes: o endpoint esta
    # no ar e verificado, e todo caminho de falha (timeout, 4xx, 5xx, corpo
    # estranho) cai no comportamento de hoje - despacha. A flag existe como freio,
    # nao como espera de deploy.
    NEGOTIATION_DISPATCH_IDEMPOTENCY_ENABLED: bool = True

    # Teto por consulta a guarda persistida. Medimos 22-87ms na sondagem; 5s e
    # folga generosa. Estourar nao trava nada: cai no fail-open e despacha.
    NEGOTIATION_DISPATCH_IDEMPOTENCY_TIMEOUT: float = 5.0

    # Alias para compatibilidade com framework antigo e Pinecone (que usam lowercase)
    @property
    def openai_api_key(self) -> str:
        return self.OPENAI_API_KEY

    @property
    def openai_embedding_model(self) -> str:
        return self.OPENAI_EMBEDDING_MODEL

    @property
    def pinecone_api_key(self) -> str:
        return self.PINECONE_API_KEY

    @property
    def pinecone_index_name(self) -> str:
        return self.PINECONE_INDEX_NAME

    @property
    def pinecone_dimension(self) -> int:
        return self.PINECONE_DIMENSION

    @property
    def pinecone_environment(self) -> str:
        return self.PINECONE_ENVIRONMENT

    @property
    def rag_chunk_size(self) -> int:
        return self.RAG_CHUNK_SIZE

    @property
    def rag_chunk_overlap(self) -> int:
        return self.RAG_CHUNK_OVERLAP

    # API
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_TITLE: str = "Zellinho"

    # API Key for upload endpoint (same as Zellu uses)
    API_KEY_ZELLU_IA: str = ""
    # Secret compartilhado para assinar webhooks enviados ao backend Zellu.
    AI_WEBHOOK_HMAC_SECRET: str = ""

    # ---------------------------------------------------------------------
    # Atendimento telefonico por IA (ElevenLabs Agents)
    # Contrato: 20260823-contrato-atendimento-telefonico-elevenlabs.md
    # ---------------------------------------------------------------------
    # Chave da workspace ElevenLabs. Usada para baixar o audio da ligacao.
    ELEVENLABS_API_KEY: str = ""
    # Agente que atende. E o MESMO no telefone e na chamada de voz no app
    # (emenda 20260903): mesmo prompt, mesmas tools, mesmo data
    # collection. Criado por agents/conversational_agent/main.py.
    ELEVENLABS_AGENT_ID: str = ""
    # Segredo do post-call webhook, gerado junto com o webhook na ElevenLabs.
    #
    # 🔴 OBRIGATORIO em qualquer ambiente que receba ligacao. A rota
    # /webhooks/elevenlabs/post-call e isenta do gate global de x-api-key
    # (src/main.py:ROTAS_SEM_GATE_DE_CHAVE) porque a ElevenLabs autentica por
    # HMAC, entao este segredo e a UNICA autenticacao dela. VAZIO significa que
    # a rota RECUSA todo webhook - e nenhuma ligacao chega na Zellu.
    ELEVENLABS_WEBHOOK_SECRET: str = ""
    # Enable only after the site persists messagedata.intakeRecord as an open ticket.
    CLIENT_INTAKE_RECORD_ENABLED: bool = False
    ELEVENLABS_API_URL: str = "https://api.elevenlabs.io"
    # Reportado como `model` no evento de custo da ligacao. O contrato de voz
    # (§5) fixa este literal: e a chave da tabela de precos da Zellu, e um valor
    # que nao casa com linha nenhuma entra com custo chapado de fallback. Se o
    # agente trocar de modelo TTS, a Zellu precisa semear o preco antes.
    ELEVENLABS_VOICE_MODEL: str = "eleven_turbo_v2_5"

    # ---------------------------------------------------------------------
    # Observabilidade de custos de IA - contrato /api/ai/usage enriquecido
    # docs/20260619-contrato-ai-usage-enriquecido.md
    # ---------------------------------------------------------------------
    # Liga/desliga o envio de eventos de uso/custo para o backend Zellu.
    AI_USAGE_ENABLED: bool = True
    # API key do header x-api-key esperado por /api/ai/usage (SystemSetting
    # API_KEY_IA_PRODUCTION no backend). Se vazio, usa API_KEY_ZELLU_IA.
    API_KEY_IA_PRODUCTION: str = ""
    # Timeout (ms) para o POST /api/ai/usage. Fire-and-forget: nunca bloqueia o fluxo.
    AI_USAGE_TIMEOUT_MS: int = 8000

    @property
    def ai_usage_api_key(self) -> str:
        """Chave usada no header x-api-key de /api/ai/usage (fallback p/ a chave Zellu)."""
        return self.API_KEY_IA_PRODUCTION or self.API_KEY_ZELLU_IA

    # Zellu Integration - URL do Backend Zellu (Next.js) que recebe respostas da IA
    # IMPORTANTE: Esta URL deve apontar para o Backend Zellu (porta 8655), NÃO para este serviço de IA (porta 8000)
    # Use http://localhost:8655 quando os serviços rodam no mesmo servidor
    ZELLU_WEBHOOK_URL: str = "http://zellu-ia.147.93.9.113.sslip.io"

    # Entrega garantida do callback da IA -> app (POST /api/chat/webhook).
    # Contrato: docs/20260805-contrato-entrega-garantida-callback-webhook.md
    # Ponto 2: 5xx significa que a resposta NAO foi persistida do lado do app;
    # sem retry aqui, ela se perde. Pior caso ~93s (3 x 30s + backoff 1s + 2s),
    # dentro do watchdog de 2 min do app (CHAT_AI_RESPONSE_TIMEOUT_SECONDS).
    CHAT_CALLBACK_TIMEOUT_S: float = 30.0     # timeout por tentativa
    CHAT_CALLBACK_MAX_ATTEMPTS: int = 3       # inclui a primeira tentativa
    CHAT_CALLBACK_BACKOFF_S: float = 1.0      # backoff inicial, dobra a cada retry

    # Zellu Backend Integration (Company Response AI)
    # URL do backend Zellu para enviar mensagens da nossa IA (ZelinhU) para empresa
    ZELLU_BACKEND_WEBHOOK_URL: str = ""  # Ex: http://localhost:3000/api/webhooks/company/ticket-response
    ZELLU_COMPANY_ID: str = ""  # ID da empresa na plataforma Zellu
    ZELLU_API_KEY: str = ""  # API Key para autenticação com backend Zellu

    # URL base do Backend Zellu para busca de empresas (POST /api/ai/validate-company)
    ZELLU_BACKEND_URL: str = "http://localhost:8655"

    # URL base do nosso serviço (para o backend Zellu chamar de volta)
    SERVICE_BASE_URL: str = "http://localhost:8000"

    # Company Response AI Configuration (conforme setup-guide.md)
    # Modo mock usa dados em memória, produção chama endpoints reais
    COMPANY_RESPONSE_USE_MOCK: bool = True
    COMPANY_RESPONSE_WEBHOOK_URL: str = ""  # URL do serviço de IA real (produção)
    COMPANY_RESPONSE_WEBHOOK_TIMEOUT_MS: int = 60000  # 60s timeout
    COMPANY_RESPONSE_WEBHOOK_MAX_RETRIES: int = 3
    COMPANY_RESPONSE_WEBHOOK_RETRY_DELAY_MS: int = 2000
    COMPANY_WEBHOOK_RATE_LIMIT: int = 15  # req/min por sessionId
    # ---------------------------------------------------------------------
    # Versao do template da PETICAO INICIAL (contrato de documentos fieis).
    #
    # A "v2" (plano de 22/09) devolve as subsecoes 3.1 a 3.4 ao texto do
    # modelo e troca quatro secoes por catorze lacunas pontuais. Ligada em
    # 23/09, quando a plataforma confirmou o template v2 em producao. "v1"
    # continua valendo como volta rapida por variavel de ambiente, sem deploy.
    #
    # A amigavel e a notificacao nao tem versao: seguem o contrato vigente.
    PETICAO_TEMPLATE_VERSION: str = "v2"

    COMPANY_RESPONSE_AI_MODEL: str = "gpt-4o-mini"
    COMPANY_RESPONSE_AI_MAX_TOKENS: int = 1500
    COMPANY_RESPONSE_AI_TEMPERATURE: float = 0.7

    # ---------------------------------------------------------------------
    # TURNO ASSINCRONO DA IA DA EMPRESA
    # Contrato: 20260911-turno-assincrono-ia-da-empresa.md
    # ---------------------------------------------------------------------
    # O interruptor oficial e a presenca do `callbackUrl` no payload de turno:
    # com ele respondemos 202 e entregamos o resultado por callback; sem ele,
    # 200 sincrono como sempre. Esta flag existe so para NOS voltarmos ao
    # sincrono sem depender de um deploy do backend.
    COMPANY_TURN_ASYNC_ENABLED: bool = True
    # Entrega do callback do turno. Um 5xx do backend significa que a mensagem
    # da empresa NAO foi gravada (regra 3 do §3.3): sem retry aqui, o turno se
    # perde e a negociacao so volta a andar quando o cron deles desiste.
    COMPANY_TURN_CALLBACK_TIMEOUT_S: float = 30.0   # timeout por tentativa
    COMPANY_TURN_CALLBACK_MAX_ATTEMPTS: int = 3     # inclui a primeira tentativa
    COMPANY_TURN_CALLBACK_BACKOFF_S: float = 2.0    # backoff inicial, dobra a cada retry

    # RAG Configuration
    RAG_ENABLED: bool = True
    PINECONE_API_KEY: str = ""
    PINECONE_ENVIRONMENT: str = "us-east-1"
    PINECONE_DIMENSION: int = 1536
    PINECONE_INDEX_NAME: str = "zellu-legal-docs"
    DOCUMENTS_PATH: str = str(BASE_DIR / "data" / "documents")
    # Hosts exatos adicionais das URLs de arquivos enviadas pelo backend.
    # MINIO_ENDPOINT também é permitido. Sem curingas/hosts inferidos do payload.
    # Padrao: o bucket R2 onde a plataforma guarda os anexos do chat (host visto no
    # log de 04/10/2026). A variavel de ambiente, em JSON, substitui a lista inteira.
    EVIDENCE_ALLOWED_HOSTS: list[str] = ["74f9bbf23d5cd0707f3936b57e0ec774.r2.cloudflarestorage.com"]
    # Formulario de nome/telefone/e-mail/CPF no chat de abertura. Desligado: a
    # plataforma ja registra no caso o usuario da conta logada.
    INTAKE_CLIENT_DETAILS_FORM: bool = False

    # Auditoria documental defensiva. O scan roda fora do event loop, com timeout
    # e concorrencia limitada, para nao travar a API. Em producao o compose sobe
    # um clamd separado; em dev, indisponibilidade vira flag auditavel.
    DOCUMENT_AUDIT_ENABLED: bool = True
    DOCUMENT_MALWARE_SCAN_ENABLED: bool = True
    DOCUMENT_MALWARE_FAIL_CLOSED: bool = False
    DOCUMENT_AUDIT_MAX_CONCURRENCY: int = 2
    DOCUMENT_MAX_EXPANDED_BYTES: int = 150 * 1024 * 1024
    DOCUMENT_PDF_MAX_OBJECTS: int = 200_000  # containers inspecionados por PDF
    DOCUMENT_VISION_MIN_TEXT_CHARS: int = 800
    DOCUMENT_VISION_MAX_PAGES: int = 3
    DOCUMENT_VISION_TIMEOUT_S: float = 25.0
    DOCUMENT_TRANSCRIPTION_TIMEOUT_S: float = 90.0
    # Assinatura digital de PDF (pyHanko). Raizes confiaveis em PEM/DER no
    # diretorio abaixo (ex.: ACs-Raiz ICP-Brasil, publicadas pelo ITI). Sem
    # raizes, so a integridade e verificada; autoria fica nao confirmada.
    DOCUMENT_SIGNATURE_VERIFY_ENABLED: bool = True
    DOCUMENT_SIGNATURE_TRUST_DIR: str = str(BASE_DIR / "security" / "trust_roots")
    DOCUMENT_SIGNATURE_ALLOW_FETCHING: bool = False  # CRL/OCSP pela rede
    DOCUMENT_SIGNATURE_TIMEOUT_S: float = 20.0
    CLAMAV_HOST: str = "clamav"
    CLAMAV_PORT: int = 3310
    CLAMAV_TIMEOUT_S: float = 8.0
    RAG_CHUNK_SIZE: int = 1000
    RAG_CHUNK_OVERLAP: int = 200
    RAG_TOP_K: int = 3

    # Upload/Storage Configuration
    UPLOAD_DIR: str = str(BASE_DIR / "uploads")
    MAX_FILE_SIZE: int = 25 * 1024 * 1024  # 25MB = StreamMaxLength padrao do clamd e MAX_BYTES do case_evidence
    MAX_TOTAL_SIZE: int = 100 * 1024 * 1024  # 100MB
    ALLOWED_MIME_TYPES: list = [
        # Audio
        "audio/webm", "audio/mpeg", "audio/wav", "audio/ogg", "audio/m4a",
        "audio/mp4", "audio/x-m4a", "audio/x-wav", "audio/flac",
        # Video
        "video/mp4", "video/webm", "video/avi", "video/quicktime",
        # Images
        "image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp",  # SVG fora: carrega <script>
        # Documents
        "application/pdf", "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
        # Compressed
        "application/zip", "application/x-rar-compressed", "application/x-7z-compressed",
        # Others
        "application/json", "text/plain"
    ]

    # MinIO/S3 Configuration (optional - nao usado em producao hoje)
    # MINIO_ENABLED=false (padrao): MinioService nao faz nenhuma chamada de rede
    # no boot nem depois. MINIO_ENDPOINT continua valendo para liberar o host em
    # downloads de evidencias (case_evidence), independente desta flag.
    MINIO_ENABLED: bool = False
    MINIO_TIMEOUT_SECONDS: float = 5.0
    MINIO_ENDPOINT: str = ""
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "chat-ticket-files"
    MINIO_USE_SSL: bool = True

    # Edge route (plugin edgeroute do Bifrost). EDGE_ROUTE=false (padrao): nada muda.
    # Quando true, as chamadas LLM saem marcadas como trafego edge via header.
    EDGE_ROUTE: bool = False
    # Nome do header lido pelo plugin edgeroute.
    EDGE_ROUTE_HEADER: str = "X-Bifrost-Route"

    # Firecrawl (web search agent para busca de CNPJ)
    FIRECRAWL_API_KEY: str = ""

    # Cache persistente das buscas de CNPJ feitas pelo chat.
    # Se MONGODB_URI ficar vazio, o atendimento continua sem salvar cache local.
    MONGODB_URI: str = ""
    API_CNPJ_CONNECTION_URL: str = ""
    API_CNPJ_CONNECTION_KEY: str = ""
    MONGODB_DATABASE: str = "cnpjsdb"
    COMPANY_LOOKUP_CACHE_COLLECTION: str = "cnpjs"
    MONGODB_TIMEOUT_MS: int = 3000
    STAGED_INTAKE_ENABLED: bool = True
    INTAKE_CONVERSATION_MODEL: str = "gpt-5-mini"
    # Escada de busca da empresa: 2a tentativa (web profunda) tem orcamento proprio,
    # maior que o da 1a (Scrapling 12 s). ON_EMPTY: 1a tentativa vazia ja aprofunda.
    COMPANY_DEEP_SEARCH_TIMEOUT_S: float = 30.0
    COMPANY_DEEP_SEARCH_ON_EMPTY: bool = True
    JEV_API_KEY: str = ""
    JEV_API_URL: str = "https://api.typesafe.ai/v1/systemone"
    JEV_MODEL: str = "jev-latest"
    JEV_CONFIDENCE_THRESHOLD: float = 0.70
    JEV_MARGIN_THRESHOLD: float = 0.50
    # Conferência semântica extra do Writer. FAIL decisivo aciona a correção e,
    # se persistir, vira ponto a corrigir (não bloqueia). Para FAIL, o gate usa P(FAIL)
    # retornada pelo JEV + margem; o campo genérico confidence fica só no log.
    # Falha do provedor JEV não derruba a geração.
    DOCUMENT_JEV_VALIDATION_ENABLED: bool = True
    DOCUMENT_JEV_CONFIDENCE_THRESHOLD: float = 0.65
    DOCUMENT_JEV_MARGIN_THRESHOLD: float = 0.40
    # O payload leva o caso inteiro: 3 s estourava e o JEV sumia em silencio
    # (provider_error). Roda em paralelo com o revisor, que leva bem mais.
    DOCUMENT_JEV_TIMEOUT_S: float = 25.0
    LEGAL_RAG_NAMESPACE: str = "json-sections-v1"

    # Rate Limiting
    RATE_LIMIT_REQUESTS: int = 10
    RATE_LIMIT_WINDOW: int = 60  # seconds

    # ---------------------------------------------------------------------
    # Redis + cache de embeddings (contrato 20260629 - "Cache de embeddings
    # por hash de texto"). Credenciais vem do servidor (Coolify): setar
    # REDIS_HOST, REDIS_PORT, REDIS_USERNAME e REDIS_PASSWORD como env vars.
    # Se REDIS_HOST ficar vazio, o cache opera SO em memoria (sem quebrar).
    # ---------------------------------------------------------------------
    REDIS_ENABLED: bool = True
    REDIS_HOST: str = ""            # hostname interno do Redis no Coolify
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_USERNAME: str = ""        # credencial (Coolify)
    REDIS_PASSWORD: str = ""        # credencial (Coolify)
    REDIS_SSL: bool = False

    # Cache de embeddings (sha256(model+texto) -> vetor).
    EMBEDDING_CACHE_ENABLED: bool = True
    # Embedding e deterministico; TTL so limita memoria no Redis (default 30 dias).
    EMBEDDING_CACHE_TTL_SECONDS: int = 2592000
    # Tamanho maximo do cache L1 em memoria (itens, LRU).
    EMBEDDING_CACHE_MEMORY_MAX: int = 10000

    class Config:
        """Configuração adicional."""

        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # Ignore extra fields in .env




# Singleton para settings
_settings_instance = None

def get_settings() -> Settings:
    """Retorna instancia singleton de Settings."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance



def llm_default_headers() -> dict:
    """default_headers para clientes OpenAI: {EDGE_ROUTE_HEADER: 'edge'} se EDGE_ROUTE, senao {}."""
    try:
        settings = get_settings()
    except Exception:  # settings invalidas (ex.: testes sem env) -> comportamento padrao
        return {}
    if settings.EDGE_ROUTE and settings.EDGE_ROUTE_HEADER:
        return {settings.EDGE_ROUTE_HEADER: "edge"}
    return {}
