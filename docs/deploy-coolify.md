# Deploy no Coolify: log de-para

Serviço: `ia-service.zellu.tec.br` (FastAPI na porta 8000), deployado pelo Coolify a partir do `Dockerfile` da raiz. Painel: `coolify.zellu.tec.br`. O Arthur tem acesso ao painel, mas **não** tem SSH na VPS. O passo a passo de configuração está no README ("Rodar no Coolify").

Legenda de evidência em [`README.md`](README.md#legenda). "Original" = zip do repositório (arquivos de 2026-10-06 14h16).

---

## CO-01 · Boot travado pelo MinIO

| | |
|---|---|
| **Como estava** | `src/minio_service.py`: com `MINIO_ENDPOINT` preenchido, o construtor chamava `bucket_exists()` **no boot**, com o cliente MinIO padrão (timeouts longos e retries do urllib3). O `app.py` reclamava de credenciais sempre que `MINIO_ENDPOINT` existia. Relato da rodada 2: endpoint fora do ar (503) segurando o startup por **~2 min 45 s** e derrubando o healthcheck. O MinIO **não é usado em produção**. [RELATO: tempo de 2 min 45 s] |
| **O que mudamos** | Rodada 2 (`zellinho_minio_healthcheck.zip`, 06/10 22h38). `config.py`: novas `MINIO_ENABLED: bool = False` e `MINIO_TIMEOUT_SECONDS: float = 5.0`. `minio_service.py`: com `MINIO_ENABLED=false` **não faz nenhuma chamada de rede** ("[MINIO] Disabled…"); com `true`, usa um `urllib3.PoolManager` próprio (timeout de conexão/leitura = `MINIO_TIMEOUT_SECONDS`, `Retry(total=1, backoff_factor=0.2)`). `app.py`: `if settings.MINIO_ENDPOINT:` → `if settings.MINIO_ENABLED and settings.MINIO_ENDPOINT:`. `MINIO_ENDPOINT` continua liberando o host para download de evidências, independente da flag. |
| **Por quê** | Um serviço opcional e fora do ar não pode impedir o app de ficar pronto. |
| **Como testamos** | **Reprodução local em 07/10 ~15h11** (box do Don, mesmas dependências, `COOLIFY_FQDN` definido), medindo o tempo até `/health` = 200. Resultados na tabela abaixo. [VERIFICADO] |
| **Resultado** | **Corrigiu** (no código e na reprodução). Em produção: depende do redeploy. |
| **Próximo ajuste** | Manter `MINIO_ENABLED` ausente/`false` no Coolify. Se um dia o MinIO for usado, ligar a flag e confirmar boot < 15 s. |

Reprodução (07/10, local):

| Cenário do MinIO | Original | zelinho-ia (`MINIO_ENABLED` ausente) | zelinho-ia (`MINIO_ENABLED=true`) |
|---|---|---|---|
| Endpoint respondendo **503** na hora (servidor local) | `/health` 200 em **8,3 s** ("Max retries exceeded… too many 503 error responses") | **2,1 s** ("[MINIO] Disabled") | 2,1 s (falha rápida, 1 retry) |
| Endpoint **inacessível** (IP sem rota, conexão nunca completa) | ver nota ¹ | **3,1 s** | **12,3 s** (timeout de 5 s + 1 retry) |

¹ O original **não respondeu `/health` em 380 s** (6 min 20 s, quando o teste foi interrompido), preso no `bucket_exists()` antes da linha `[MINIO]` aparecer no log. Isso passa de longe o `start-period` de 40 s do healthcheck antigo (CO-02). Os ~2 min 45 s relatados em produção estão entre os dois extremos: depende de como o endpoint falha.

## CO-02 · Healthcheck do Dockerfile: rollbacks

| | |
|---|---|
| **Como estava** | `HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3`. O boot real carrega PaddleOCR, WeasyPrint, Pinecone e o RAG. Somado ao MinIO (CO-01), o container era marcado *unhealthy* e o Coolify fazia **rollback**. [VERIFICADO: Dockerfile original] [RELATO: rollbacks] |
| **O que mudamos** | `HEALTHCHECK --start-period=180s --interval=30s --timeout=10s --retries=5` em `curl -f http://localhost:8000/health` (rodada 2). Dá 180 s de carência e depois tolera 5 falhas seguidas. |
| **Por quê** | Carência compatível com um boot pesado, para o Coolify não reverter um deploy saudável. |
| **Como testamos** | Boot local do zelinho-ia: `/health` 200 em ~2–3 s (sem serviços externos). Não testamos no Coolify. |
| **Resultado** | **Parcial**: corrigido no Dockerfile, **ainda não validado num deploy real**. [NÃO VERIFICADO no Coolify] |
| **Próximo ajuste** | No próximo deploy, anotar quanto tempo o container leva para ficar *healthy*. Conferir se o Coolify tem um healthcheck próprio configurado no painel, porque ele pode sobrepor o do Dockerfile. |

## CO-03 · `--reload` do uvicorn ligado em produção

| | |
|---|---|
| **Como estava** | `app.py` original: `is_production = os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RENDER")` e `enable_reload = not is_production and not is_windows`. No Coolify nenhuma das duas variáveis existe, então **o reload ficava ligado em produção**: um processo vigia os arquivos e reinicia o app quando algo muda (uploads, documentos gerados). [VERIFICADO: código] |
| **O que mudamos** | Só neste repo (07/10, commit `cfe49b6`). `is_production` também detecta `COOLIFY_RESOURCE_UUID`, `COOLIFY_FQDN` ou `ENVIRONMENT=production`. `enable_reload = UVICORN_RELOAD in ("1","true","yes") and not is_production and not is_windows`, ou seja, reload só quando pedido em dev. O `CMD ["python","app.py"]` do Dockerfile não mudou. |
| **Por quê** | Reinícios no meio de requisições e do healthcheck, além de CPU gasta vigiando o disco. |
| **Como testamos** | Reprodução local (07/10 ~15h09) com `COOLIFY_FQDN`/`COOLIFY_RESOURCE_UUID` definidos. **Original**: o log mostra `INFO: Started reloader process [...] using StatReload`. **zelinho-ia**: só `Started server process`, sem reloader. Os dois responderam `/health` 200. [VERIFICADO] |
| **Resultado** | **Corrigiu** no código. **Ainda não está em produção**: essa mudança não estava em nenhum zip entregue antes deste repo. |
| **Próximo ajuste** | Depois do redeploy a partir deste repo, procurar "Started reloader process" no log do Coolify (não pode aparecer). Garantir também que não há *Custom Start Command* com `--reload` no painel. |

## CO-04 · Imagem Docker copiando dados de clientes

| | |
|---|---|
| **Como estava** | O `Dockerfile` original tinha `COPY docs_teste/`, `COPY document_urls/` e `COPY generated_documents/`, pastas versionadas com **dados de casos reais**. Elas iam para dentro da imagem. [VERIFICADO] |
| **O que mudamos** | Esses três `COPY` saíram; entrou `COPY data/ ./data/` (documentos do RAG). `RUN mkdir -p uploads data/documents logs document_urls generated_documents` cria as pastas vazias. O `.dockerignore` tira docs, testes e dados do contexto de build. (07/10, só neste repo) |
| **Por quê** | LGPD e tamanho da imagem. Essas pastas são saída de runtime, não código. |
| **Como testamos** | Boot local a partir de um clone limpo: `/health` 200. Mesmas 46 rotas do original. **Docker não foi buildado** no box (não há Docker aqui). [NÃO VERIFICADO: build da imagem] |
| **Resultado** | **Parcial**: corrigido no repo, build da imagem não testado. |
| **Próximo ajuste** | Primeiro deploy a partir deste repo: confirmar que o build passa e que geração de documento funciona (as pastas existem e são graváveis pelo usuário `zellinho`). |

## CO-05 · `requirements.txt` enxuto e `chardet<6`

| | |
|---|---|
| **Como estava** | `requirements.txt` original com ~135 pacotes, vários sem uso (streamlit, pandas, pyarrow, altair, GitPython, ruff…), e `pinecone` + `pinecone-client` juntos. [VERIFICADO] |
| **O que mudamos** | Ficou só o que é importado pelo código rastreado a partir do `app.py`, com os **pins originais**. Removido `pinecone-client` (conflita com `pinecone` 7). Adicionados `chardet<6`, `tzdata` e `python-multipart`. Testes ficaram em `requirements-dev.txt`. |
| **Por quê** | Build menor e mais rápido (CO-06), menos superfície. `chardet<6`: pedido na especificação do repo. **O motivo técnico exato não está registrado em arquivo**: hipótese de incompatibilidade com `requests`. [NÃO VERIFICADO: motivo do `chardet<6`] |
| **Como testamos** | Instalação limpa num venv Python 3.11 (`uv`): ok, `chardet==5.2.0` resolvido. Boot ok. 194 testes do repo passando. Suíte antiga: 1.268 passam e 65 falham, **iguais** no original e no zelinho-ia. |
| **Resultado** | **Corrigiu** (instala, sobe e passa nos testes). Tempo de build no Coolify não medido. |
| **Próximo ajuste** | Medir o tempo do próximo build no Coolify. |

## CO-06 · Rebuild de ~43 minutos

| | |
|---|---|
| **Como estava** | Build no Coolify levando **~43 min**. Em 06/10 o deploy "não conseguiu, demorou muito tempo", com o Arthur atribuindo isso à internet ruim (ligação de 07/10 06h33). [RELATO] |
| **O que mudamos** | Nada específico no Coolify. Indiretamente: `requirements.txt` enxuto (CO-05), `COPY requirements.txt` antes do código (cache de camada) e `.dockerignore`. |
| **Por quê** | — |
| **Como testamos** | Não testado. **Causas não confirmadas** (sem acesso à VPS). Hipóteses: `paddleocr`/`paddlepaddle` e `scrapling[fetchers]` baixados a cada build sem cache; VPS com pouca CPU/RAM competindo com os containers em execução. [NÃO VERIFICADO] |
| **Resultado** | **Não corrigido / não medido.** |
| **Próximo ajuste** | Não marcar "no cache" no Coolify e anotar o tempo do próximo build. Se continuar alto, buildar fora da VPS (GitHub Actions → registry) e deixar o Coolify só fazer *pull*. |

## CO-07 · Incidente de 2026-10-07: painel lento, Cloudflare 523 e recuperação

| Hora (BRT) | Evento | Evidência |
|---|---|---|
| ~14h00 | Painel `coolify.zellu.tec.br` muito lento: **5–13 s** por página | Observado na sessão; **log não salvo** [RELATO] |
| depois de ~14h00 | Cloudflare responde **HTTP 523** (*Origin is unreachable*): o Cloudflare não alcança o servidor de origem | Observado na sessão; **log não salvo** [RELATO] |
| **14h50** | Painel volta ao normal | Relato do Arthur [RELATO] |
| **15h06** | Conferência do Don: `GET https://coolify.zellu.tec.br/login` → **200 em 0,84 s**; `GET https://ia-service.zellu.tec.br/health` → **200 `{"status":"healthy"}` em 0,86 s** | `curl` do box [VERIFICADO] |

| | |
|---|---|
| **Como estava** | Painel inacessível. Bloqueou o redeploy, o upload dos zips e a conferência das variáveis (necessária para o webhook pós-chamada, EL-17). |
| **O que mudamos** | Nada do nosso lado (sem SSH). Preparamos o pedido para quem administra a VPS (rascunho abaixo). **Não sabemos o que foi feito para recuperar.** |
| **Por quê** | — |
| **Como testamos** | `curl` às 15h06 (tabela acima). |
| **Resultado** | **Recuperado** (por ação de terceiros ou sozinho). **Causa raiz desconhecida.** [NÃO VERIFICADO: causa] |
| **Próximo ajuste** | Perguntar a quem administra a VPS o que aconteceu (CPU/RAM/disco? build preso?). Aproveitar a janela para o redeploy a partir deste repo. |

Rascunho de mensagem para quem administra a VPS (o Arthur envia; **não foi enviada**):

> Oi! Hoje por volta das 14h o painel do Coolify (coolify.zellu.tec.br) ficou bem lento (5–13 s por página) e depois o Cloudflare começou a devolver erro 523 (origem inacessível). Às 14h50 voltou. Você sabe o que aconteceu? Vale olhar `htop`, `df -h` e `docker ps -a`, e ver se algum build ficou preso, porque o último build levou ~43 min. Obrigado!

## Checklist do próximo deploy

- [ ] Variáveis no Coolify colando **só o valor** (sem `NOME=`), CH-09
- [ ] `MINIO_ENABLED` ausente ou `false`, CO-01
- [ ] `OPENAI_API_KEY`, `PINECONE_API_KEY`, `JEV_API_KEY`, `API_KEY_ZELLU_IA`, `ELEVENLABS_WEBHOOK_SECRET` preenchidas
- [ ] Sem comando custom com `--reload`; log sem "Started reloader process", CO-03
- [ ] Tempo até *healthy* anotado, CO-02
- [ ] Tempo de build anotado, CO-06
- [ ] `curl https://ia-service.zellu.tec.br/health` → 200

## Feedback do Arthur / Próximos testes

- [ ] Causa raiz do incidente de 07/10 (resposta de quem administra a VPS), CO-07.
- [ ] Tempo do próximo build com o `requirements.txt` enxuto, CO-05/06.
- [ ] Confirmar no log de produção que não aparece "Started reloader process", CO-03.
- Anotações:
