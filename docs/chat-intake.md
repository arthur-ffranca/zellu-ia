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

## Variáveis desta frente (padrões no código)

| Variável | Antes (original) | Agora | Item |
|---|---|---|---|
| `INTAKE_CONVERSATION_MODEL` | `gpt-5` | `gpt-5-mini` | CH-01 |
| `INTAKE_REASONING_EFFORT` | `minimal` | `minimal` (sem mudança) | — |
| `INTAKE_VERBOSITY` | `low` | `low` (sem mudança) | — |
| `LEGAL_FALLBACK_EFFORT` | `low` | `low` (sem mudança) | — |
| `COMPANY_DEEP_SEARCH_TIMEOUT_S` | não existia | `30` | CH-08 |
| `COMPANY_DEEP_SEARCH_ON_EMPTY` | não existia | `true` | CH-08 |

## Feedback do Arthur / Próximos testes

- [ ] Medir `llm.intake_turn` em produção depois do redeploy (CH-01).
- [ ] Bolhas aparecem uma vez cada e a resposta final não some (CH-02/03/04).
- [ ] Recusar a empresa 2× seguidas; mandar foto da nota fiscal; empresa só com Instagram (CH-08).
- [ ] Conferir `OPENAI_API_KEY` no Coolify sem prefixo duplicado e rotacionar a chave (CH-09).
- Anotações:
