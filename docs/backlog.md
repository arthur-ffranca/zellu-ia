# Backlog

Atualizado em 2026-10-07 15h (BRT). Ordem sugerida. IDs remetem a `DE-PARA.md`.

| # | Item | Status | Bloqueio / dependência | Próximo passo |
|---|---|---|---|---|
| 1 | **Refazer o deploy a partir deste repo** | 🟡 Desbloqueado | Coolify voltou às 14h50 (15h06: painel 200, `/health` 200) | Redeploy a partir do `zelinho-ia` (já inclui as rodadas 0–4 e a correção do `--reload`). Seguir o checklist de `deploy-coolify.md` e anotar tempo de build e tempo até *healthy* (CO-02, CO-06). Perguntar ao responsável pela VPS a causa do incidente (CO-07) |
| 2 | **Reativar o webhook pós-chamada** ("Zellu - pos-chamada telefonica") | 🔴 Aberto | Painel do Coolify de volta: já dá para conferir a env | Conferir o segredo HMAC nos dois lados (ElevenLabs × `ELEVENLABS_WEBHOOK_SECRET`), reativar no painel da ElevenLabs, garantir que `/api/phone/calls` devolve `chatId`/`userId`, testar com uma ligação. Ver `elevenlabs-raquel.md` EL-17 |
| 3 | **Ligação de teste real com a Raquel** | 🟡 A fazer | Idealmente após o item 2 | Roteiro em `elevenlabs-raquel.md` → Próximos testes (EL-05/06/07/09/12/13/15/16). Nenhuma ligação real foi feita depois das 11h00 |
| 4 | **SMS via Twilio com link curto da Zellu** | ⏸️ Aguardando | Credenciais Twilio + destino do link curto | Quando chegarem: definir texto do SMS, credenciais só no Coolify, testar |
| 5 | Linha `legaltech-fix` / `zellu-bugfixes` | ⛔ Fora do escopo | Decisão do Arthur: **não** incorporar neste repo | Nada a fazer aqui |
| 6 | Rotacionar a chave OpenAI que aparece no `HANDOFF.md` do repo antigo e conferir o prefixo duplicado no Coolify (CH-09) | 🔴 Segurança | — | Revogar na OpenAI e cadastrar a nova só no Coolify, colando só o valor |
| 7 | Confirmar com o negócio a regra rígida do telefone (EL-11/12) | 🟡 Decisão | — | A regra antiga (emenda 6) mandava seguir o relato mesmo sem validar o número |

## Feedback do Arthur / Próximos testes

- Anotações:
