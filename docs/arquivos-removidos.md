# Arquivos que ficaram de fora (repo antigo → zelinho-ia)

Origem: `zellinho_chat-main` (snapshot de 2026-10-06/07 com as rodadas 1 a 4 aplicadas). O repo antigo tinha **547 arquivos (~15 MB)**; o `zelinho-ia` tem só o que o serviço usa.

## Como foi decidido o que entra

1. **Rastreamento estático de imports** a partir de `app.py` → `src.main` → todos os routers (`api/routes/*`, `agents/conversational_agent/{age_check,phone_identity,post_call,web_call}.py`) e serviços. A análise percorre *todos* os `import`/`from … import`, inclusive os que ficam dentro de funções (imports tardios), e os pacotes-pai. Resultado: **133 módulos Python**.
2. **Arquivos de dados lidos em runtime** (procurados por `open(...)`, `Path(__file__)`, `DOCUMENTS_PATH`, templates): `templates/*.html`, `static/index.html`, `src/utils/aeroportos_br.json`, `src/knowledge_bases/data/seed_*.json`, `data/documents/direito_*.txt` (RAG jurídico), `security/trust_roots/`.
3. **Licença do código vendorizado** (`src/open_dots/vendor/LICENSE`, `UPSTREAM.md`) mantida.
4. **Versões mais novas**: conferido arquivo a arquivo contra `pr_files/` e os 4 zips de entrega. Tudo já estava no `zellinho_chat-main`, **exceto** `agents/conversational_agent/age_check.py`, que veio do `zellinho_age_check.zip` (rodada 4, mais formatos de data).

## Verificação de que nada usado ficou para trás

- `pip install -r requirements.txt` em venv Python 3.11 limpo: OK.
- Import de **todos** os 133 módulos: 133 OK, 0 falhas. Imports tardios de terceiros (paddleocr, pyhanko, weasyprint, scrapling, firecrawl, pypdfium2, docx, redis, pymongo, minio): todos OK.
- Boot real com `python app.py` e env de teste (`MINIO_ENABLED=false`, chaves falsas): **`GET /health` → 200 `{"status":"healthy"}` em ~3 s**, sem processo de reload.
- **Rotas**: a lista (método + caminho) das 46 rotas do app é **idêntica** entre o repo antigo e o novo.
- **Testes**: a suíte original inteira (1.333 testes) rodada contra o código antigo e contra o novo dá **o mesmo conjunto de resultados** (1.268 passam, as mesmas 65 falham nos dois — falhas pré-existentes, ver "Riscos" abaixo). Ou seja: a limpeza não quebrou nada que funcionava.

## O que ficou de fora e por quê

| Item (repo antigo) | Qtde | Motivo |
|---|---|---|
| `.env` | 1 | **Segredos reais.** Nunca entra em repositório. |
| `debug_payloads/*.json` | 35 | Dumps de payloads reais de chats (dados pessoais de clientes). O código ainda pode gerar esse dump em runtime; a pasta está no `.gitignore`/`.dockerignore`. |
| `document_urls/*.json` | 53 | URLs de documentos gerados para casos reais (dados pessoais). Pasta é criada vazia no container. |
| `generated_documents/*` | 54 | Documentos jurídicos gerados para casos reais (dados pessoais). Pasta é criada vazia no container. |
| `data/documents/.case_evidence/records.sqlite3` | 1 | Banco local de evidências de casos (dados pessoais). Recriado em runtime. |
| `docs_teste/` (inclui `instrucao_secreta.pdf`, casos) | 16 | Material de teste manual/red-team; não é lido pelo código (só era copiado pelo Dockerfile antigo). |
| `data/documents/0{1..4}-*.md` | 4 | Conteúdo do guia por perfil; só é usado pelo script `scripts/index_guide_docs.py` (indexação manual no Pinecone), não pelo serviço. |
| `data/documents/Modelo_*.docx` | 3 | Modelos de referência citados apenas em comentários; o código não abre esses arquivos. |
| Cópias legadas na raiz: `main.py`, `intake.py`, `state.py`, `state_manager.py`, `staged_intake_flow.py`, `company_intake.py`, `company_search_pipeline.py`, `case_documents.py`, `document_generation_service.py`, `document_validation_service.py` | 10 | Duplicatas antigas dos módulos em `src/` e `agents/`; nenhum import aponta para elas (o app sobe `src.main:app`). |
| `PATCH_autostart_handler.py` | 1 | Patch avulso não importado. |
| `src/intake.py`, `src/open_dots/company.py`, `src/services/chat_memory_layer.py` | 3 | Código morto: nenhum módulo importa. |
| `api/schemas_zellu.py` | 1 | Schemas não referenciados. |
| `models/conversation.py`, `escalation.py`, `knowledge_base.py`, `standard_response.py` | 4 | Modelos não referenciados (só `models/company.py` é usado). |
| `agents/conversational_agent/main.py` + `voice_intake_prompt.txt` + `requirements.txt` | 3 | Script de **provisionamento** do agente ElevenLabs (cria/atualiza o agente via SDK `elevenlabs`); não roda no serviço. A configuração viva do agente está documentada em `docs/elevenlabs-raquel.md` e `docs/anexos/`. |
| `agents/conversational_agent/tests/*` | 3 | Testes do script de provisionamento acima. |
| `scripts/*.py` (index_guide_docs, validate_company_mongo/search, validate_document_audit, build_aeroportos_br) | 5 | Ferramentas operacionais manuais (indexação, validação contra bases reais, geração do JSON de aeroportos). Ficam no repo antigo. |
| `tests/` (91 de 94 arquivos) | 91 | Suíte antiga muito acoplada a dados/fixtures e com 65 falhas pré-existentes. Mantidos **17 arquivos de teste** que cobrem o trabalho recente e passam 100% (194 testes) + `conftest.py`. |
| `tests/data/**` (casos, PDFs, notas fiscais) | — | Fixtures da suíte antiga; os testes mantidos não usam. |
| `docs/` antigo (51 arquivos: specs, contratos datados, PDFs, zips, `backup_urls_supabase_*.txt`, `temp_doc.pdf`) | 51 | Documentação histórica/contratos com o backend, misturada a backups e temporários. A documentação nova está em `docs/` deste repo. |
| `*.md` da raiz (HANDOFF, ARQUITETURA_PROJETO, SETUP__, specs de webhook, respostas datadas) | ~20 | Mesma razão. **Atenção: o `HANDOFF.md` antigo contém uma chave de API em texto puro** (ver Riscos). |
| PDFs (`DOCUMENTAÇÃO TÉCNICA ZELLU[1].pdf` ×2, `Relatorio_QA_...pdf`) | 3 | Documentos, não código. |
| `nova documentação/company-response.zip`, `external-chat-user-management.7z`, `docs/*.zip` | 3+ | Pacotes compactados (não versionamos zips). |
| `reports/company_search_live_validation.json` | 1 | Saída de validação manual contra bases reais. |
| `response.json`, `MANIFEST.txt`, `tmpclaude-*-cwd` | 4 | Temporários. |
| `.claude/setttings.local.json`, `.pytest_cache/`, `__pycache__/` | — | Configuração de ferramenta e caches. |
| `docker-compose.yml` | 1 | Compose local com ClamAV; o deploy real é Coolify + Dockerfile. Pode ser recuperado do repo antigo se precisar de ClamAV local. |
| `README.md`, `.gitignore`, `.dockerignore`, `.env.example`, `requirements.txt` antigos | 5 | Reescritos para este repo. |

## Mudanças em arquivos copiados (as únicas)

| Arquivo | Mudança | Por quê |
|---|---|---|
| `agents/conversational_agent/age_check.py` | Versão do `zellinho_age_check.zip` | Rodada 4: aceita D/M/AA, D.M.AAAA, DDMMAAAA, DDMMAA |
| `app.py` | Auto-reload agora é **opt-in** (`UVICORN_RELOAD=true`); produção também detectada por `COOLIFY_*`/`ENVIRONMENT=production` | O app antigo só desligava o reload se existisse `RAILWAY_ENVIRONMENT` ou `RENDER` — no Coolify nenhuma das duas existe, então o **uvicorn rodava com reload em produção** |
| `Dockerfile` | Removidos `COPY docs_teste/ document_urls/ generated_documents/`; adicionado `COPY data/`; as pastas de saída são criadas vazias com `mkdir` | As pastas antigas tinham dados de clientes e não são lidas pelo código. Healthcheck (180 s / 5 retries) e `CMD ["python","app.py"]` sem mudança |
| `requirements.txt` | Só pacotes importados, pins originais; removidos ~90 pacotes não usados (streamlit, pandas, altair, GitPython, ruff, tiktoken, `pinecone-client` etc.); adicionados `chardet<6`, `tzdata`, `python-multipart` explícitos | Ver comentários no arquivo |

Nenhuma lógica de negócio foi alterada — em especial o **roteamento JEV (Pinecone → JEV → GPT)** está byte a byte igual ao original.

## Riscos / pontos de atenção

- **Chave de API exposta no repo antigo**: `HANDOFF.md` do `zellinho_chat` tem uma `OPENAI_API_KEY` em texto puro. Se esse repo está (ou esteve) em algum Git remoto, **revogar/rotacionar a chave** na OpenAI. Não foi copiada para cá.
- **65 testes já falhavam no código original** (busca de empresa por cidade sem `municipios_br.json`, veredito de documentos, `requests_no_resume`, `case_evidence` etc.). Existe uma linha de correções separada (`zellu-bugfixes.patch`, que inclui `src/utils/data/municipios_br.json`) que **não** faz parte deste repo, porque não estava no `zellinho_chat-main` e mexeria em lógica. Decidir se entra numa próxima rodada.
- `config.py` mantém padrões com endereços internos (`ZELLU_WEBHOOK_URL` com IP via sslip.io, host R2 em `EVIDENCE_ALLOWED_HOSTS`). Não são credenciais, mas expõem infraestrutura; em produção são sobrescritos por variáveis de ambiente.
- O `Dockerfile` não pôde ser buildado nesta máquina (sem Docker); o boot foi validado em venv Python 3.11 com as mesmas dependências.

## Feedback do Arthur / Próximos testes

- [ ] Conferir se algum script de `scripts/` deve voltar (ex.: `index_guide_docs.py` para reindexar o guia).
- [ ] Decidir sobre o `zellu-bugfixes.patch` (corrige parte das 65 falhas).
- [ ] Build da imagem no Coolify a partir deste repo.
- Anotações:
