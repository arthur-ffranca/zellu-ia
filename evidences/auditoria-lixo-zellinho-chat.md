# MASSACRE: 284 arquivos de lixo inútil no repo Zelluu/zellinho_chat

Data: out/2026. Método: grep de importadores no repo inteiro + o que o `Dockerfile` realmente copia + o que está versionado sem motivo. Não é opinião — é fato verificável item por item. **Total: 284 arquivos que não servem pra absolutamente nada.**

Data: out/2026. Método: grep de importadores no repo inteiro + o que o `Dockerfile` realmente copia + o que está versionado sem motivo. Não é opinião — é fato verificável item por item.

## O quadro geral

A aplicação que roda em produção é: `app.py` → `uvicorn src.main:app`. Ponto final. O resto deste capítulo é a lista de tudo que **não faz parte disso e mesmo assim está lá**, ocupando espaço, confundindo quem lê e fingindo que é código.

## O placar do lixo: 284 × 0

| Categoria | Arquivos | Utilidade |
|---|---|---|
| `.py` cadáveres na raiz | 11 | zero |
| `debug_payloads/` | 162 | zero (e ainda é risco) |
| `document_urls/` | 53 | zero |
| `generated_documents/` | 54 | zero |
| `reports/` + `nova documentação/` + `tmpclaude-*` | 4 | zero |
| **TOTAL** | **284** | **nada** |

(Fora os 20 `.md` avulsos da raiz — esses já foram organizados para `docs_old/` e saíram da conta do lixo.)

## Os 11 cadáveres da raiz — SERVEM PRA PORRA NENHUMA

Onze arquivos `.py` largados na raiz, cada um com o gêmeo vivo em `src/`, **zero importadores no repo inteiro** e **fora da imagem Docker**. Não rodam em produção, não rodam nos testes, não rodam em lugar nenhum. São restos de refatoração que alguém teve preguiça de apagar:

| Cadáver | Gêmeo vivo que faz o trabalho | Tempo de vida útil restante |
|---|---|---|
| `company_intake.py` | `src/services/company_intake.py` | zero |
| `company_search_pipeline.py` | `src/services/company_search_pipeline.py` | zero |
| `intake.py` | `src/intake.py` + `src/open_dots/intake.py` | zero |
| `state.py` | `agents.state` (via `src`) | zero |
| `state_manager.py` | `src/state_manager.py` | zero |
| `staged_intake_flow.py` | `src/services/staged_intake_flow.py` | zero |
| `case_documents.py` | `src/services/case_documents.py` | zero |
| `document_generation_service.py` | serviços de geração em `src/` | zero |
| `document_validation_service.py` | `src/services/document_validation_service.py` | zero |
| `main.py` | `src/main.py` (o que o `app.py` sobe de verdade) | zero |
| `PATCH_autostart_handler.py` | nada — patch avulso sem importador, lixo histórico puro | menos que zero |

Pior que inúteis: são **armadilhas**. Qualquer pessoa nova no repo abre `main.py` ou `company_intake.py` na raiz, acha que é o código real, edita o arquivo errado e perde o dia. Cada dia que passam lá, cobram pedágio de quem tenta entender o projeto.

## O entulho versionado — LIXO COM HISTÓRICO NO GIT

| Entulho | O que é | Por que é um absurdo |
|---|---|---|
| `debug_payloads/` (162 arquivos!) | Cento e sessenta e dois payloads de requisição **commitados** | Além de lixo, é risco: payload real pode conter dado de cliente, agora eternizado no histórico do git. Apagar + `.gitignore` ontem. |
| `document_urls/` (53 arquivos) | JSONs cuspidos pelo programa em runtime | Versionar output de execução é como guardar o lixo da lixeira num cofre. Apaga os arquivos, mantém a pasta (o código escreve nela). |
| `generated_documents/` (54 arquivos) | Documentos gerados em runtime | Idem. O nome da pasta já confessa: *generated*. Gerado se gera de novo — não se commita. |
| `reports/` | Resto de relatório | Uma pasta chamada `reports` com 1 arquivo abandonado. |
| `nova documentação/` | 1 arquivo com nome de pasta de tio do zap | Nem o nome se leva a sério. |
| `tmpclaude-*-cwd` | Restos de sessão do Claude | Sujeira de ferramenta commitada junto. `rm` sem pensar duas vezes. |

## Burrice bônus: 20 docs avulsos na raiz — JÁ EXECUTADO

Specs, handoffs e guias espalhados na raiz como roupa no chão do quarto. Movidos para `docs_old/` (commit `ddbfd7d`, rename com histórico). `README.md` ficou porque README mora na raiz por lei.

## O que foi poupado — e por quê (nem tudo é lixo)

- `agents/` — **vivo**. O `src` importa `agents.state` e os routers de voz. Quem mandar apagar isso quebra o boot em produção. Checado, não chutado.
- `docs_teste/` — 16 fixtures incluindo `instrucao_secreta.pdf` (fixture de segurança). Pequeno, inofensivo, útil. Fica.
- `docs/` — documentação viva. Fica.

## Sentença (falta executar)

1. `git rm` nos 11 `.py` da raiz + `PATCH_autostart_handler.py`. Risco: **zero** — grep prova que nada importa nada.
2. `git rm -r debug_payloads document_urls generated_documents reports "nova documentação"` + `.gitkeep` nas pastas de runtime + `.gitignore` nelas.
3. Commit `chore(limpeza)` e pronto: repo respira de novo.
