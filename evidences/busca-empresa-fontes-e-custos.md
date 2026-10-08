# Busca de empresa: o que implementamos + quanto custa

Data: out/2026. Estado: commitado na `main` (`b849d74` + `178cec6`), testado ao vivo.

## 1. Resumo em 5 linhas

- O cliente informa nome + cidade → **Mongo primeiro**; se não achar, **internet** (nunca repete a Casa dos Dados depois de um NÃO).
- Descoberta gratuita: **Yahoo + DuckDuckGo Lite** (Yahoo BR, Bing e Google de reserva).
- Descoberta paga opcional: **Brave Search API**, só quando há chave no `.env`.
- Leitura do CNPJ em cascata: **cnpj.biz → BrasilAPI → CNPJá → CNPJ.ws** (a primeira que responder vale).
- Tudo que acha na internet **vira registro no Mongo** — cada busca paga de hoje é uma busca grátis de amanhã.

## 2. O que mudou no código

### 2.1 Mongo — busca por qualquer variável, sem índice novo
- `src/services/company_lookup_cache.py`: aceita CNPJ (em qualquer campo), nome, fantasia, logradouro, bairro, município, CEP e filtro matriz/filial.
- Usa **só os índices que já existem** na `cnpjs` (razao_social, cnpj, text em razao_social etc.). Sem índice para o campo = filtro sobre consulta indexada, nunca varredura.
- Mantido o circuit breaker do pacote Grok (pausa de 30 s se o Mongo cair).
- `src/utils/data/municipios_br.json` restaurado (5313 cidades) — o filtro de cidade dependia dele.

### 2.2 Escada — NÃO pede referência, não repete Casa dos Dados
- `src/services/company_search_ladder.py` + `company_intake.py` (`_search_via_firecrawl`) + `company_search_pipeline.py`:
- Cliente disse NÃO → pergunta **uma referência melhor antes** de buscar de novo.
- Depois de NÃO (ou de referência dada), a busca é **só Mongo + internet**. A API da Casa dos Dados não é chamada de novo (`company_casa_skipped` no log).
- O site casadosdados saiu dos diretórios da busca profunda (devolveria as mesmas empresas da 1ª tentativa).

### 2.3 Descoberta web (quem acha o link)
- `src/services/scrapling_company_search.py`:
- **Brave pago primeiro, quando há chave** (`_brave_links`, header `X-Subscription-Token`, `country=BR`, `search_lang=pt-br`).
- **Yahoo + DuckDuckGo Lite juntos** (Yahoo exige seguir redirect; DuckDuckGo limita após ~2 consultas seguidas).
- Reservas só se ninguém trouxer nada: Yahoo BR, Bing, Google. (Bing devolve lixo para robô; Google exige JavaScript — por isso são último recurso.)
- Fonte que bloquear (202/403/429, ou 401 no Brave) **fica 10 minutos em pausa**.
- O CNPJ é extraído **do próprio link** (`cnpj.biz/<cnpj>`, `econodata/.../<cnpj>-NOME`) — não depende do layout de cada diretório.

### 2.4 Leitura do CNPJ (quem confirma os dados)
- `read_company(cnpj)`: tenta `cnpj.biz`; se falhar, **BrasilAPI → CNPJá → CNPJ.ws** (cada uma pausa 60 s se limitar). Cache de 1 hora.
- Nome conferido contra o pedido (`_name_matches`); na busca profunda vale 1 termo relevante da marca (o filtro de cidade acontece depois, no `localize_companies`).

### 2.5 Anexos (pacote Grok + ajustes)
- Pacote de velocidade aplicado: timeout do LlamaParse, atalho da camada de texto, cache de parse por sha256.
- `evidence_metadata` entrega `notice` (aviso de anexo não lido vai ao front); recibo do chat inclui o aviso.
- Download: hosts do backend liberados, retry com `x-api-key` **só na Zellu** (nunca no storage de terceiro), redirect para o storage seguido sem levar a chave, erro gravado com `host` sem a URL assinada.
- Cada anexo lido ganha **título + briefing** no registro do caso (LLM no pós-caso, heurística local de reserva) — entra no `record_payload` e no documento do caso.

## 3. Prova ao vivo

### 3.1 Primeira leva — Yahoo como descoberta (10 empresas, 10/10)

| Empresa | Local | Tempo | CNPJ achado |
|---|---|---|---|
| Casas Bahia | Shopping Dom Pedro Campinas | 2,0 s | 33041260090805 (+2 filiais) |
| Magazine Luiza | Franca | 1,7 s | 47960950000121 (+1 filial) |
| Lojas Renner | Porto Alegre | 1,9 s | 92754738000162 (+1 filial) |
| Droga Raia | Av. Paulista, São Paulo | 3,4 s | 60605664007895 (+2 filiais) |
| Bradesco | Osasco | 4,5 s | 60746948000112 (+2, via BrasilAPI) |
| Petz | Morumbi, São Paulo | 2,3 s | 18328118004015 (+1, via BrasilAPI) |
| Localiza | Belo Horizonte | 2,5 s | 16670085006609 (+2, via BrasilAPI) |
| Gol Linhas Aéreas | São Paulo | 3,9 s | 07575651007757 (+2, via BrasilAPI) |
| Natura | Cajamar | 3,8 s | 71673990002030 (+2, via BrasilAPI) |
| Ambev | São Paulo | 1,9 s | 07526557000100 (+1, via BrasilAPI) |

Detalhe honesto: o primeiro run dessa leva deu 2/10 — o Yahoo responde com redirect (307) e o código não seguia. Corrigido o `follow_redirects`, foi 10/10. Metade das leituras saiu pela BrasilAPI porque o cnpj.biz recusou sob volume — a cascata se pagou no primeiro dia.

### 3.2 Segunda leva — Brave ligado + busca profunda (5 empresas, 5/5)

Primeira tentativa (Brave + Yahoo mesclados):

| Empresa | Local | Tempo | Resultado |
|---|---|---|---|
| Kabum | — | 2,7 s | 5 candidatos, matriz 05570714001716 (Extrema-MG) |
| Gracie Barra | Valinhos/SP | 2,8 s | 2 candidatos, franquia local 26304195000157 |
| Rede Sol | Mirassol/SP | 3,9 s | 3 candidatos, 08243869000179 (via BrasilAPI) |
| Higa Atacadista | Campinas/SP | 5,4 s | sem resultado → profunda |
| Rede Villa Simpatia | Campos do Jordão/SP | 3,8 s | sem resultado → profunda |

Busca profunda (com a referência, várias consultas, 25 s de orçamento):

| Empresa | Tempo | Candidatos | Achado |
|---|---|---|---|
| Higa Atacadista | 11,9 s | 6 | matriz 46029724000169 + 2 filiais (Campinas) |
| Rede Villa Simpatia | 9,6 s | 7 | filial Campos do Jordão 07722158001781 |

Nota: na profunda da Higa veio também um homônimo inapto (N Higa, 54500392000187) — o filtro de situação/município na hora de oferecer continua decidindo a candidata certa.

## 4. Custos: Firecrawl x Brave x grátis

### 4.1 Tabela

| Fonte | Custo | Cartão | O que entrega |
|---|---|---|---|
| Yahoo + DuckDuckGo Lite | **US$ 0** | não | descoberta (links) |
| BrasilAPI + CNPJá + CNPJ.ws | **US$ 0** | não | leitura/dados do CNPJ |
| cnpj.biz (leitura de página) | **US$ 0** | não | leitura/dados do CNPJ |
| **Brave Search API** | **US$ 5 de crédito/mês (~1000 buscas)**; depois **US$ 5/1000 buscas** | **sim** | descoberta (links de qualidade, 0,7 s) |
| Firecrawl | **1000 créditos grátis/mês (= 500 buscas)**; depois **US$ 16/mês (2500 buscas)** ou **US$ 83/mês (50k)** | não (no grátis) | busca + leitura de página |

*Firecrawl: cada busca custa 2 créditos a cada 10 resultados.*

### 4.2 Cenário: 5000 buscas no 1º mês

- **Firecrawl**: 500 grátis + 4500 pagas → precisa de plano pago (≈ **US$ 32**, 2× o de US$ 16, para ~5000 buscas).
- **Brave**: ~1000 grátis (crédito de US$ 5) + 4000 pagas × US$ 5/1000 = **US$ 20**.
- **Nosso desenho (Brave + grátis)**: a descoberta Brave custa no cenário acima, mas a **leitura é sempre grátis** (BrasilAPI/cnpj.biz) — no Firecrawl, busca e leitura consomem o mesmo crédito.

### 4.3 O volante: 5k registros a mais no Mongo

Aqui está o ponto central: **toda empresa achada na internet é salva no Mongo** (`save_company_lookup_results`). Então:

- As 5000 buscas do 1º mês viram **~5000 registros a mais no Mongo**.
- A partir do 2º mês, esses casos viram **acerto no Mongo = custo zero** (nem Brave, nem Firecrawl).
- Quanto mais roda, menor o custo marginal: o gasto pago de hoje compra gratuidade permanente. Por isso o Brave de US$ 5 no 1º mês se paga — ele acelera o enchimento da base, e a base cheia dispensa busca paga.

### 4.4 Recomendação

1. Rodar o 1º mês no **Brave (crédito grátis) + fontes grátis** — custo US$ 0, enchendo o Mongo.
2. Só avaliar Firecrawl se alguma categoria de empresa se mostrar invisível para Yahoo/Brave (até agora: 15/15 encontradas sem ele).
3. Nunca pagar leitura de CNPJ — BrasilAPI + cnpj.biz cobrem de graça.

## 5. Chaves e flags (`.env`)

```
BRAVE_API_KEY=...            # descoberta paga; sem ela, Yahoo + DDG assumem
FIRECRAWL_API_KEY=...        # hoje: não usado pela busca de empresa
MONGODB_URI=...              # cache/registro (cnpjsdb.cnpjs)
LLAMAPARSE_TIMEOUT_S=45
DOCUMENT_TEXT_LAYER_FAST_PATH=true
EVIDENCE_PROGRESS_MESSAGE_ENABLED=true
```

## 6. Arquivos tocados

- `src/services/scrapling_company_search.py` — Yahoo/DDG/Brave, cascata de leitura, bloqueio por fonte.
- `src/services/company_search_ladder.py`, `company_intake.py`, `company_search_pipeline.py` — escada sem repetir Casa dos Dados.
- `src/services/company_lookup_cache.py` — busca por qualquer variável nos índices existentes.
- `src/services/case_evidence.py` — notice, download com chave, título + briefing.
- `src/utils/data/municipios_br.json` — restaurado (filtro de cidade).
- `config.py`, `.env.example` — `BRAVE_API_KEY` / `BRAVE_SEARCH_API_KEY`.
- Testes: `test_company_search_sources.py` (Yahoo/DDG/Brave/APIs), `test_escada_busca_empresa.py`, `test_case_evidence.py` (título/briefing), suites de anexo.
