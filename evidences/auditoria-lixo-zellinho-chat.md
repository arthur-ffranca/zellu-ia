# Auditoria: lixo inútil no repo Zelluu/zellinho_chat

Data: out/2026. Método: grep de importadores em todo o repo + o que o `Dockerfile` copia para a imagem + o que está versionado à toa. Veredito por item, sem dó.

## Como foi apurado

- Entrada real: `app.py` → `uvicorn src.main:app`. Só.
- Código vivo: `src/`, `api/`, `agents/` (importado pelo `src`), `models/`, `config.py`.
- A imagem Docker copia só: `agents/`, `api/`, `src/`, `models/`, `static/`, `templates/`, `docs_teste/`, `document_urls/`, `generated_documents/`, `security/`, `config.py`, `app.py`. O resto **não existe em produção**.

## Lista — serve pra nada

### Duplicados na raiz (11 arquivos .py) — SERVE PRA NADA
Cada um tem o gêmeo vivo em `src/` e **zero importadores** no repo inteiro. Não entram na imagem Docker. São cadáveres de refatoração que ninguém apagou:

| Arquivo morto | Gêmeo vivo que roda |
|---|---|
| `company_intake.py` | `src/services/company_intake.py` |
| `company_search_pipeline.py` | `src/services/company_search_pipeline.py` |
| `intake.py` | `src/intake.py` + `src/open_dots/intake.py` |
| `state.py` | `src/` (via `agents.state`) |
| `state_manager.py` | `src/state_manager.py` |
| `staged_intake_flow.py` | `src/services/staged_intake_flow.py` |
| `case_documents.py` | `src/services/case_documents.py` |
| `document_generation_service.py` | `src/services/document_validation_service.py` + geração em `src/` |
| `document_validation_service.py` | `src/services/document_validation_service.py` |
| `main.py` | `src/main.py` (é ele que o `app.py` sobe) |
| `PATCH_autostart_handler.py` | nada — patch avulso sem importador |

### Pastas de entulho versionado — SERVE PRA NADA
| Pasta | Conteúdo | Veredito |
|---|---|---|
| `debug_payloads/` | **162 payloads de requisição commitados** | Lixo com potencial dado de cliente no histórico do git. Apagar + gitignore. |
| `document_urls/` | 53 JSONs de URLs geradas em runtime | Artefato de execução, não código. Apagar arquivos, manter a pasta (o código escreve nela). |
| `generated_documents/` | 54 arquivos gerados em runtime | Idem acima. |
| `reports/` | 1 arquivo | Resto de relatório. Apagar. |
| `nova documentação/` | 1 arquivo | Nome já diz tudo. Apagar. |
| `tmpclaude-*-cwd` | 2 arquivos | Resto de sessão do Claude. Apagar. |

### Docs avulsos na raiz (20 arquivos .md) — RESOLVIDO
Entulho de specs e handoffs espalhados na raiz. Movidos para `docs_old/` no commit `ddbfd7d` (rename, histórico preservado). `README.md` ficou.

## O que foi poupado (não é lixo)
- `agents/` — vivo, `src` importa de lá. Quem mandar apagar quebra o boot.
- `docs_teste/` — 16 fixtures (casos de teste + `instrucao_secreta.pdf`, fixture de segurança). Pequeno, inofensivo, útil em QA manual.
- `docs/` — documentação viva do projeto.

## Ação recomendada (falta executar)
1. `git rm` dos 11 `.py` da raiz + `PATCH_autostart_handler.py`.
2. `git rm -r debug_payloads document_urls generated_documents reports "nova documentação"` + `.gitkeep` nas duas pastas de runtime + `.gitignore` para elas e `debug_payloads/`.
3. Commit `chore(limpeza)` separado. Nada disso é importado por nada — risco zero, verificado por grep.
