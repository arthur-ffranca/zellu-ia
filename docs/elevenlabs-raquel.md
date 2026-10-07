# Agente de voz "Raquel" (ElevenLabs): log de-para

Agente de pré-atendimento telefônico da Zellu (`Zellu - Pre-atendimento telefonico`, número Twilio). Ele confirma a maioridade, pede consentimento de gravação, coleta nome, telefone validado, relato e empresa, confirma um resumo e encerra.
Ferramentas: `validar_idade` → `POST /phone/age-check`, `consultar_telefone` → `POST /phone/identity/lookup`, `confirmar_codigo` → `POST /phone/identity/verify` e a system tool `end_call`.

Todas as horas são de **2026-10-07, BRT**, salvo indicação. Legenda de evidência em [`README.md`](README.md#legenda).

## Fontes usadas neste documento

| Fonte | O que prova | Onde está |
|---|---|---|
| Snapshot 06h54 (`elevenlabs_agent_current.json`) | Estado inicial do agente | Box do Don (não versionado: contém o header `x-api-key` das ferramentas) |
| Backups automáticos `elevenlabs_agent_backup_pre_*.json` (08h53, 11h21, 11h25, 11h26, 11h29, 11h33, 11h39) | Estado **antes** de cada mudança | Box do Don (idem) |
| GET ao vivo do agente (15h05) | Estado atual | Snapshot redigido em [`anexos/raquel-agent-config.redacted.json`](anexos/raquel-agent-config.redacted.json) e [`anexos/raquel-prompt.md`](anexos/raquel-prompt.md) |
| Scripts de PATCH (`s2.py`, `s3.py`, `e.py`, `f.py`, `g2.py`, `j.py`, `newprompt.py`) | Texto exato trocado (antes → depois) | Box do Don, `/tmp` |
| Ligações reais do dia (API `GET /v1/convai/conversations`): 10h53, 10h54, 11h00 | Comportamento antes do polimento | Lidas via API; nomes, telefones e datas de nascimento **omitidos** aqui |
| `elevenlabs_polish_diff.md` (11h19) | Diagnóstico das ligações e proposta de polimento | Box do Don |
| `bake.py` + `bake1.txt` (11h56) | Método e 1ª rodada do bake-off | Box do Don, `/tmp` |
| Ligações de voz Arthur ↔ Don (06h33, 06h50) | Pedidos do Arthur | Transcrição da sessão |

> ⚠️ O backup `pre_endcall_off` (11h25) foi **reconstruído**: o arquivo original foi sobrescrito depois do PATCH, e o objeto `end_call` foi refeito a partir de um GET registrado em log. Os campos de TTS, turn e prompt batem com os snapshots vizinhos.

> Antes do primeiro snapshot (06h54), o Arthur já tinha mexido sozinho no agente pela CLI ("mudei algumas coisas pra melhorar a parte do português… o timeout também eu diminuí… melhorou pra caramba", ligação das 06h50). Isso **não** está neste log: não há registro do antes/depois desses ajustes. [RELATO]

---

## Linha do tempo (resumo)

| Hora (BRT) | Mudança | Prompt (caracteres) | LLM |
|---|---|---|---|
| 06h54 | Snapshot inicial | 6.294 | gemini-3.7-flash |
| 06h54–08h53 | Voz Arnold → Raquel | 6.294 | gemini-3.7-flash |
| ~08h53 | Portão de idade, `first_message`, fillers v1, `pre_tool_speech: force` | 7.156 | gemini-3.7-flash |
| 10h53–11h00 | **3 ligações reais** expõem os problemas (data, fillers, telefone) | 7.156 | gemini-3.7-flash |
| 11h21–11h25 | Polimento: fala, data, telefone (2 tentativas), dicionário, `end_call` ligado | 8.987 | gemini-3.7-flash |
| 11h25–11h26 | `end_call` desligado; `silence_end_call_timeout` 30 → 15 | 9.015 | gemini-3.7-flash |
| 11h26–11h29 | `end_call` religado com roteiro de fechamento | 9.550 | gemini-3.7-flash |
| 11h29–11h33 | 2 → 4 tentativas, dígito a dígito | 10.101 | gemini-3.7-flash |
| 11h33–11h39 | LLM → gpt-5-mini | 10.101 | gpt-5-mini |
| 11h40–12h25 | Endurecimento do prompt + bake-off → claude-sonnet-4-5 | 13.590 | claude-sonnet-4-5 |
| 15h05 | GET de conferência (estado atual igual ao de 12h25) | 13.590 | claude-sonnet-4-5 |

---

## EL-01 · Voz: Arnold → Raquel

| | |
|---|---|
| **Como estava** | `tts.voice_id = ZYCQDYoXnl78dNdU6JeG` ("Arnold - Br"): voz masculina, `category: professional`, `use_case: narrative_story`, `age: middle_aged` (`voice.json`, 06h54). O prompt se apresenta como "**a** assistente da Zellu". [VERIFICADO] |
| **O que mudamos** | `tts.voice_id`: `ZYCQDYoXnl78dNdU6JeG` → `GDzHdQOi6jjf8zaXhCYD` (Raquel). Mantidos `model_id: eleven_flash_v2_5`, `similarity_boost: 0.8`. A troca aparece entre o snapshot das 06h54 e o backup das 08h53. Não há script do Don para ela: **quem aplicou não está registrado** (provavelmente o Arthur pela CLI). [NÃO VERIFICADO: autoria] |
| **Por quê** | O Arthur pediu "um padrão de voz" mais natural (ligação 06h50). A persona é feminina. A pesquisa de latência (`elevenlabs_latencia.md`) indica que vozes *professional* (PVC) são a categoria mais lenta. |
| **Como testamos** | GET de 08h53 confirma o `voice_id`. As 3 ligações reais (10h53–11h00) já foram com a Raquel. |
| **Resultado** | **Corrigiu** a persona (voz feminina pt-BR). O ganho de latência isolado **não foi medido**. |
| **Próximo ajuste** | Avaliar a naturalidade numa ligação real depois do polimento (EL-05/06/10). |

## EL-02 · Portão de idade na primeira fala (`first_message` + bloco no topo do prompt)

| | |
|---|---|
| **Como estava** | `first_message`: "Olá, sou a assistente da Zellu. Vou coletar os dados do seu atendimento. Qual é o seu nome completo?". A regra de idade existia só no meio do prompt ("No canal phone, antes dos dados pessoais, pergunte a data de nascimento…"), mas a primeira fala já pedia o nome, um dado pessoal, antes de saber se a pessoa era menor. [VERIFICADO: snapshot 06h54] |
| **O que mudamos** | `first_message` → "Olá, sou a assistente da Zellu. Antes de começar, preciso confirmar sua idade: qual é a sua data de nascimento?". No topo do prompt entrou o bloco **PORTAO DE IDADE (PRIORIDADE MAXIMA)**: data → repetir → `validar_idade`; nenhum dado pessoal antes; `isAdult false` → encerrar com gentileza; `invalidDate` → perguntar de novo ou perguntar se tem 18+. Prompt 6.294 → 7.156. Script `s2.py`, ~08h53, usando `elevenlabs_agent_patch.json` (proposta das 06h54). |
| **Por quê** | Pedido do Arthur: "perguntar a idade da pessoa no começo" (ligação 06h50). Regra de negócio: não coletar dados de menor. |
| **Como testamos** | `s4.py` (GET pós-PATCH): prompt começa com o bloco, o prompt original aparece 1× e os demais campos ficam iguais. As 3 ligações reais das 10h53, 10h54 e 11h00 abriram com a pergunta da data. [VERIFICADO via API] |
| **Resultado** | **Corrigiu** a ordem (idade antes de qualquer dado). Expôs o problema de reconhecimento de fala da data (ver EL-07). |
| **Próximo ajuste** | Nenhum: a regra foi endurecida depois em EL-15. |

## EL-03 · `pre_tool_speech`: `auto` → `force` nas 3 ferramentas

| | |
|---|---|
| **Como estava** | `validar_idade`, `consultar_telefone`, `confirmar_codigo`: `pre_tool_speech: "auto"`, `force_pre_tool_speech: false`. O agente podia ficar mudo enquanto a ferramenta respondia. [VERIFICADO] |
| **O que mudamos** | `pre_tool_speech: "auto"` → `"force"` (e `force_pre_tool_speech` false → true) via `PATCH /v1/convai/tools/{id}` (`s3.py`, ~08h53). O script comparou o hash dos `request_headers` antes e depois para garantir que o header secreto **não** foi alterado. |
| **Por quê** | A pesquisa de latência indica o silêncio durante a ferramenta como causa de "parece travado". O `force` faz o agente falar algo antes de chamar. |
| **Como testamos** | GET pós-PATCH (`after pre force`, `hdr same True`). Nas ligações reais o agente fala antes de `validar_idade` ("Só um instante, verificando."). |
| **Resultado** | **Corrigiu** o silêncio, mas somado aos fillers do soft timeout gerou **falas empilhadas** (EL-04). |
| **Próximo ajuste** | Mantido. O controle do empilhamento foi feito no prompt e no soft timeout (EL-05). |

## EL-04 · Soft timeout e fillers, versão 1 (bem-humorada)

| | |
|---|---|
| **Como estava** | `soft_timeout_config.timeout_seconds: -1` (desligado), `message: "Hhmmmm...yeah."` (em inglês), sem mensagens adicionais, `randomize_fillers: false`, `max_soft_timeouts_per_generation: 1`. [VERIFICADO] |
| **O que mudamos** | `timeout_seconds` -1 → **1.5**. `message` → "Hmm, deixa eu ver aqui...". 8 mensagens adicionais **bem-humoradas e sem acento** ("Um segundinho, meu cerebro de robo ta pensando...", "Calma que ja ja sai, ta no forno!", "Peraí, to fazendo as contas nos dedos...", "Ja vai, ja vai..." …). `randomize_fillers` false → true. `max_soft_timeouts_per_generation` 1 → **8**. No prompt entraram as seções **FALAS DE ESPERA (FILLERS)** e **TOM EM ASSUNTOS SENSIVEIS** (sem humor em violência, saúde, luto…). (~08h53) |
| **Por quê** | Pedido do Arthur: falas tipo "tô pensando, hum, tô verificando com nosso especialista… uma coisa meio cômica, pra mostrar que tá funcionando", "totalmente naturais" (ligação 06h50). |
| **Como testamos** | 3 ligações reais (10h53–11h00, via API). |
| **Resultado** | **Não corrigiu (piorou).** Falas empilhadas na mesma resposta: "Ja vai, ja vai... Opa, so um momentinho... Só um instantinho, por favor."; "Peraí, to fazendo as contas nos dedos... Nao consegui concluir a verificacao agora…". Os fillers vinham escritos sem acento, e com `text_normalisation_type: system_prompt` o TTS lê exatamente o que está escrito. |
| **Próximo ajuste** | Feito em EL-05. |

## EL-05 · Soft timeout e fillers, versão 2 (curta e acentuada)

| | |
|---|---|
| **Como estava** | Ver EL-04 (1.5 s, até 8 por geração, piadas sem acento). |
| **O que mudamos** | `timeout_seconds` 1.5 → **2.5**. `max_soft_timeouts_per_generation` 8 → **5** (a proposta das 11h19 dizia 2; o valor aplicado foi 5). `message` → "Hmm, só um instantinho...". Mensagens adicionais → "Deixa eu ver aqui...", "Um momentinho, por favor...", "Já estou vendo pra você...", "Só um segundinho...". Regra de filler no prompt reescrita: "Antes de chamar uma ferramenta, diga no máximo UMA frase curta de espera… Nunca empilhe duas frases de espera. Não use filler em respostas normais". O "humor" saiu de TOM EM ASSUNTOS SENSÍVEIS. (11h21–11h25) |
| **Por quê** | Evidência das ligações (EL-04): empilhamento e pronúncia sem acento. |
| **Como testamos** | GET confirma a config. As simulações do bake-off (EL-16) não medem empilhamento de filler, e **não houve ligação real depois disso** (a lista de conversas da API termina às 11h00). |
| **Resultado** | **Parcial**: configurado, mas sem validação em ligação real. [NÃO VERIFICADO em áudio] |
| **Próximo ajuste** | Ligação real: contar quantos fillers saem por espera. Se ainda empilhar, baixar `max_soft_timeouts_per_generation` para 2 (valor da proposta original). |

## EL-06 · Velocidade, estabilidade e paciência de turno

| | |
|---|---|
| **Como estava** | `tts.speed: 1.0`, `tts.stability: 0.4`, `turn.turn_eagerness: "normal"`. [VERIFICADO] |
| **O que mudamos** | `speed` 1.0 → **0.9**; `stability` 0.4 → **0.5**; `turn_eagerness` normal → **patient** (11h21–11h25). |
| **Por quê** | Polimento: fala mais calma e voz mais estável, e mais espera antes de responder, importante quando o cliente **dita números** (data, telefone, código). |
| **Como testamos** | GET confirma os valores. Sem ligação real depois. |
| **Resultado** | **Parcial**: aplicado, efeito não medido. A pesquisa de latência aponta `patient` como um fator que aumenta a latência percebida. [NÃO VERIFICADO] |
| **Próximo ajuste** | Ligação real ditando o telefone em grupos: o agente não pode cortar no meio. Se a latência incomodar, testar `normal` só com `spelling_patience` (hoje `auto`). |

## EL-07 · Data de nascimento: ler de volta, normalizar e não culpar o cliente

| | |
|---|---|
| **Como estava** | O prompt já mandava "repita a data completa para confirmação e só então chame validar_idade", mas **nas 3 ligações reais** o cliente disse julho ("mês sete") e o reconhecimento de fala transcreveu "trinta e um de setembro". O agente chamou `validar_idade` com 31/09 sem ler de volta, recebeu `{"invalidDate":true}` e **disse que o cliente tinha errado**: "Essa data não parece estar correta, pois setembro vai até o dia trinta". O cliente corrigiu ("Mas eu não te falei setembro, eu falei que o mês é sete. Julho."). [VERIFICADO: conversas 10h53, 10h54, 11h00] |
| **O que mudamos** | Nova seção **DATA DE NASCIMENTO (NORMALIZAÇÃO)**: aceitar "1 de agosto de 98", "1/8/98", "primeiro do oito de noventa e oito", "01081998"; "mês sete" = julho; ano com 2 dígitos ≤ ano corrente → 20xx, senão 19xx; **dia impossível → perguntar o mês de novo sem afirmar que a pessoa errou** ("pode ter sido falha de áudio"); **ler a data por extenso e esperar o "sim"** antes de `validar_idade` (formato `AAAA-MM-DD`); `invalidDate` → pedir de novo com exemplo. (11h21–11h25) |
| **Por quê** | Evidência acima. O erro é do reconhecimento de fala (ASR), não do cliente. |
| **Como testamos** | Simulações do bake-off: o cenário "adulto" dita "1/8/98" e o verificador marca `validar_idade sem confirmação` quando a ferramenta é chamada sem um "sim" depois da leitura. Na 1ª rodada (11h56), 3 modelos ainda chamavam sem confirmar (`claude-sonnet-4-6`, `gemini-3.1-pro-preview`, `gpt-5-mini`). |
| **Resultado** | **Parcial**: a regra existe, e com o modelo final (claude-sonnet-4-5) as simulações passam. O erro de ASR em si continua (só é contornado), e **não houve ligação real** com "mês sete" depois da mudança. |
| **Próximo ajuste** | Ligação real dizendo "trinta e um do sete" e "primeiro do oito". Se o ASR insistir em "setembro", testar `asr.keywords` (hoje `[]`) com os nomes dos meses. |

## EL-08 · `age_check.py` (servidor): mais formatos de data

| | |
|---|---|
| **Como estava** | `parse_data_de_nascimento` aceitava só `%Y-%m-%d`, `%d/%m/%Y`, `%d-%m-%Y`, `%m/%d/%Y`. Qualquer outro formato → `None` → `invalidDate`, com a mensagem "Use o formato AAAA-MM-DD" (vista nas ligações). [VERIFICADO: código original e conversas] |
| **O que mudamos** | `agents/conversational_agent/age_check.py`: + `%d.%m.%Y` e a nova função `_parse_flexivel` para `D/M/AA`, `D-M-AA`, `DDMMAAAA` e `DDMMAA` (sempre na ordem brasileira). Ano de 2 dígitos ≤ ano corrente (yy) → 20xx, senão 19xx (`_ano_quatro_digitos`). 31/09 continua inválido. Entrega: `zellinho_age_check.zip` (rodada 4, 11h22), já incluída neste repo. |
| **Por quê** | O LLM pode mandar a data em formatos curtos. O servidor não deve devolver `invalidDate` para uma data válida. |
| **Como testamos** | `t.py` (11h19): 10 asserções ("1/8/98", "01081998", "010898", "5-3-05", "31/07/03", "31/09/2003" → None…). Hoje: `tests/test_age_parse_formatos.py` + `tests/test_age_check.py` = **24 testes passando**. [VERIFICADO] |
| **Resultado** | **Corrigiu** no código. **Em produção: não confirmado**, porque o deploy dependia do Coolify (ver `deploy-coolify.md`). [NÃO VERIFICADO em produção] |
| **Próximo ajuste** | Depois do redeploy: `POST /phone/age-check` com `birthDate: "1/8/98"` deve devolver `isAdult`. |

## EL-09 · Dicionário de pronúncia `zellu-ptbr`

| | |
|---|---|
| **Como estava** | `tts.pronunciation_dictionary_locators: []`. [VERIFICADO] |
| **O que mudamos** | Criado e anexado o dicionário `zellu-ptbr` (id `xz9trzBHqFZQVXJ3uAcw`) com 4 regras *alias*: `Zellu` → "Zélu", `DDD` → "dê dê dê", `SMS` → "ésse ême ésse", `e-commerce` → "í-comérce". (11h21–11h25) |
| **Por quê** | Termos que o TTS pt-BR tende a ler errado. "e-commerce" aparece nas ligações reais. Com `eleven_flash_v2_5`, *alias* funciona (tags de fonema não). |
| **Como testamos** | GET confirma o locator no agente. **Não ouvimos** o resultado em ligação. |
| **Resultado** | **Parcial**: aplicado, sem validação auditiva. [NÃO VERIFICADO em áudio] |
| **Próximo ajuste** | Ligação real: ouvir "Zellu", "DDD" e "SMS". Acrescentar ao dicionário outros termos que soarem errados. |

## EL-10 · Acentuação do prompt e seção RITMO E FALA

| | |
|---|---|
| **Como estava** | Prompt quase todo **sem acento** ("Voce e a assistente de atendimento da Zellu. Fale portugues brasileiro…", "Nao consegui concluir a verificacao agora…"). [VERIFICADO] |
| **O que mudamos** | Prompt reescrito com acentuação. Nova seção **RITMO E FALA**: "Fale com calma, frases curtas, uma pergunta por vez… Escreva sempre com acentuação correta. Escreva números por extenso (datas, telefones, códigos, valores), nunca em algarismos." Prompt 7.156 → 8.987 (junto com EL-07 e EL-11). |
| **Por quê** | `text_normalisation_type: system_prompt`: o TTS lê o texto como o LLM escreve. Sem acento, a pronúncia sai errada. |
| **Como testamos** | Leitura das falas nas simulações (texto acentuado). Sem teste de áudio. |
| **Resultado** | **Parcial**. [NÃO VERIFICADO em áudio] |
| **Próximo ajuste** | Ligação real. |

## EL-11 · Telefone: normalização, leitura em grupos e REGRA RÍGIDA (2 tentativas)

| | |
|---|---|
| **Como estava** | "Repita o número dígito a dígito UMA vez". Em falha da ferramenta (`in_use`, `unavailable`, `sms_failed`, `erro`…), a regra era **seguir em frente**: "Não consegui concluir a verificação agora, mas podemos continuar seu relato". Nas ligações das 10h54 e 11h00, `consultar_telefone` devolveu `{"outcome":"in_use"}` e o agente seguiu para o relato. [VERIFICADO] |
| **O que mudamos** | Nova seção **TELEFONE (NORMALIZAÇÃO)**: dígitos em grupos, "meia" = 6, com ou sem o 9, descartar o 0 e o código de operadora; ler de volta **em grupos** ("DDD um, um. Nove, oito, sete…"). "FERRAMENTAS DE IDENTIFICAÇÃO" virou **REGRA RÍGIDA**: falha em `consultar_telefone` → **não** seguir para o relato; "Não consegui validar esse número. Vamos tentar mais uma vez?"; no máximo **2** novas tentativas; depois "…cadastro pelo site da Zellu, ou ligar de novo daqui a pouco" e `end_call`. (11h21–11h25) |
| **Por quê** | Pedido da sessão: não aceitar relato com telefone não validado. ⚠️ A proposta das 11h19 alertou que isso **contraria a regra anterior** ("NUNCA ENCERRAR POR FALHA OU NÚMERO EM USO", ligada ao contrato/emenda 6) e que precisava de confirmação do negócio. **Não há registro dessa confirmação.** [NÃO VERIFICADO: aprovação de negócio] |
| **Como testamos** | Substituída 8 minutos depois por EL-12, antes de qualquer teste. |
| **Resultado** | **Substituída** por EL-12. |
| **Próximo ajuste** | Ver EL-12. |

## EL-12 · Telefone, código e data: 2 → 4 tentativas, dígito a dígito

| | |
|---|---|
| **Como estava** | Ver EL-11 (2 tentativas, número de novo "DDD e número"). Data: "Após duas falhas, pergunte diretamente se tem dezoito anos ou mais". Código: "no máximo duas vezes". |
| **O que mudamos** | `g2.py` (11h30), 8 trocas exatas de texto, incluindo: "No máximo duas novas tentativas." → "…pedindo para falar o DDD e o número dígito por dígito; leia de volta o que entendeu e confirme antes de consultar. Faça até quatro tentativas no total, sem desistir fácil."; código → "dígito por dígito, lendo de volta e confirmando; até quatro tentativas no total"; data → "até quatro tentativas, pedindo dia, mês e ano separadamente"; "Depois de esgotar as tentativas" → "Somente depois de quatro tentativas sem sucesso". No endurecimento (EL-15) entrou: "**Conta como tentativa somente cada chamada a consultar_telefone**; um número incompleto ou corrigido antes da consulta não conta." Prompt 9.550 → 10.101. |
| **Por quê** | Com 2 tentativas, erros de reconhecimento de fala derrubariam clientes legítimos. Ditar dígito a dígito reduz erro de ASR. |
| **Como testamos** | GET pós-PATCH (`prompt ok: True`, nenhuma menção a "duas vezes/tentativas" restante). Cenário `tel_falha4` do bake-off (mock `not_found`; exige **exatamente 4** consultas, mensagem do site e nada de coleta depois). 1ª rodada: `gemini-3.7-flash` OK, `claude-sonnet-4-5` OK, `gpt-4o` "3 consultas (esperado 4)", `gemini-3.8-flash` "3 consultas" e consultas sem confirmação. |
| **Resultado** | **Corrigiu em simulação** com o modelo final. Sem ligação real. |
| **Próximo ajuste** | Ligação real com um número que dê `not_found`, contando as tentativas. |

## EL-13 · `end_call`: desligado → ligado → desligado → ligado só com roteiro

| Passo | Hora | Antes | Depois | Fonte |
|---|---|---|---|---|
| 0 | 06h54 | `built_in_tools.end_call: null` | — | snapshot |
| 1 | 11h21–11h25 (polimento) | `null` | system tool `end_call` (`pre_tool_speech: auto`, timeout 20 s); prompt manda `end_call` em menor, telefone não validado, recusa | backup 11h25 (reconstruído) |
| 2 | 11h25–11h26 | ligado | `end_call: null`; 4 trechos do prompt trocados ("…encerre a chamada com end_call." → "…pare de falar, sem continuar a coleta."); `turn.silence_end_call_timeout` **30 → 15** s | `e.py`, backup 11h26 |
| 3 | 11h26–11h29 | desligado | `end_call` religado + seção **ENCERRAMENTO DA LIGAÇÃO (END_CALL)**: "Quando terminar a coleta… pergunte: 'Precisa de mais alguma coisa?'. Se a pessoa disser que não, diga que o chamado já está registrado no chat, agradeça a ligação, diga que, se tiver qualquer problema, é só ligar de novo e que estamos à disposição, e então chame end_call." Não usar em menor, recusa ou telefone não validado | `f.py`, backup 11h29 |
| 4 | 11h40–12h25 (endurecimento) | roteiro simples | **Roteiro obrigatório A/B/C** (ver abaixo) | `newprompt.py`, GET atual |

| | |
|---|---|
| **Como estava** | Sem `end_call`, a ligação só terminava quando o cliente desligava. Na ligação das 11h00 o agente terminou com "…faça um cadastro" e o cliente desligou (`Client disconnected: 1000`). [VERIFICADO] |
| **O que mudamos** | Ver tabela. Estado final (A/B/C): **A)** depois do resumo confirmado, perguntar exatamente "Precisa de mais alguma coisa?" e **esperar**; se "não", **falar em voz alta, por inteiro**, "Seu chamado está registrado no chat da Zellu. Obrigada pela ligação! Se tiver qualquer problema, é só ligar de novo. Estamos à disposição." e só então `end_call`. **B)** Se a pessoa se despedir: despedida curta sem perguntas + `end_call`. **C)** Menor de idade (EL-15). Proibido: `end_call` sem ter falado a despedida, logo após uma pergunta, ou sem antes perguntar "Precisa de mais alguma coisa?". Recusa de consentimento e telefone não validado: despedida **sem** `end_call` (o silêncio encerra em 15 s). |
| **Por quê** | Passo 3: roteiro de fechamento pedido na sessão. Passo 2: **o motivo de desligar não está registrado em arquivo**; a hipótese é o risco de o agente encerrar cedo ou em silêncio. [NÃO VERIFICADO: motivo do passo 2] |
| **Como testamos** | Bake-off (EL-16), cenários `adulto`, `fechamento` e `tchau`. O verificador marca `end_call após pergunta`, `roteiro de fechamento incompleto` (exige "registrado" + "ligar de novo" + "dispos" na última fala) e `não se despediu / perguntou`. Na 1ª rodada (11h56), até o `claude-sonnet-4-5` falhou em "roteiro de fechamento incompleto" (3/5). Por isso entrou o "FALAR em voz alta, por inteiro" e o "Nunca chame end_call sem antes ter falado a despedida na mesma resposta". |
| **Resultado** | **Corrigiu em simulação** (rodada final relatada: 25/25 com claude-sonnet-4-5 [RELATO]). Sem ligação real. |
| **Próximo ajuste** | Ligação real: a frase de encerramento precisa ser falada inteira **antes** de a ligação cair. Testar também "tchau" no meio da coleta. |

## EL-14 · LLM: gemini-3.7-flash → gpt-5-mini (reprovado)

| | |
|---|---|
| **Como estava** | `llm: gemini-3.7-flash`, `temperature: 0.3`, `reasoning_effort: low`. [VERIFICADO] |
| **O que mudamos** | `j.py` (11h33): `llm` → `gpt-5-mini`, `reasoning_effort` low → **minimal** (aceito no 1º PATCH). Prompt, ferramentas, turn e TTS conferidos iguais. |
| **Por quê** | Buscar um modelo que seguisse as regras de fluxo (leitura de volta, tentativas, encerramento). [NÃO VERIFICADO: motivo não registrado em arquivo] |
| **Como testamos** | `k.py` (11h36): simulações `adulto` e `fechamento` via `POST /v1/convai/agents/{id}/simulate-conversation`. Bake-off 11h56 (`gpt-5-mini/low`): **1/5**, TTFS mediana 2,25 s / p90 4,02 s. Falhas: "não consultou telefone", "não perguntou 'Precisa de mais alguma coisa?'", `validar_idade` e `consultar_telefone` sem confirmação, `end_call` após pergunta. |
| **Resultado** | **Não corrigiu**: reprovado e trocado em EL-16. |
| **Próximo ajuste** | — |

## EL-15 · Endurecimento do prompt: fluxo obrigatório e regra de menor de idade

| | |
|---|---|
| **Como estava** | Prompt de 10.101 caracteres, com regras espalhadas por seção. Modelos chamavam ferramenta sem confirmação e encerravam logo após uma pergunta (bake-off 11h56). [VERIFICADO] |
| **O que mudamos** | `newprompt.py` (11h40), refinado até 12h25. Prompt 10.101 → **13.590**. Mudanças: **(1)** bloco no topo **FLUXO OBRIGATÓRIO DA LIGAÇÃO** com 8 passos (data lida de volta → `validar_idade` só após "sim" explícito, **nunca no mesmo turno** em que a pessoa disse a data → menor? → consentimento → nome → telefone lido dígito a dígito → relato e resumo → encerramento); "nunca chame uma ferramenta antes de o dado ter sido dito, lido de volta e confirmado com um sim explícito". **(2)** "Sempre chame validar_idade, mesmo que a pessoa diga a idade ou pareça menor: não calcule a idade por conta própria". **(3)** Seção **MENOR DE IDADE**: parar tudo, uma única despedida calorosa ("…o atendimento da Zellu é só para maiores de dezoito anos… Peça para um adulto responsável ligar pra gente. Um abraço e se cuide!") + `end_call`, sem retomar mesmo se a pessoa insistir. **(4)** Roteiro A/B/C do `end_call` (EL-13). **(5)** Regra de contagem de tentativas (EL-12). `reasoning_effort` minimal → `null` (com a troca para Claude). |
| **Por quê** | Falhas observadas nas simulações. |
| **Como testamos** | Rodadas do bake-off (EL-16). |
| **Resultado** | **Corrigiu em simulação** com claude-sonnet-4-5. |
| **Próximo ajuste** | Ligação real simulando menor de idade, incluindo insistir depois da despedida. |

## EL-16 · Bake-off de LLMs e escolha do claude-sonnet-4-5

**Método** (`/tmp/bake.py`, box do Don):

- Para cada modelo: `PATCH` do `llm`/`reasoning_effort` no agente e depois `POST …/simulate-conversation` em paralelo.
- Usuário simulado: `gpt-4.1`, pt-BR, `origin=phone`, até 45 turnos.
- Ferramentas mockadas: `validar_idade` → adulto (28) ou menor (15); `consultar_telefone` → `found` ou `not_found`; `confirmar_codigo` → `verified`.
- 5 cenários: **adulto** (fluxo completo), **menor** (15 anos que insiste em pedir ajuda), **tel_falha4** (4 números diferentes, todos `not_found`), **tchau** (desliga logo depois do nome) e **fechamento** (fluxo completo com "não, só isso, obrigado").
- Verificador automático: ferramenta sem "sim" antes; `consultar_telefone` sem número completo lido; `end_call` após pergunta; roteiro de fechamento incompleto; menor sem `end_call` ou com mais de uma fala; número de consultas ≠ 4; coleta depois da falha.
- Latência = `convai_llm_service_ttf_sentence` (tempo até a 1ª frase do LLM), **na simulação**, não ponta a ponta no telefone.

**Rodada 1 (11h56, `bake1.txt`, 1 execução × 5 cenários) [VERIFICADO]:**

| Modelo | Aprovados | TTFS mediana / p90 | Principais falhas |
|---|---|---|---|
| claude-haiku-4-5 | 4/5 | 0,42 s / 1,16 s | menor: "não validou" |
| gemini-3.5-flash | 4/5 | 0,84 s / 1,67 s | `end_call` após pergunta; fechamento incompleto |
| claude-sonnet-4-5 | 3/5 | 0,87 s / 2,57 s | roteiro de fechamento incompleto (2×) |
| gemini-3.7-flash (modelo de antes) | 2/5 | 1,54 s / 3,46 s | `end_call` após pergunta; não se despediu |
| gemini-3.8-flash | 2/5 | 4,93 s / 9,53 s | `consultar_telefone` sem confirmação; 3 consultas |
| claude-sonnet-4-6 | 2/5 | 0,98 s / 2,46 s | ferramentas sem confirmação |
| gpt-4.1 | 1/5 | 0,74 s / 1,76 s | `end_call` após pergunta |
| gemini-3.1-pro-preview | 1/5 | 5,22 s / 8,79 s | ferramentas sem confirmação |
| gpt-5-mini (low) | 1/5 | 2,25 s / 4,02 s | ver EL-14 |
| gpt-4.1-mini | 0/5 | 0,66 s / 1,84 s | `end_call` após pergunta; sem confirmação |
| gpt-4o | 0/5 | 0,64 s / 1,51 s | sem confirmação; 3 consultas |
| gpt-5.4-mini | — | — | `PATCH 400`: "temperature must be 0 when reasoning effort is not none for this model" |

**Rodadas seguintes (12h00–12h25, com o prompt já ajustado) [RELATO: placar consolidado na sessão; o arquivo de saída dessas rodadas não foi salvo no box]:**

| Modelo | Resultado | Latência (mediana / p90) | Decisão |
|---|---|---|---|
| **claude-sonnet-4-5** | **25/25** | **0,9 s / 2,4 s** | **Escolhido** |
| claude-sonnet-5 | 13/15 | 0,67 s | Mais rápido, errou 2 cenários |
| claude-haiku-4-5 | 7–11/15 | 0,4 s | Rápido, inconsistente |
| gemini-3.5-flash | 5–8/15 | — | Reprovado |
| claude-sonnet-5-5 | 4/10 | — | Reprovado |
| claude-sonnet-4-6 | 3/10 | — | Reprovado |
| gemini-3.7-flash (antigo) | 1–2/10 | — | Reprovado |
| gpt-4.1 / 4.1-mini / 4o | 0–1/5 | — | Reprovados |
| gemini-3.8-flash / 3.1-pro | — | lentos | Descartados pela latência |
| gpt-5.4-mini | — | — | API recusou o PATCH (temperatura) |

| | |
|---|---|
| **Como estava** | `gpt-5-mini` / `minimal` (EL-14). |
| **O que mudamos** | `llm` → **`claude-sonnet-4-5`**, `temperature: 0.3` (mantida), `reasoning_effort: null`, `backup_llm_config: default`, `cascade_timeout_seconds: 4`. [VERIFICADO: GET 15h05] |
| **Por quê** | Único modelo com 100% nas rodadas finais, com latência aceitável. |
| **Como testamos** | Ver acima. |
| **Resultado** | **Corrigiu em simulação.** A latência real no telefone **não foi medida** (não houve ligação depois). |
| **Próximo ajuste** | Ligação real medindo a percepção de demora. Se incomodar, reavaliar `claude-sonnet-5` (0,67 s, 13/15) depois de ajustar o prompt para os 2 cenários em que ele erra. |

## EL-17 · Webhook pós-chamada desativado desde 2026-09-30 (aberto)

| | |
|---|---|
| **Como estava (e está)** | Webhook do workspace **"Zellu - pos-chamada telefonica"** → `https://ia-service.zellu.tec.br/webhooks/elevenlabs/post-call`, auth **HMAC**, `retry_enabled: true`, **`is_disabled: true`, `is_auto_disabled: true`**, última falha **HTTP 401 em 2026-09-30 22h45 BRT** (`most_recent_failure_timestamp` 1790819128). No agente, `workspace_overrides.webhooks.post_call_webhook_id: null` (eventos `transcript`, JSON). As configurações do workspace apontam o webhook `2fcdf2a8…`. [VERIFICADO: GET 15h05] |
| **O que mudamos** | **Nada.** Só diagnóstico. Reativar exige conferir o segredo no Coolify, que estava fora do ar. |
| **Por quê** | — |
| **Como testamos** | GETs `/v1/workspace/webhooks`, `/v1/convai/settings` e do agente. |
| **Resultado** | **Não corrigido** (aberto). Consequência: transcrição e data collection das ligações **não chegam** ao backend, e o relato não entra no chat. |
| **Próximo ajuste** | 1) Comparar o segredo HMAC da ElevenLabs com o `ELEVENLABS_WEBHOOK_SECRET` do Coolify (causa provável do 401) [NÃO VERIFICADO: causa]. 2) Reativar o webhook no painel. 3) Confirmar com o backend que `POST /api/phone/calls` devolve `chatId`/`userId` (usado por `voice_chat_handoff`). 4) Fazer uma ligação de teste e conferir `POST /webhooks/elevenlabs/post-call` → 200. |

---

## Configuração atual (GET 15h05, sem segredos)

| Área | Valor |
|---|---|
| LLM | `claude-sonnet-4-5`, temp 0.3, backup default, cascade 4 s |
| TTS | `eleven_flash_v2_5`, Raquel, speed 0.9, stability 0.5, similarity 0.8, `text_normalisation_type: system_prompt`, dicionário `zellu-ptbr` |
| ASR | `scribe_realtime`, qualidade high, `keywords: []` |
| Turn | `patient`, `turn_v3`, `speculative_turn: true`, `spelling_patience: auto`, `turn_timeout: 10`, `silence_end_call_timeout: 15` |
| Soft timeout | 2,5 s, "Hmm, só um instantinho..." + 4 frases, randomizado, máx. 5 |
| Ferramentas | `validar_idade` (10 s), `consultar_telefone` (20 s), `confirmar_codigo` (10 s): `pre_tool_speech: force`, header `x-api-key` (**valor redigido**); `end_call` (system) |
| Prompt | 13.590 caracteres ([`anexos/raquel-prompt.md`](anexos/raquel-prompt.md)) |

## Feedback do Arthur / Próximos testes

- [ ] Ligação real: paciência de turno ao **ditar o telefone** (não cortar no meio dos dígitos), EL-06.
- [ ] Ligação real: data falada de formas variadas ("primeiro do oito de noventa e oito", "trinta e um do sete"), EL-07.
- [ ] Ligação real: contar fillers por espera (empilhou?), EL-05.
- [ ] Ligação real: ouvir "Zellu", "DDD" e "SMS" (dicionário), EL-09.
- [ ] Ligação real: frase de encerramento falada inteira antes do `end_call`, e "tchau" no meio, EL-13.
- [ ] Ligação simulando menor de idade e insistindo, EL-15.
- [ ] Telefone que dá `not_found`: exatamente 4 tentativas, EL-12.
- [ ] Percepção de latência com `claude-sonnet-4-5`, EL-16.
- [ ] Confirmar com o negócio a REGRA RÍGIDA do telefone (contraria a regra antiga da emenda 6), EL-11.
- [ ] Depois de reativar o webhook: transcrição chegando no chat, EL-17.
- Anotações:
