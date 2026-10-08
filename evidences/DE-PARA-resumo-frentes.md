# DE-PARA: resumo de todas as frentes

Uma linha por mudança. O detalhe (como estava, o que mudamos, por quê, como testamos, resultado e próximo ajuste) está no documento de cada frente, pelo **ID**. Horas em BRT. Legenda de evidência em [`README.md`](README.md#legenda).

**Resultado:** ✅ corrigiu · 🟡 parcial (aplicado, sem validação real) · ❌ não corrigiu · ⏳ aberto · ↪️ substituído

## Agente de voz Raquel (ElevenLabs): [`elevenlabs-raquel.md`](elevenlabs-raquel.md)

| ID | Item | Antes | Depois | Quando | Resultado | Evidência |
|---|---|---|---|---|---|---|
| EL-01 | Voz | Arnold - Br (masculina, *professional*) | Raquel `GDzHdQOi6jjf8zaXhCYD` | 06h54–08h53 | ✅ persona; latência não medida | VERIFICADO (autoria não registrada) |
| EL-02 | Portão de idade | 1ª fala pedia o nome | 1ª fala pede a data de nascimento + bloco no topo do prompt | ~08h53 | ✅ | VERIFICADO (3 ligações reais) |
| EL-03 | `pre_tool_speech` | `auto` | `force` (3 ferramentas; header secreto intacto) | ~08h53 | ✅ (gerou empilhamento, ver EL-04) | VERIFICADO |
| EL-04 | Soft timeout v1 | `-1`, "Hhmmmm...yeah.", máx. 1 | 1,5 s, 8 piadas sem acento, máx. 8 | ~08h53 | ❌ empilhou e pronunciou errado | VERIFICADO (ligações 10h53–11h00) |
| EL-05 | Soft timeout v2 | 1,5 s / máx. 8 / piadas | 2,5 s / máx. 5 / 5 frases acentuadas; "no máximo 1 filler" | 11h21–11h25 | 🟡 | sem ligação real depois |
| EL-06 | Speed / stability / turno | 1.0 / 0.4 / `normal` | 0.9 / 0.5 / `patient` | 11h21–11h25 | 🟡 | sem ligação real depois |
| EL-07 | Data: leitura de volta | ASR "31 de setembro" → agente culpava o cliente | Normalização, leitura por extenso + "sim", perguntar o mês de novo sem culpar | 11h21–11h25 | 🟡 (ok em simulação) | VERIFICADO (antes); simulação (depois) |
| EL-08 | `age_check.py` | 4 formatos | + `D.M.AAAA`, `D/M/AA`, `DDMMAAAA`, `DDMMAA` | 11h22 | ✅ código / ⏳ produção | 24 testes passando |
| EL-09 | Dicionário de pronúncia | nenhum | `zellu-ptbr` (Zellu, DDD, SMS, e-commerce) | 11h21–11h25 | 🟡 | sem teste de áudio |
| EL-10 | Acentos + RITMO E FALA | prompt sem acento | acentuado; números por extenso | 11h21–11h25 | 🟡 | sem teste de áudio |
| EL-11 | Telefone: regra rígida | falha → seguir para o relato | falha → pedir de novo (máx. 2) → site/religar | 11h21–11h25 | ↪️ EL-12 | VERIFICADO (antes: `in_use` 2×) |
| EL-12 | Tentativas | 2, "DDD e número" | 4, dígito a dígito; conta só cada `consultar_telefone` | 11h29–11h33 (+12h25) | ✅ em simulação | `bake1.txt` + relato |
| EL-13 | `end_call` | desligado | ligado → desligado (`silence_end_call_timeout` 30→15) → ligado com roteiro A/B/C | 11h21–12h25 | ✅ em simulação | `bake1.txt` + relato; motivo do "desligado" NÃO VERIFICADO |
| EL-14 | LLM | gemini-3.7-flash / effort low | gpt-5-mini / minimal | 11h33 | ❌ 1/5 | `bake1.txt` |
| EL-15 | Prompt endurecido | 10.101 caracteres | 13.590: fluxo obrigatório, regra de menor, roteiro de encerramento | 11h40–12h25 | ✅ em simulação | relato do placar final |
| EL-16 | LLM final | gpt-5-mini | **claude-sonnet-4-5**, temp 0.3, cascade 4 s | ≤12h25 | ✅ em simulação (25/25, 0,9 s / 2,4 s) | rodada 1 VERIFICADA; placar final RELATO |
| EL-17 | Webhook pós-chamada | autodesativado (401 em 30/09 22h45) | **sem mudança** | — | ⏳ | VERIFICADO (GET 15h05) |
| EL-18 | Pronúncia "Zellu" | alias `Zélu` (É aberto) só para `Zellu` | alias **`Zêlu`** (/ˈze.lu/, "ZÊ-lu") para `Zellu`, `ZELLU`, `zellu`; dicionário v. `rLsI7xkuV7NyKmg0ODSz` | 16h30 | 🟡 | GET 16h30 + amostras em `/workspace/zellu_pron/` (sem ligação real) |

## Chat do consumidor: [`chat-intake.md`](chat-intake.md)

| ID | Item | Antes | Depois | Quando | Resultado | Evidência |
|---|---|---|---|---|---|---|
| CH-01 | Modelo do intake | `gpt-5` (effort `minimal` e verbosity `low` **já existiam**) | `gpt-5-mini` | 06/10 21h40 | 🟡 latência não medida | VERIFICADO (diff) |
| CH-02 | Bolhas de progresso | chat parado | 6 bolhas `is_finished=False`, fora do histórico do LLM | 06/10 21h40–22h | 🟡 | sem teste no app |
| CH-03 | `dispatch_id` nas bolhas | eco nas bolhas (rodada 0) | sem eco (só na resposta final) | 06/10 ~22h | 🟡 | sem teste no app |
| CH-04 | "Estou analisando…" | bolha falsa depois da análise | enviada antes; grupo final 3→2 bolhas | 06/10 21h40 | 🟡 | sem teste no app |
| CH-05 | Ingestão de evidências | leitura completa todo turno | `include_files` só na etapa de anexos | 06/10 21h40 | 🟡 | suíte |
| CH-06 | Tom da resposta | template se a LLM fizesse ≠1 pergunta | resposta da LLM, se não repergunta | 06/10 21h40 | 🟡 | suíte |
| CH-07 | Pedido de documento | mesma frase sempre | 3 variações em rodízio | 06/10 21h40 | 🟡 | suíte |
| CH-08 | Busca da empresa | recusa → "outra referência/CNPJ"; recusados voltavam | escada 3 degraus; recusados e consultas memorizados | 06/10 ~22h | ✅ código / ⏳ produção | 24 testes passando |
| CH-09 | `OPENAI_API_KEY` | valor com `OPENAI_API_KEY=` dentro | orientação: só o valor no Coolify | — | ⏳ não conferido no Coolify | checagem booleana do `.env` |
| CH-10 | Turno com anexo | leitura completa no turno, 1 arquivo e 1 página por vez; LlamaParse `agentic_plus` até 180 s; `LLAMAPARSE_*` sem efeito pelo env | variáveis declaradas; cache por sha256; arquivos e páginas em paralelo; imagem ≤2048 px; atalho da camada de texto e bolha de leitura opcionais | 07/10 17h | 🟡 65,7 s → 23,1 s local (3 anexos); reenvio 0,3 s | benchmark local (LlamaParse simulado, Visão real) + 22 testes |
| CH-11 | Próximos passos (analyst) | `gpt-5` medium fixo | `ANALYST_NEXT_STEPS_MODEL` / `ANALYST_REASONING_EFFORT` (vazios = igual) | 07/10 17h | 🟡 sem ganho estável medido (31–60 s) | OpenAI real, 3 rodadas |
| CH-12 | Mongo da busca de empresa | 3 s por chamada com o Mongo fora; socket sem timeout | disjuntor 30 s; `socketTimeoutMS` 5000; `max_time_ms` no CNPJ | 07/10 17h | ✅ código (turno de busca 15,0 s → 2,0 s) / ⏳ produção | pymongo real + 4 testes |

## Deploy no Coolify: [`deploy-coolify.md`](deploy-coolify.md)

| ID | Item | Antes | Depois | Quando | Resultado | Evidência |
|---|---|---|---|---|---|---|
| CO-01 | MinIO no boot | `bucket_exists()` no boot, sem timeout curto | `MINIO_ENABLED=false` (zero rede); timeout 5 s + 1 retry se ligado | 06/10 22h38 | ✅ | reprodução local: original 8,3 s (503) e preso por minutos (host inacessível) × zelinho-ia 2–3 s |
| CO-02 | Healthcheck | start 40 s, 3 retries | start 180 s, 5 retries | 06/10 22h38 | 🟡 | sem deploy real ainda |
| CO-03 | `--reload` | ligado no Coolify | opt-in (`UVICORN_RELOAD`), produção detecta `COOLIFY_*` | 07/10 (este repo) | ✅ código / ⏳ produção | reprodução: original "Started reloader process… StatReload"; novo sem reloader |
| CO-04 | Dados na imagem | `COPY` de 3 pastas com dados de clientes | pastas vazias criadas no build | 07/10 (este repo) | 🟡 | build Docker não testado |
| CO-05 | `requirements.txt` | ~135 pacotes | só os usados + `chardet<6` | 07/10 (este repo) | ✅ instala, sobe, testes iguais | motivo do `chardet<6` NÃO VERIFICADO |
| CO-06 | Rebuild de 43 min | ~43 min | sem mudança direta | — | ❌ não medido | RELATO |
| CO-07 | Incidente 523 | lento (5–13 s) → 523 | recuperado às 14h50; 15h06 painel 200 em 0,84 s, `/health` 200 | 07/10 | ✅ recuperado / causa ⏳ | RELATO + `curl` 15h06 |

## Bifrost / EDGE_ROUTE: [`bifrost-edge.md`](bifrost-edge.md)

| ID | Item | Antes | Depois | Quando | Resultado | Evidência |
|---|---|---|---|---|---|---|
| BF-01 | Onde marcar | — | env no serviço (ideia do Arthur), em vez de só plugin | 07/10 06h33 | ✅ decidido | RELATO (ligação) |
| BF-02 | `EDGE_ROUTE` | clientes sem `default_headers` | header `X-Bifrost-Route: edge` nos 7 clientes quando `true` | 07/10 06h41 | 🟡 sem efeito até existir `OPENAI_BASE_URL` | teste da função |
| BF-03 | Plugin + Datadog | — | desenho (Go, OTLP, monitores) | 07/10 | ⏳ não implementado | código do Bifrost conferido |
