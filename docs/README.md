# Documentação do zelinho-ia

Registro do que o Arthur e o Don fizeram juntos em 2026-10-06/07, em formato **de-para**: para cada mudança, como estava (com evidência), o que mudamos (antes → depois), por quê, como testamos, o resultado e o próximo ajuste.

## Índice

| Documento | Conteúdo |
|---|---|
| [`DE-PARA.md`](DE-PARA.md) | **Comece aqui.** Tabela única com todas as mudanças de todas as frentes |
| [`elevenlabs-raquel.md`](elevenlabs-raquel.md) | Agente de voz Raquel (EL-01 a EL-17): voz, idade, fillers, data, telefone, `end_call`, LLMs, bake-off, webhook |
| [`chat-intake.md`](chat-intake.md) | Chat do consumidor (CH-01 a CH-09): modelo do intake, bolhas de progresso, escada de busca da empresa, chave da OpenAI |
| [`deploy-coolify.md`](deploy-coolify.md) | Coolify (CO-01 a CO-07): MinIO, healthcheck, `--reload`, imagem, requirements, rebuild, incidente 523 |
| [`bifrost-edge.md`](bifrost-edge.md) | Bifrost e `EDGE_ROUTE` (BF-01 a BF-03), com o desenho do plugin em anexo |
| [`backlog.md`](backlog.md) | O que falta, em ordem |
| [`arquivos-removidos.md`](arquivos-removidos.md) | O que ficou de fora do repo limpo e por quê |
| [`../CHANGELOG.md`](../CHANGELOG.md) | Cronologia |
| [`anexos/raquel-prompt.md`](anexos/raquel-prompt.md) | Prompt atual da Raquel (13.590 caracteres) |
| [`anexos/raquel-agent-config.redacted.json`](anexos/raquel-agent-config.redacted.json) | Config atual do agente, **redigida** (sem headers, tokens ou ID) |

## Legenda

| Marca | Significado |
|---|---|
| **[VERIFICADO]** | Conferido num arquivo, backup, diff, resposta de API ou teste rodado pelo Don |
| **[RELATO]** | Dito pelo Arthur ou registrado na sessão, sem arquivo de prova no box (ex.: placar final do bake-off, horário da recuperação do Coolify) |
| **[NÃO VERIFICADO]** | Hipótese ou motivo não registrado. Não tratar como fato |
| ✅ / 🟡 / ❌ / ⏳ / ↪️ | Corrigiu / parcial (aplicado, sem validação real) / não corrigiu / aberto / substituído |

## De onde vêm as evidências

- **Código**: `git diff` entre o zip original do repositório (arquivos de 2026-10-06 14h16) e este repo, mais os zips de cada rodada (rodada 0, 06/10 21h40; rodada 1, ~22h; rodada 2 MinIO/healthcheck, 22h38; rodada 3 edge route, 07/10 06h41; rodada 4 age check, 11h22).
- **ElevenLabs**: snapshot das 06h54, backups automáticos antes de cada PATCH, scripts de PATCH, ligações reais lidas pela API e GET de conferência às 15h05. Os arquivos brutos **não** estão no repo porque contêm o header secreto das ferramentas. Só existe a versão redigida em `anexos/`.
- **Reproduções locais** (07/10 ~15h): boot do original × zelinho-ia com variáveis do Coolify e com um MinIO falhando.
- **Ligações de voz Arthur ↔ Don** (07/10 06h33 e 06h50): pedidos do Arthur.

Dados pessoais das ligações (nomes, telefones, datas de nascimento) foram omitidos.
