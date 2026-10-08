# Changelog

Formato: data/hora (BRT) · área · o que mudou · ID no log de-para. Mais recente primeiro. O detalhe de cada item está em [`docs/DE-PARA.md`](docs/DE-PARA.md).

## 2026-10-07

### Documentação
- Docs reescritas em formato de-para (como estava / o que mudamos / por quê / como testamos / resultado / próximo ajuste), com `docs/README.md` (índice e legenda) e `docs/DE-PARA.md` (tabela única).
- Correção de um registro anterior: `INTAKE_REASONING_EFFORT=minimal` e `INTAKE_VERBOSITY=low` **já existiam** no código original. Nossa mudança no intake foi só o modelo `gpt-5` → `gpt-5-mini` (CH-01).

### Infra
- ~14h00 · painel do Coolify lento (5–13 s) → Cloudflare 523. **14h50** recuperado [RELATO]. 15h06: painel 200 em 0,84 s e `/health` 200 (CO-07).

### Repositório (`zelinho-ia`)
- Repo limpo só com o código usado (133 módulos rastreados a partir de `app.py`), mesmas 46 rotas e mesmo resultado da suíte antiga (1.268 passam / 65 falham nos dois). → `docs/arquivos-removidos.md`
- `app.py`: reload do uvicorn opt-in (`UVICORN_RELOAD=true`); produção detectada também por `COOLIFY_*`/`ENVIRONMENT=production` (CO-03).
- `Dockerfile`: não copia mais pastas com dados de clientes (CO-04).
- `requirements.txt` enxuto + `chardet<6` (CO-05).

### Chat do consumidor: latência do turno com anexo (branch `perf/attachment-latency`)
- ~17h00 · leitura de anexos: `LLAMAPARSE_*`, `PADDLEOCR_ENABLED` e `DOCUMENT_PARSE_*` declarados (antes o env era ignorado); cache por sha256 com leitura única; arquivos e páginas em paralelo; imagem da Visão ≤2048 px; atalho da camada de texto e bolha "estou lendo" opcionais (desligados); timeout de 60 s no cliente da Visão; novas linhas `STEP_TIMING`. Local: 65,7 s → 23,1 s com 3 anexos (CH-10).
- ~17h00 · `ANALYST_NEXT_STEPS_MODEL` / `ANALYST_REASONING_EFFORT` para os próximos passos (vazios = igual) (CH-11).
- ~17h00 · Mongo da busca de empresa: disjuntor de 30 s, `socketTimeoutMS` e `max_time_ms` no CNPJ. Turno de busca com Mongo fora: 15,0 s → 2,0 s (CH-12).
- Benchmark `scripts/bench_attachment_latency.py`; suíte 194 → 216 passando.

### Agente de voz Raquel (ElevenLabs)
- 16h30 · pronúncia "Zellu" → "ZÊ-lu": alias `Zêlu` para Zellu/ZELLU/zellu, dicionário `zellu-ptbr` v. `rLsI7xkuV7NyKmg0ODSz` (EL-18).
- 12h25 · LLM final **claude-sonnet-4-5** (EL-16); prompt endurecido, 13.590 caracteres (EL-15).
- 11h56 · bake-off, rodada 1 (`bake1.txt`) (EL-16).
- 11h33 · LLM gpt-5-mini: reprovado, 1/5 (EL-14).
- 11h29–11h33 · 4 tentativas, dígito a dígito (EL-12).
- 11h25–11h29 · `end_call` desligado (`silence_end_call_timeout` 30→15) e religado com roteiro de fechamento (EL-13).
- 11h22 · **Rodada 4** (`zellinho_age_check.zip`): `age_check.py` com mais formatos de data (EL-08).
- 11h21–11h25 · polimento: speed 0.9, stability 0.5, `patient`, soft timeout 2,5 s com frases acentuadas, dicionário `zellu-ptbr`, normalização e leitura de volta de data e telefone, regra rígida do telefone (EL-05, 06, 07, 09, 10, 11).
- 10h53–11h00 · 3 ligações reais expõem: ASR "31 de setembro", fillers empilhados e sem acento, `in_use` ignorado.
- ~08h53 · portão de idade na 1ª fala, fillers v1, `pre_tool_speech: force` (EL-02, 03, 04).
- 06h54–08h53 · voz Arnold → Raquel (EL-01).
- Diagnóstico: webhook pós-chamada autodesativado desde 30/09 22h45 (401) (EL-17, aberto).

### Serviço de IA
- 06h41 · **Rodada 3** (`zellinho_edge_route.zip`): `EDGE_ROUTE`/`EDGE_ROUTE_HEADER` e `llm_default_headers()` em 7 clientes OpenAI (BF-02).
- 06h33 · decisão: marcar a rota por env no serviço, ideia do Arthur (BF-01).

## 2026-10-06

### Serviço de IA
- 22h38 · **Rodada 2** (`zellinho_minio_healthcheck.zip`): `MINIO_ENABLED=false`, timeout 5 s + 1 retry; healthcheck `start-period=180s`, `retries=5` (CO-01, CO-02).
- ~22h00 · **Rodada 1** (`zellinho_chat_pr_files.zip`): escada de busca da empresa em 3 degraus (CH-08); bolhas sem `dispatch_id` (CH-03); + 2 bolhas de busca.
- 21h40 · **Rodada 0** (latência/humanização): intake `gpt-5` → `gpt-5-mini` (CH-01); bolhas de progresso (CH-02); "analisando" antes da análise (CH-04); ingestão leve (CH-05); resposta da LLM em vez do template (CH-06); pedido de documento variado (CH-07).
- 21h29 · `.env` de referência com `OPENAI_API_KEY` de prefixo duplicado (CH-09).

## Feedback do Arthur / Próximos testes

- Anotações:
