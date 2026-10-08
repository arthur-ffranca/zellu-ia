# Chat do consumidor (intake e busca de empresa): log de-para

O intake é a primeira parte do atendimento no chat. O ZelinhU entende o relato, identifica a empresa (com CNPJ), coleta datas, valores e objetivo, e prepara o caso. Arquivos principais: `src/open_dots/intake.py`, `src/open_dots/agent.py`, `src/open_dots/memory.py`, `src/services/staged_intake_flow.py`, `src/services/company_intake.py` e `src/services/company_search_*`.

O roteamento jurídico **Pinecone → JEV → GPT não foi tocado**. Legenda de evidência em [`README.md`](README.md#legenda).

## Fontes

| Fonte | Data/hora (BRT) | Uso |
|---|---|---|
| Zip original do repositório (`zellinho_chat-main`, arquivos de 2026-10-06 14h16) | 06/10 | **"Como estava"** de todos os itens (base de comparação) |
| Rodada 0, latência e humanização (zip com `staged_intake_flow.py`, `config.py`, `intake_progress.py`, `open_dots/*`, `main.py`…) | 06/10 21h40 | Primeira versão das mudanças |
| Rodada 1 intermediária (zip) | 06/10 22h06 | Escada de busca, 1ª versão |
| `zellinho_chat_pr_files.zip` (rodada 1 final) | 06/10 ~22h | Versão entregue |
| `.env` preparado para latência (comentários lidos; **valores não**) | 06/10 21h06 | Variáveis sugeridas |
| Este repositório (`git diff` contra o zip original) | 07/10 | **"O que mudamos"** |
| Execuções de teste do Don | 06/10 21h52–21h59 e 07/10 | **"Como testamos"** |

---

## CH-01 · Modelo do intake: `gpt-5` → `gpt-5-mini`

| | |
|---|---|
| **Como estava** | `config.py`: `INTAKE_CONVERSATION_MODEL: str = "gpt-5"`; `staged_intake_flow.py`: `env("INTAKE_CONVERSATION_MODEL", default="gpt-5")`; `.env.example`: `INTAKE_CONVERSATION_MODEL=gpt-5`. O código original **já tinha** `INTAKE_REASONING_EFFORT="minimal"`, `INTAKE_VERBOSITY="low"` e `LEGAL_FALLBACK_EFFORT="low"`, com o comentário "gpt-5 sem effort explícito roda em 'medium' (10-30s por turno)". [VERIFICADO: zip original] |
| **O que mudamos** | Padrão `gpt-5` → **`gpt-5-mini`** nos três lugares (`config.py`, `staged_intake_flow.py`, `.env.example`; rodada 0, 21h40). `effort`/`verbosity` **não** foram mudados por nós: já vinham assim. O `.env` preparado (21h06) sugeria `INTAKE_CONVERSATION_MODEL=gpt-5-mini`, `OPENAI_MODEL=gpt-5`, `OPENAI_MODEL_FAST=gpt-4o-mini`, `INTAKE_REASONING_EFFORT=minimal`, `INTAKE_VERBOSITY=low`. |
| **Por quê** | Latência do chat: cada turno do intake é extração + resposta curta, e um modelo menor responde mais rápido. |
| **Como testamos** | Suíte antiga (1.333 testes): o mesmo conjunto de falhas pré-existentes antes e depois (`/tmp/baseline_fail.txt` vs `/tmp/r1_fail.txt`/`r2_fail.txt`, 06/10 21h52–21h59). Não há teste de latência. |
| **Resultado** | **Parcial**: aplicado e reversível por env sem deploy de código. **Ganho de latência em produção não medido.** [NÃO VERIFICADO] |
| **Próximo ajuste** | Depois do redeploy, medir `llm.intake_turn` no log (`src/utils/step_timing.py`, limiar `STEP_TIMING_SLOW_MS`). Sem `logging.basicConfig`, só saem os passos lentos (WARNING). |

## CH-02 · Bolhas de progresso no chat (`intake_progress.py`, novo)

| | |
|---|---|
| **Como estava** | Nenhuma mensagem intermediária: o cliente via o chat parado durante o LLM do intake e durante a busca da empresa (Mongo → Casa dos Dados → Scrapling). [VERIFICADO: o módulo não existia no original] |
| **O que mudamos** | Novo `src/services/intake_progress.py`: `send_progress(state, msg)` envia uma bolha com `is_finished=False` pelo `zellu_client`, em modo *fire-and-forget* (uma falha só gera WARNING e nunca quebra o webhook). A bolha **não** entra em `state['messages']`, para não sujar o histórico do LLM. Mensagens e onde disparam: `MSG_BEFORE_LLM` "Beleza, tô lendo o que você contou…" (`open_dots/intake.py`, antes de `understand_turn`); `MSG_BEFORE_COMPANY_SEARCH` "Vou localizar a empresa com o que você passou…" e `MSG_CACHE_MISS` "Ainda buscando… um instante." (`company_intake.py`); `MSG_ANALYZING` "Perfeito! Estou analisando seu caso agora…" (`open_dots/agent.py`). Rodada 1: + `MSG_DEEP_SEARCH` e `MSG_REFERENCE_SEARCH` (CH-07). |
| **Por quê** | Humanização: mostrar que a mensagem foi recebida enquanto o sistema trabalha. |
| **Como testamos** | Suíte de testes do repo (194 passando hoje). Não há teste dedicado às bolhas, e **não houve teste no app real**. |
| **Resultado** | **Parcial**: implementado. Comportamento no app não verificado. [NÃO VERIFICADO no app] |
| **Próximo ajuste** | Conferir no app que cada bolha aparece **uma vez** e que a resposta final não é descartada (ver CH-03). |

## CH-03 · Bolhas sem `dispatch_id` (correção dentro da própria rodada)

| | |
|---|---|
| **Como estava** | Na rodada 0 (21h40), `src/main.py` gravava `state['dispatch_id']` para ecoar o despacho também nas bolhas intermediárias. [VERIFICADO: zip 21h40] |
| **O que mudamos** | Na versão final, o eco saiu: as bolhas vão **sem `dispatch_id`**. Comentário no código: "o eco do despacho (contrato 20260807) fica só na resposta final do turno, para o app não tratar a bolha como 'a' resposta e descartar a final como duplicada". |
| **Por quê** | Evitar que o app trate a bolha como resposta do turno e descarte a resposta real. |
| **Como testamos** | Leitura do contrato descrito no código. Sem teste no app. |
| **Resultado** | **Parcial**: corrigido no código. [NÃO VERIFICADO no app] |
| **Próximo ajuste** | Mesmo teste de CH-02. |

## CH-04 · Fim do atraso falso de "Estou analisando…"

| | |
|---|---|
| **Como estava** | `src/main.py`, ao concluir o intake, mandava 3 bolhas com `delays = [0, 1500, 2500]`: "Perfeito, {nome}! Recebi todas as informacoes." / "Estou analisando seu caso agora..." / "Pronto! Seu chamado foi criado…". O "analisando" aparecia **depois** que a análise já tinha acabado, só como atraso cosmético. `hand_types = [None, None, SNAP]`. [VERIFICADO] |
| **O que mudamos** | `MSG_ANALYZING` passou a ser enviada **antes** de `classify`/`analyze`/`documents` (`open_dots/agent.py`). O grupo final ficou com 2 bolhas: `delays = [0, 1500]`, `hand_types = [None, SNAP]`. |
| **Por quê** | O aviso tem que vir enquanto o trabalho acontece, não depois. |
| **Como testamos** | Suíte do repo. Sem teste no app. |
| **Resultado** | **Parcial**: implementado. [NÃO VERIFICADO no app] |
| **Próximo ajuste** | Conferir no app a ordem: "analisando" → (espera real) → "Recebi…" → "Pronto!". |

## CH-05 · Ingestão de evidências mais leve durante o intake

| | |
|---|---|
| **Como estava** | `open_dots/agent.py`: `await ingest_case_evidence(state)` em todo turno, com leitura completa dos arquivos. [VERIFICADO] |
| **O que mudamos** | `ingest_case_evidence(state, include_files=at_attachments)`, em que `at_attachments` é verdadeiro só quando já há arquivos, documentos pedidos ou o estágio é `ATTACHMENTS`. Segue o que a própria docstring da função pede ("include_files=False no intake"). |
| **Por quê** | Latência: não reler anexos em cada turno de conversa. |
| **Como testamos** | Suíte do repo. |
| **Resultado** | **Parcial**: implementado, ganho não medido. [NÃO VERIFICADO] |
| **Próximo ajuste** | Medir o tempo do turno com e sem anexos. |

## CH-06 · Resposta da LLM preferida ao template frio

| | |
|---|---|
| **Como estava** | `open_dots/memory.py::next_missing_question`: se a resposta limpa da LLM tivesse um número de "?" diferente de 1, o sistema usava o **template fixo** (`fallback`). [VERIFICADO] |
| **O que mudamos** | `if not cleaned or cleaned.count('?') != 1 or …: return fallback` → `if cleaned and not any(<gate já preenchido>): return cleaned`. Agora vale a resposta não vazia da LLM, desde que não repergunte algo já respondido. |
| **Por quê** | Humanização: o template soava robótico. |
| **Como testamos** | Suíte do repo (`test_ordem_do_intake.py` e outros). |
| **Resultado** | **Parcial**: implementado. Percepção no chat não avaliada. |
| **Próximo ajuste** | Revisar 5 conversas reais depois do deploy. |

## CH-07 · Pedido de documento sem repetir a mesma frase

| | |
|---|---|
| **Como estava** | Toda vez: "Para continuar, preciso de pelo menos um documento do caso: nota fiscal, laudo, comprovante, contrato, fotos ou áudio. Use o botão de anexos para enviar." [VERIFICADO] |
| **O que mudamos** | 3 variações em rodízio com `documents_nag_count` ("Ainda preciso de pelo menos um documento…", "Sem anexo fica difícil continuar…", "Pode enviar pelo menos um documento…"). O contador zera quando o pedido começa. |
| **Por quê** | Repetir a mesma frase parece bug ou loop. |
| **Como testamos** | Suíte do repo. |
| **Resultado** | **Parcial**: implementado. Não há teto de pedidos (continua em rodízio). |
| **Próximo ajuste** | Avaliar um teto e oferecer seguir sem anexo. |

## CH-08 · Escada de busca da empresa em 3 degraus

| | |
|---|---|
| **Como estava** | `open_dots/intake.py`: quando o cliente escolhia "nenhuma" no dropdown, `clear_form` + "Qual outra referência ajuda a identificar a empresa? Pode ser loja, shopping, bairro ou cidade."; ao recusar a confirmação, "Informe outra referência da empresa ou o CNPJ para corrigir a busca.". Não havia memória de CNPJs recusados (eles podiam voltar ao dropdown) nem de consultas já feitas. 1ª busca vazia → `_firecrawl_fallback_no_results` direto. Casa dos Dados só com `tipo_busca: 'exata'`, todos os termos obrigatórios, `limit` fixo em 5 e `timeout` fixo em 15 s. [VERIFICADO] |
| **O que mudamos** | Novo `src/services/company_search_ladder.py`. **Degrau 1**, com o que o cliente contou: Mongo → Casa dos Dados → Scrapling. **Degrau 2**, busca profunda na web: dispara quando o cliente recusa (no form ou em texto, "não é nenhuma dessas") ou quando o degrau 1 vem vazio (`COMPANY_DEEP_SEARCH_ON_EMPTY=true`). Tem orçamento próprio (`COMPANY_DEEP_SEARCH_TIMEOUT_S=30`), não repete consultas e não reoferece CNPJ recusado. **Degrau 3**, pedir referência: CNPJ, site/Instagram, endereço, nome na nota ou foto da nota fiscal, com 3 frases que variam e teto; esgotado, cai no `cnpj_or_retry` que já existia. Estado novo em `agents/state.py`: `company_search_attempt`, `company_rejected_cnpjs`, `company_search_queries`, `company_awaiting_reference`, `company_reference_asks`, `company_reference_hints`. `casa_dos_dados_search.search_companies(..., fuzzy=False, limit=5, timeout=15)`: com `fuzzy`, usa `tipo_busca: 'radical'` e basta 1 termo relevante. `scrapling_company_search.py`: mais diretórios de CNPJ (cnpja, econodata, cnpj.info…), desembrulha links do Bing (`/ck/a?…u=a1<base64>`) e ignora sites que não são da empresa. `company_intake.py` filtra recusados e aceita `select_message`/`confirm_prefix`. |
| **Por quê** | O cliente comum não sabe o CNPJ e abandonava quando o fluxo reoferecia as mesmas empresas ou pedia CNPJ logo de cara. |
| **Como testamos** | Novo `tests/test_escada_busca_empresa.py`: **24 testes passando** (12 funções, algumas parametrizadas): recusa em texto; recusa → busca profunda sem reoferecer recusados; 2ª recusa pede referência sem repetir a pergunta; "não" na confirmação única também sobe a escada; 1ª tentativa vazia aprofunda direto; busca profunda não repete consultas; resultado salvo no Mongo; link do Bing desembrulhado; CNPJ do rodapé do site entra sem virar "verificado"; CNPJ da foto da nota é usado e a chave da NF-e não vira CNPJ; e-mail não é confundido com site. [VERIFICADO] |
| **Resultado** | **Corrigiu no código e nos testes.** **Sem teste em produção** (deploy pendente). [NÃO VERIFICADO em produção] |
| **Próximo ajuste** | Testar no chat real: recusar 2× seguidas; mandar foto da nota; empresa só com Instagram. |

## CH-09 · `OPENAI_API_KEY` com prefixo duplicado

| | |
|---|---|
| **Como estava** | No `.env` de referência recebido (06/10 21h29), o **valor** de `OPENAI_API_KEY` começa com o texto `OPENAI_API_KEY=`, ou seja, a linha era `OPENAI_API_KEY=OPENAI_API_KEY=sk-…`. Checagem booleana feita sem imprimir o valor: prefixo duplicado = **sim**, e o resto do valor tem o formato de chave de projeto da OpenAI. [VERIFICADO] |
| **O que mudamos** | **Nada no código.** É configuração: no Coolify, o campo de valor deve conter só `sk-…`. O `.env` preparado às 21h06 já veio sem o prefixo duplicado (checagem booleana). |
| **Por quê** | A OpenAI recebe a string inteira como chave, e isso dá 401 (*invalid API key*). |
| **Como testamos** | Só a checagem do arquivo. **O 401 não foi visto em log** (é a consequência esperada), e **não sabemos** se o valor no Coolify tem o mesmo problema. [NÃO VERIFICADO no Coolify] |
| **Resultado** | **Não corrigido por nós** (fora do nosso acesso). Pendente de conferência. |
| **Próximo ajuste** | No Coolify → Environment Variables: conferir `OPENAI_API_KEY` (e todas as outras) colando **só o valor**. Rotacionar a chave, porque ela aparece em texto puro no `HANDOFF.md` do repo antigo. |

## CH-10 · Turno com anexo: leitura configurável, paralela e com cache

| | |
|---|---|
| **Como estava** | O turno que recebe anexo roda a leitura completa **dentro do turno** (`open_dots/agent.py` → `ingest_case_evidence(include_files=True)` sempre que há arquivo), apesar de as docstrings dizerem "pós-caso". Cada arquivo, em sequência: download (cliente HTTP novo por arquivo) → preflight/ClamAV → **LlamaParse `agentic_plus`** (upload + job + polling a cada 1,5 s, `confidence_score_effort=high`, limite **180 s**) → páginas ruins (menos de 40 caracteres ou confiança < 0,72) uma a uma: render 3x → PaddleOCR (serializado, modelos baixados no 1º uso porque o `Dockerfile` não os inclui) → Visão `gpt-4o` `detail=high` (25 s). `LLAMAPARSE_*`, `PADDLEOCR_ENABLED` e `DOCUMENT_PARSE_*` eram lidos com `getattr(settings, NOME, padrão)` **sem declaração no `Settings`**: como o `Settings` ignora variáveis não declaradas, **o `.env` não conseguia mudar nada disso**. O cliente síncrono da Visão usava o timeout padrão da lib (600 s); o `wait_for` de 25 s solta o turno, mas a thread continua presa no executor compartilhado. [VERIFICADO no código] |
| **O que mudamos** | (1) As variáveis acima foram declaradas em `config.py`, com os **mesmos padrões** (agora o `.env` funciona). (2) Novo `src/services/evidence_parse_cache.py`: a leitura é guardada pelo sha256 do conteúdo (mais tipo, extensão e tier na chave); o mesmo arquivo não paga a cascata de novo (URL renovada, reenvio, outro turno ou chat); duas chegadas simultâneas viram uma leitura só; falha e timeout **não** entram no cache. `EVIDENCE_PARSE_CACHE_ENABLED=true`. (3) Arquivos do mesmo turno são baixados, auditados e lidos em paralelo (`EVIDENCE_FILE_CONCURRENCY=3`); o laço que monta os registros continua sequencial e igual, e o malware continua sem chegar ao leitor. (4) Páginas reparadas em paralelo (`DOCUMENT_PAGE_CONCURRENCY=3`); o Paddle segue serializado pelo lock, e o ganho vem da Visão. (5) A imagem vai à Visão reduzida a 2048 px no lado maior, que é o que a OpenAI usa em `detail=high` (`DOCUMENT_VISION_MAX_SIDE_PX=2048`). (6) Opcionais, **desligados**: atalho da camada de texto do PDF, que pula o LlamaParse quando todas as páginas têm texto nativo (`DOCUMENT_TEXT_LAYER_FAST_PATH`), e a bolha "Recebi seus anexos. Estou lendo o conteúdo…" (`EVIDENCE_PROGRESS_MESSAGE_ENABLED`). (7) `DOCUMENT_ANALYZER_TIMEOUT_S=60` no cliente da Visão. (8) Um `ChatModel` por loop no `build_all_documents`: as 3 chamadas paralelas dividem a conexão. (9) Novas linhas `STEP_TIMING`: `evidence.download/preflight/read/llamaparse/text_layer/render_page/paddle_page/vision_page/parallel_warm`, `llm.<módulo>` e `rag.search`. |
| **Por quê** | Arthur relatou ~3 min na etapa do formulário/resumo com anexo. O limite de 180 s do LlamaParse, somado ao fallback por página, explica sozinho um turno de 3 min com **um** arquivo. |
| **Como testamos** | Novo `tests/test_attachment_latency.py` (22 testes): cache (reuso, falha não cacheada, leitura única simultânea, chave muda com o tier), páginas em paralelo com a mesma saída do sequencial, atalho da camada de texto (ligado, desligado, PDF escaneado), redução de imagem, registros idênticos com 1 e 3 arquivos em paralelo, malware sem leitor, padrões antigos preservados e env agora lido. Benchmark `scripts/bench_attachment_latency.py` com arquivos fictícios: LlamaParse **simulado** (20 s por arquivo; nada foi enviado ao LlamaParse), Visão `gpt-4o` **real**, Paddle ausente. [VERIFICADO local] |
| **Resultado** | **Turno com 3 anexos: 65,7 s → 23,1 s** (−65%). Mesmo arquivo de novo: **0,3 s**. Com Paddle simulado a 6 s/página: 79,2 s → 29,7 s. Com 1 arquivo o ganho é pequeno (o LlamaParse continua no caminho); para esse caso valem as flags abaixo. Suíte: 194 → **216 passando**. [VERIFICADO local; NÃO VERIFICADO em produção] |
| **Próximo ajuste** | Em produção: `LLAMAPARSE_TIMEOUT_S=45`, `DOCUMENT_TEXT_LAYER_FAST_PATH=true`, `EVIDENCE_PROGRESS_MESSAGE_ENABLED=true`; testar `LLAMAPARSE_TIER` mais rápido com 5 documentos reais (qualidade × tempo). Baixar os modelos do Paddle no build (`Dockerfile`). Ler o anexo em segundo plano com prazo no turno (ainda não implementado). |

## CH-11 · "Próximos passos" do analyst: modelo e esforço configuráveis

| | |
|---|---|
| **Como estava** | No turno de confirmação ("Sim, o relato está correto") rodam, em sequência: classify (`gpt-4o-mini`) → RAG + roteamento JEV (intocado) → `LegalAnalysis._generate_next_steps` com `OPENAI_MODEL=gpt-5` (sem `reasoning_effort`, então `medium`, mais até 12.000 caracteres de anexos no prompt) → documento HTML/PDF → `build_all_documents` (3× `gpt-4o-mini` em paralelo). [VERIFICADO no código] |
| **O que mudamos** | `ANALYST_NEXT_STEPS_MODEL` e `ANALYST_REASONING_EFFORT`, ambos vazios por padrão (comportamento igual). Valem só para a chamada de próximos passos; a cópia do LLM herda chave, temperatura e registro de custo. O roteamento Pinecone → JEV → GPT não foi tocado. |
| **Por quê** | Foi a etapa LLM mais lenta medida no turno de confirmação. |
| **Como testamos** | OpenAI real, prompt fictício, 3 rodadas: `gpt-5` medium **31–60 s**, low 32–49 s, minimal 35–36 s; `gpt-5` **sem** o bloco de anexos 20,5–21,8 s; `gpt-5-mini` low 18–30 s. Classify 1,7–1,8 s; `build_all_documents` 5,7–6,7 s. Teste unitário da cópia do LLM. [VERIFICADO local] |
| **Resultado** | **Parcial.** O esforço de raciocínio **não** trouxe ganho estável. O que mais pesa é o bloco de anexos no prompt (+10 a 12 s) e a variação do próprio `gpt-5`. Flags entregues desligadas. |
| **Próximo ajuste** | Medir `STEP_TIMING step=llm.analyst` em produção. Avaliar `ANALYST_NEXT_STEPS_MODEL=gpt-5-mini` com a qualidade dos passos, ou mandar ao analyst só o resumo dos anexos. |

## CH-12 · Mongo da busca de empresa: disjuntor e timeout de socket

| | |
|---|---|
| **Como estava** | `company_lookup_cache.py` usa o pymongo síncrono, sempre via `asyncio.to_thread` (não trava o event loop, mas o turno espera cada chamada em sequência). `serverSelectionTimeoutMS=connectTimeoutMS=3000`, **sem `socketTimeoutMS`** (resposta que nunca chega prende a thread para sempre); `find_company_by_cnpj` sem `max_time_ms`; nenhuma memória de falha, então cada chamada espera 3 s de novo. Chamadas por etapa: **busca da empresa** = 1 `find_company_lookup_results` + até 5 `find_brand_icon` (uma por raiz de CNPJ, teto de 4 s no total; a thread segue rodando depois do teto) + 1 `save_company_lookup_results`; **escolha/confirmação** = 1 save (+ 1 `find_company_by_cnpj` da matriz se for filial); **busca profunda** = até 3 lookups + 1 save + ícones; **CNPJ digitado** = 1 `find_company_by_cnpj`; **turnos seguintes com matriz pendente** (até 3, inclusive anexo e confirmação) = 1 `find_company_by_cnpj` cada. Turnos de anexo e de confirmação **não** consultam o Mongo, exceto com matriz pendente. [VERIFICADO no código] |
| **O que mudamos** | Disjuntor: depois de falha de rede, seleção ou prazo (`ServerSelectionTimeoutError`, `AutoReconnect`, `NetworkTimeout`, `ExecutionTimeout`…), as consultas do processo pulam o Mongo por `MONGODB_BREAKER_COOLDOWN_S=30` e devolvem o mesmo vazio que a falha já devolvia. Erro de consulta não abre o disjuntor. `MONGODB_SOCKET_TIMEOUT_MS=5000`. `max_time_ms` no `find_company_by_cnpj`. O log de falha agora traz `ms=`. |
| **Por quê** | Com o Mongo fora, cada consulta custa 3 s, e a busca da empresa faz várias. |
| **Como testamos** | pymongo real apontando para IP não roteável: **cada chamada 3,0 s** (as 4 funções). Turno de busca com a busca externa simulada em 2 s: **15,0 s → 2,0 s** com o disjuntor aberto. 4 testes unitários (pula depois da 1ª falha; desligado = antigo; erro de consulta não abre; `socketTimeoutMS` no cliente). [VERIFICADO local] |
| **Resultado** | **Corrigiu no código e nos testes**: só a 1ª consulta da janela paga 3 s. **Não** é causa dos ~3 min do formulário (ver acima). [NÃO VERIFICADO em produção] |
| **Próximo ajuste** | Conferir no log se aparece `[COMPANY-LOOKUP-CACHE] … ServerSelectionTimeoutError ms=3000`. Se o Mongo de produção estiver bom, nada a fazer. |

## Medição do turno com anexo (07/10, local)

Arquivos fictícios: nota fiscal em PDF (2 páginas com texto), laudo escaneado em PDF (2 páginas de imagem) e foto de 12 MP. LlamaParse **simulado** em 20 s por arquivo. Visão e LLMs **reais** (OpenAI). JEV e Pinecone não chamados.

| Etapa | Antes | Depois (padrões do branch) | Observação |
|---|---|---|---|
| Download (3 arquivos) | 0,9 s em série | 0,3 s em paralelo | simulado 300 ms/arquivo |
| Preflight/ClamAV | ~0 | ~0 | sem clamd local; em produção até 8 s/arquivo se o host não responder |
| LlamaParse | 60 s (3 × 20 s em série) | ~20 s (paralelo) | **limite real 180 s/arquivo** |
| Render + Paddle | 0,2 s + ausente | igual | com Paddle a 6 s/página: 12 s em série nos dois |
| Visão `gpt-4o` (2 páginas) | 4,5 s em série | ~2,4 s em paralelo | real |
| **Turno com anexo** | **65,7 s** | **23,1 s** | reenvio do mesmo arquivo: **0,3 s** |
| Classify `gpt-4o-mini` | 1,8 s | igual | real |
| Próximos passos `gpt-5` | 31–60 s | igual (flags desligadas) | real; ver CH-11 |
| `build_all_documents` (3× paralelo) | 5,7–6,7 s | igual | real |
| Mongo fora do ar, por chamada | 3,0 s | 3,0 s só na 1ª; depois ~0 | real (pymongo) |
| Mongo fora do ar, turno de busca | 15,0 s | 2,0 s | busca externa simulada em 2 s |
| Pior caso pelo código, 1 anexo | 180 s (LlamaParse) + fallback | igual com os padrões; ≤45 s + fallback com `LLAMAPARSE_TIMEOUT_S=45` | explica os ~3 min |

## Variáveis desta frente (padrões no código)

| Variável | Antes (original) | Agora | Item |
|---|---|---|---|
| `INTAKE_CONVERSATION_MODEL` | `gpt-5` | `gpt-5-mini` | CH-01 |
| `INTAKE_REASONING_EFFORT` | `minimal` | `minimal` (sem mudança) | — |
| `INTAKE_VERBOSITY` | `low` | `low` (sem mudança) | — |
| `LEGAL_FALLBACK_EFFORT` | `low` | `low` (sem mudança) | — |
| `COMPANY_DEEP_SEARCH_TIMEOUT_S` | não existia | `30` | CH-08 |
| `COMPANY_DEEP_SEARCH_ON_EMPTY` | não existia | `true` | CH-08 |
| `LLAMAPARSE_TIER` / `_TIMEOUT_S` / `_POLL_INTERVAL_S` / `_CONFIDENCE_EFFORT` | `agentic_plus` / 180 / 1,5 / `high` (sem efeito pelo env) | iguais, agora ajustáveis | CH-10 |
| `PADDLEOCR_ENABLED`, `DOCUMENT_PARSE_MIN_PAGE_CHARS`, `DOCUMENT_PARSE_MIN_CONFIDENCE` | `true` / 40 / 0,72 (sem efeito pelo env) | iguais, agora ajustáveis | CH-10 |
| `DOCUMENT_RENDER_SCALE` | 3.0 fixo | 3.0 | CH-10 |
| `DOCUMENT_PAGE_CONCURRENCY` | 1 (laço) | 3 | CH-10 |
| `EVIDENCE_FILE_CONCURRENCY` | 1 (laço) | 3 | CH-10 |
| `EVIDENCE_PARSE_CACHE_ENABLED` | não existia | `true` | CH-10 |
| `DOCUMENT_VISION_MAX_SIDE_PX` | sem redução | 2048 | CH-10 |
| `DOCUMENT_TEXT_LAYER_FAST_PATH` | não existia | `false` (recomendado `true`) | CH-10 |
| `EVIDENCE_PROGRESS_MESSAGE_ENABLED` | não existia | `false` (recomendado `true`) | CH-10 |
| `DOCUMENT_ANALYZER_TIMEOUT_S` | 600 (padrão da lib) | 60 | CH-10 |
| `ANALYST_NEXT_STEPS_MODEL` / `ANALYST_REASONING_EFFORT` | não existiam | vazios (= antigo) | CH-11 |
| `MONGODB_BREAKER_COOLDOWN_S` | não existia | 30 | CH-12 |
| `MONGODB_SOCKET_TIMEOUT_MS` | infinito | 5000 | CH-12 |

## Feedback do Arthur / Próximos testes

- [ ] Medir `llm.intake_turn` em produção depois do redeploy (CH-01).
- [ ] Bolhas aparecem uma vez cada e a resposta final não some (CH-02/03/04).
- [ ] Recusar a empresa 2× seguidas; mandar foto da nota fiscal; empresa só com Instagram (CH-08).
- [ ] Conferir `OPENAI_API_KEY` no Coolify sem prefixo duplicado e rotacionar a chave (CH-09).
- [ ] Mandar o log de **um** turno lento com anexo (chat_id, hora de envio e hora da resposta), filtrando `STEP_TIMING`, `[CASE-EVIDENCE]`, `[EVIDENCE-CACHE]`, `[ANEXOS]`, `[WRITER]`, `[COMPANY-LOOKUP-CACHE]` e `[ANALYST]` (CH-10/11/12).
- [ ] Ligar `LLAMAPARSE_TIMEOUT_S=45`, `DOCUMENT_TEXT_LAYER_FAST_PATH=true` e `EVIDENCE_PROGRESS_MESSAGE_ENABLED=true` e repetir o teste (CH-10).
- [ ] Testar `LLAMAPARSE_TIER` mais rápido com 5 documentos reais (CH-10).
- Anotações:
