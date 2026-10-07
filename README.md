# zelinho-ia

Serviço de IA da **Zellu** (legal-tech em pré-lançamento): o "ZelinhU", assistente que atende o consumidor no chat e no telefone, entende o caso, encontra a empresa certa, gera documentos jurídicos e conduz a negociação com a IA da empresa.

Este repositório é uma **versão limpa** do antigo `zellinho_chat`: contém só o código que de fato roda em produção (rastreado a partir do ponto de entrada `app.py`), sem experimentos, dumps, caches ou dados de clientes. O comportamento é o mesmo do serviço original — as 46 rotas da API são idênticas e a suíte de testes original dá exatamente o mesmo resultado nos dois códigos (ver [`docs/arquivos-removidos.md`](docs/arquivos-removidos.md)).

> Mantido por Arthur (AI engineer). Documentação de tudo que foi feito em [`docs/`](docs/) e [`CHANGELOG.md`](CHANGELOG.md).

## Sumário

- [Arquitetura](#arquitetura)
- [Estrutura de pastas](#estrutura-de-pastas)
- [Rodar localmente](#rodar-localmente)
- [Rodar no Coolify](#rodar-no-coolify)
- [Variáveis de ambiente](#variáveis-de-ambiente)
- [Testes](#testes)
- [Documentação](#documentação)

## Arquitetura

```
                ┌────────────────────────── Backend Zellu (app/painel) ──────────────────────────┐
                │  POST /api/chat/webhook   POST /api/webhooks/company/...   /api/phone/calls      │
                └───────────────▲───────────────────────────┬─────────────────────▲──────────────┘
                     callbacks  │                            │ webhooks            │ registro da ligação
                                │                            ▼                     │ (chatId/userId)
┌───────────────────────────────┴───────────── zelinho-ia (FastAPI, :8000) ───────┴──────────────┐
│ app.py ─► uvicorn "src.main:app"                                                                │
│                                                                                                 │
│  Chat do consumidor (/message, /webhook/chat, /api/external/chat/webhook)                       │
│    └─ Open Dots (src/open_dots) ─ intake em etapas (staged_intake_flow, gpt-5-mini)             │
│         ├─ Escada de busca da empresa: Mongo → Casa dos Dados → Scrapling → web profunda        │
│         │                              → pedir referência (CNPJ/site/nota)                      │
│         ├─ Roteamento jurídico: Pinecone (candidatos) → JEV (escolha) → GPT (fallback)          │
│         ├─ Documentos: auditoria/OCR (PaddleOCR), validação JEV, geração (WeasyPrint/docx)      │
│         └─ Negociação ZelinhU ↔ IA da empresa (api/routes/negotiation, webhooks_sender)         │
│                                                                                                 │
│  Canal de voz (agente ElevenLabs "Raquel")                                                      │
│    ├─ POST /phone/age-check          → agents/conversational_agent/age_check.py                 │
│    ├─ POST /phone/identity/lookup    → phone_identity.py (código por SMS)                       │
│    ├─ POST /phone/identity/verify    → phone_identity.py                                        │
│    ├─ POST /phone/web-call/token     → web_call.py (ligação dentro do app)                      │
│    └─ POST /webhooks/elevenlabs/post-call → post_call.py (HMAC) → /api/phone/calls              │
│                                          → voice_chat_handoff.py (relato entra no chat)         │
│                                                                                                 │
│  LLM via OpenAI SDK (opcionalmente pelo gateway Bifrost com EDGE_ROUTE=true)                    │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
       │ OpenAI   │ Pinecone   │ JEV (typesafe)   │ MongoDB (cache CNPJ)   │ Redis (cache embeddings, opcional)
       │ Firecrawl/Scrapling   │ MinIO (desligado por padrão)   │ ClamAV (scan de anexos, opcional)
```

Pontos importantes:

- **Ponto de entrada**: `python app.py` valida a configuração, imprime um resumo e sobe `uvicorn src.main:app` em `API_HOST:API_PORT` (padrão `0.0.0.0:8000`). Auto-reload **desligado** por padrão (só liga com `UVICORN_RELOAD=true` em dev).
- **Healthcheck**: `GET /health` → `{"status":"healthy"}`.
- **Roteamento JEV** (`src/services/staged_intake_flow.py`, `case_document_pipeline.py`, `document_jev_validator.py`): Pinecone traz os artigos candidatos, o JEV escolhe com probabilidades e limiares (`JEV_CONFIDENCE_THRESHOLD`, `JEV_MARGIN_THRESHOLD`), e o GPT entra como fallback. **Esta lógica é mantida exatamente como no serviço original.**
- **Estado** das conversas fica em memória/SQLite local (`data/documents/.case_evidence`, `.voice_intake`) — nunca versionado.

## Estrutura de pastas

```
app.py                 ponto de entrada (valida config e sobe o uvicorn)
config.py              Settings (pydantic-settings) — todas as variáveis de ambiente
src/main.py            app FastAPI, rotas principais e wiring dos routers
src/open_dots/         runtime do chat (intake, leitura, writer, ZelinhU, negociação)
src/services/          serviços: intake em etapas, busca de empresa, documentos, RAG, JEV, voz
src/knowledge_bases/   bases de conhecimento compartilhadas (seed de jurisprudência/legislação)
src/utils/             utilitários (datas, telefone, CNPJ, aeroportos, normalização de texto)
api/                   routers e schemas (empresa, chat externo, negociação, reabertura, sender)
agents/                estado da conversa, guide agent e canal de voz (conversational_agent/)
models/                modelo de empresa
templates/ static/     templates HTML dos documentos e página estática
data/documents/        base jurídica em texto (RAG) — sem dados de clientes
security/trust_roots/  raízes de confiança para validar assinatura digital de PDFs
tests/                 testes selecionados (busca de empresa, voz, idade, JEV, intake, upload)
docs/                  documentação detalhada (pt-BR)
```

## Rodar localmente

Requisitos: Python **3.11** (mesma versão da imagem) e as bibliotecas de sistema do WeasyPrint (pango/harfbuzz). No Ubuntu/Debian:

```bash
sudo apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libgl1 libglib2.0-0 libgomp1 fonts-liberation
```

```bash
git clone <repo> zelinho-ia && cd zelinho-ia
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # demora: paddleocr/paddlepaddle são grandes
cp .env.example .env                      # preencha OPENAI_API_KEY e PINECONE_API_KEY (nunca commite o .env)
python app.py                             # sobe em http://localhost:8000
curl http://localhost:8000/health         # {"status":"healthy"}
```

Para desenvolver com auto-reload: `UVICORN_RELOAD=true python app.py`.
Swagger: `http://localhost:8000/docs`.

Com Docker:

```bash
docker build -t zelinho-ia .
docker run --env-file .env -p 8000:8000 zelinho-ia
```

## Rodar no Coolify

1. **Novo recurso** → Application → repositório Git deste projeto, branch `main`, build pack **Dockerfile** (o `Dockerfile` da raiz).
2. **Porta**: `8000`. Domínio: o do serviço de IA (hoje `ia-service.zellu.tec.br`).
3. **Environment Variables**: cadastre as variáveis da tabela abaixo **direto no Coolify** (nunca no repositório). Cole **só o valor** — sem repetir `NOME=` no campo de valor (já causou 401 na OpenAI, ver [`docs/chat-intake.md`](docs/chat-intake.md)).
4. **Healthcheck**: já vem do Dockerfile — `start-period=180s`, `interval=30s`, `timeout=10s`, `retries=5` em `/health`. Não reduza o start-period: o boot importa bibliotecas pesadas e o Coolify fazia rollback antes do app ficar saudável.
5. Garanta `MINIO_ENABLED=false` (padrão) — o MinIO não é usado e travava o boot.
6. **Não** configure comando custom com `--reload`; o `CMD` é `python app.py`.
7. Deploy. O primeiro build é longo (dependências de OCR); detalhes e incidentes em [`docs/deploy-coolify.md`](docs/deploy-coolify.md).

## Variáveis de ambiente

Lista completa (só nomes) em [`.env.example`](.env.example); padrões em `config.py`. As principais:

| Variável | Obrigatória | Padrão | Para quê |
|---|---|---|---|
| `OPENAI_API_KEY` | **sim** | — | Chave da OpenAI (todas as chamadas LLM e embeddings) |
| `PINECONE_API_KEY` | **sim** (na prática) | — | Pinecone; o cliente é criado no import — sem ela o app não sobe |
| `PINECONE_INDEX_NAME` / `PINECONE_ENVIRONMENT` | não | `zellu-legal-docs` / `us-east-1` | Índice RAG jurídico |
| `INTAKE_CONVERSATION_MODEL` | não | `gpt-5-mini` | Modelo do intake conversacional |
| `INTAKE_REASONING_EFFORT` / `INTAKE_VERBOSITY` | não | `minimal` / `low` | Latência do intake (gpt-5*) |
| `OPENAI_MODEL` / `OPENAI_MODEL_FAST` | não | `gpt-5` / `gpt-4o-mini` | Modelos gerais / rápidos |
| `JEV_API_KEY` | sim p/ JEV | — | Roteamento jurídico e rerank via JEV (**não alterar a lógica**) |
| `JEV_API_URL`, `JEV_MODEL` | não | typesafe `systemone`, `jev-latest` | Endpoint/modelo JEV |
| `JEV_CONFIDENCE_THRESHOLD` / `JEV_MARGIN_THRESHOLD` | não | `0.70` / `0.50` | Limiares de aceitação do JEV |
| `ZELLU_BACKEND_URL` / `ZELLU_BACKEND_WEBHOOK_URL` | sim em prod | — | Backend Zellu (callbacks do chat, `/api/phone/calls`) |
| `API_KEY_ZELLU_IA` | sim em prod | — | Chave S2S que o backend e a ElevenLabs usam para chamar este serviço (`x-api-key`) |
| `AI_WEBHOOK_HMAC_SECRET` | sim em prod | — | HMAC dos webhooks backend → IA |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_AGENT_ID` | sim p/ voz | — | Agente de voz Raquel (download de áudio, web call) |
| `ELEVENLABS_WEBHOOK_SECRET` | sim p/ voz | — | HMAC do webhook pós-chamada (tem que ser **igual** ao configurado na ElevenLabs) |
| `MONGODB_URI` / `MONGODB_DATABASE` | não | — / `cnpjsdb` | Cache/base de CNPJ (1º degrau da busca de empresa) |
| `API_CNPJ_CONNECTION_URL` / `API_CNPJ_CONNECTION_KEY` | não | — | API de CNPJ |
| `FIRECRAWL_API_KEY` | não | — | Busca web de empresa |
| `COMPANY_DEEP_SEARCH_TIMEOUT_S` / `COMPANY_DEEP_SEARCH_ON_EMPTY` | não | `30` / `true` | 2º degrau da escada (web profunda) |
| `MINIO_ENABLED` | não | `false` | Liga o MinIO (não usado hoje) |
| `EDGE_ROUTE` / `EDGE_ROUTE_HEADER` | não | `false` / `X-Bifrost-Route` | Marca chamadas LLM para o gateway Bifrost ([`docs/bifrost-edge.md`](docs/bifrost-edge.md)) |
| `REDIS_ENABLED` / `REDIS_HOST` | não | `true` / — | Cache de embeddings (sem host, usa só memória) |
| `RAG_ENABLED` | não | `true` | Liga o RAG jurídico |
| `DOCUMENT_MALWARE_SCAN_ENABLED` / `CLAMAV_HOST` | não | `true` / `clamav` | Scan de anexos (ClamAV) |
| `UVICORN_RELOAD` | não | `false` | Auto-reload só para dev local |

## Testes

```bash
pip install -r requirements-dev.txt
pytest -q tests        # 194 testes: escada de busca, voz (idade, telefone, pós-chamada, handoff), JEV, intake, upload
```

## Documentação

| Documento | Conteúdo |
|---|---|
| [`docs/elevenlabs-raquel.md`](docs/elevenlabs-raquel.md) | Log cronológico do agente de voz Raquel: voz, portão de idade, fillers, dicionário de pronúncia, regras de telefone, end_call, bake-off de modelos, lacuna da transcrição pós-chamada |
| [`docs/chat-intake.md`](docs/chat-intake.md) | Rodada de latência/humanização do chat, escada de busca de empresa, intake gpt-5-mini, correção da chave OpenAI, novas variáveis |
| [`docs/deploy-coolify.md`](docs/deploy-coolify.md) | Deploy no Coolify: travamento do MinIO, rollbacks do healthcheck, `--reload`, rebuild de 43 min, incidente de 2026-10-07 |
| [`docs/bifrost-edge.md`](docs/bifrost-edge.md) | Gateway Bifrost, plugin edge route e Datadog |
| [`docs/arquivos-removidos.md`](docs/arquivos-removidos.md) | O que ficou de fora do repo antigo e por quê |
| [`docs/backlog.md`](docs/backlog.md) | Pendências |
| [`CHANGELOG.md`](CHANGELOG.md) | Histórico datado |
