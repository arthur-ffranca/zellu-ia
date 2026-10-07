# Bifrost (gateway LLM): rota "edge" e telemetria. Log de-para

**Bifrost** (`github.com/maximhq/bifrost`) é um gateway de LLM compatível com a API da OpenAI. Objetivo: separar e medir no **Datadog** o tráfego LLM que sai pela rota **edge** e pela rota **origin** (latência p95 e taxa de erro por rota, provedor e modelo).

Legenda de evidência em [`README.md`](README.md#legenda). "Original" = zip do repositório (arquivos de 2026-10-06 14h16).

---

## BF-01 · Como marcar o tráfego: plugin no gateway × variável no serviço (decisão)

| | |
|---|---|
| **Como estava** | Nenhuma marcação de rota. Pergunta do Arthur (ligação de 07/10 06h33): "como que eu faria dentro do Datadog… pra identificar quando tá sendo edge no roteamento com o LLM? Uma tag, algo do tipo". [RELATO] |
| **O que mudamos** | 1ª proposta: um plugin `edgeroute` no Bifrost marcando `route=edge/origin` no span e no log (ver anexo). O Arthur sugeriu: "não seria mais fácil colocar essa tag dentro do script?… aí eu conseguiria ver pelo Coolify". Decisão: **variável de ambiente `EDGE_ROUTE` no serviço** (no estilo das `MINIO_*`), que manda o header lido pelo gateway. |
| **Por quê** | Mais simples de ligar e desligar pelo painel do Coolify, sem build de plugin. Ressalva registrada: a tag fica no chamador, então outra aplicação que passe pelo Bifrost sem essa env não é marcada. |
| **Como testamos** | — (decisão) |
| **Resultado** | **Decidido** → BF-02. |
| **Próximo ajuste** | — |

## BF-02 · Flag `EDGE_ROUTE` e header `X-Bifrost-Route` nos clientes OpenAI

| | |
|---|---|
| **Como estava** | `config.py` sem flags de rota. Os 7 clientes OpenAI eram criados sem `default_headers`: `src/llm.py` (`client_options` = `api_key`, `timeout`, `max_retries`), `staged_intake_flow.py`, `document_case_understanding.py`, `document_analyzer.py`, `audio_transcription.py`, `embedding_service.py`, `embedding_cache.py`. [VERIFICADO] |
| **O que mudamos** | Rodada 3 (`zellinho_edge_route.zip`, 07/10 06h41). `config.py`: `EDGE_ROUTE: bool = False`, `EDGE_ROUTE_HEADER: str = "X-Bifrost-Route"` e a função `llm_default_headers()` → `{EDGE_ROUTE_HEADER: "edge"}` se `EDGE_ROUTE=true`, senão `{}` (e `{}` se as settings forem inválidas). Os 7 clientes passaram a receber `default_headers=llm_default_headers()`. |
| **Por quê** | Ver BF-01. Com a flag desligada, nada muda. |
| **Como testamos** | 07/10 ~15h10: `EDGE_ROUTE=false` → `{}`; `EDGE_ROUTE=true` → `{'X-Bifrost-Route': 'edge'}`. Suíte do repo: 194 testes passando. [VERIFICADO] |
| **Resultado** | **Parcial**: implementado e desligado por padrão. **Ainda não tem efeito**: o serviço chama a OpenAI direto (não existe `OPENAI_BASE_URL` no config), então o header não passa por nenhum Bifrost. A OpenAI ignora o header, sem efeito colateral. O JEV não usa o SDK OpenAI e **não é afetado**. |
| **Próximo ajuste** | Implementar `OPENAI_BASE_URL` opcional apontando para o Bifrost. Só depois ligar `EDGE_ROUTE=true` em homologação (Coolify → Environment Variables → `EDGE_ROUTE=true` → Redeploy). |

## BF-03 · Plugin `edgeroute` e Datadog (desenho)

| | |
|---|---|
| **Como estava** | Nada. |
| **O que mudamos** | Só **desenho**: código Go de exemplo do plugin (`HTTPTransportPreHook` lê o header; `PostLLMHook` grava `route` no span e um log JSON), `config.json`, Datadog Agent com OTLP, consultas e monitores (anexo abaixo). |
| **Por quê** | Para o Datadog ver a rota, alguém precisa ler o header no gateway. |
| **Como testamos** | Interfaces e assinaturas conferidas no código do Bifrost (clone de 07/10). O plugin **não foi compilado** nem implantado. [NÃO VERIFICADO: build e exportação de atributos customizados pelo plugin otel] |
| **Resultado** | **Não implementado** (desenho). |
| **Próximo ajuste** | Decidir se o Bifrost entra (quem hospeda, onde roda o Datadog Agent). Compilar o `.so` na mesma versão do `bifrost-http` e validar `@route:edge` no Datadog. No streaming, logar só no último chunk. |

---

## Anexo: desenho do plugin `edgeroute` e do lado Datadog (pesquisa de 2026-10-07, NÃO implantado)

O conteúdo abaixo é a pesquisa original (código do Bifrost clonado em 07/10/2026). Legenda: **[VERIFICADO]** conferido no código/docs; **[SUPOSIÇÃO]** decisão de design ou comportamento não confirmado. O código Go é um *sketch* — ainda não foi buildado nem implantado.

### A.1. Onde interceptar

#### Interfaces de plugin reais (`core/schemas/plugin.go`) [VERIFICADO]
```go
type BasePlugin interface {
    GetName() string
    Cleanup() error
}

type HTTPTransportPlugin interface {
    BasePlugin
    HTTPTransportPreAuthHook(ctx *BifrostContext, req *HTTPRequest) (*HTTPResponse, error)
    HTTPTransportPreHook(ctx *BifrostContext, req *HTTPRequest) (*HTTPResponse, error)
    HTTPTransportPostHook(ctx *BifrostContext, req *HTTPRequest, resp *HTTPResponse) error
    HTTPTransportResponseHeadersHook(ctx *BifrostContext, req *HTTPRequest, resp *HTTPResponseMetadata) error
    HTTPTransportStreamChunkHook(ctx *BifrostContext, req *HTTPRequest, chunk *BifrostStreamChunk) (*BifrostStreamChunk, error)
}

type LLMPlugin interface {
    BasePlugin
    PreRequestHook(ctx *BifrostContext, req *BifrostRequest) error
    PreLLMHook(ctx *BifrostContext, req *BifrostRequest) (*BifrostRequest, *LLMPluginShortCircuit, error)
    PostLLMHook(ctx *BifrostContext, resp *BifrostResponse, bifrostErr *BifrostError) (*BifrostResponse, *BifrostError, error)
}
```
- `HTTPRequest{Method, Path string; Headers, Query map[string]string; Body []byte; PathParams map[string]string}` com `CaseInsensitiveHeaderLookup(key string) string`. [VERIFICADO]
- Hooks HTTP **só rodam no bifrost-http** (não no Go SDK). [VERIFICADO – comentário no plugin.go]
- Ordem: `HTTPTransportPreHook` → `PreRequestHook` → `PreLLMHook` → provider → `PostLLMHook` → `HTTPTransportPostHook`. [VERIFICADO – comentário em `PreRequestHook`]

#### Tracing nativo [VERIFICADO]
- `core/schemas/tracer.go`: `type Tracer interface` com `GetSpanHandleByID(traceID string, spanID *string) SpanHandle`, `SetAttribute(handle SpanHandle, key string, value any)`, `AddEvent(...)`, `StartSpan(...)`, `EndSpan(...)`.
- Chaves de contexto (`core/schemas/bifrost.go`): `BifrostContextKeyTracer` (Tracer), `BifrostContextKeyTraceID` (string, handle do trace store), `BifrostContextKeySpanID` (string, span atual), `BifrostContextKeyRequestID`.
- `plugins/otel` (`OtelPlugin`, `Init(ctx, *Config, logger, *modelcatalog.ModelCatalog, version)`) converte o `schemas.Trace` em OTLP (`converter.go: convertTraceToResourceSpan`) e exporta via `http`/`grpc` (`ProtocolHTTP`/`ProtocolGRPC`). Ou seja: **não usamos o SDK OTel direto** — escrevemos atributos no Tracer do Bifrost e o plugin otel os exporta.
- [SUPOSIÇÃO] que atributos arbitrários setados via `Tracer.SetAttribute` saem no span OTLP como attributes (o converter itera atributos do span; validar com um teste local).
- Dados de resposta: `resp.GetExtraFields()` → `BifrostResponseExtraFields{RoutingInfo RoutingInfo; Latency int64 /*ms*/; ...}`; `RoutingInfo{Provider ModelProvider; Model string; Key string; IsFallback bool; ...}`. Requisição: `req.GetRequestFields() (provider, model, fallbacks)`. Erro: `bifrostErr.EffectiveHTTPStatus() int`. [VERIFICADO]

#### Como detectar "edge" [SUPOSIÇÃO – escolha de design]
Ordem de precedência:
1. Header `X-Bifrost-Route: edge` (ou `X-Edge: 1`) lido no `HTTPTransportPreHook` e gravado no contexto (chave própria `edgeRouteKey`).
2. Fallback: provider/key pertencente a uma lista configurada (ex.: key `edge-*` ou provider custom `edge-openai` apontando para base_url do edge) — checado no `PostLLMHook` via `RoutingInfo`.
3. Caso contrário → `origin`.

### A.2. Código: plugin `edgeroute`

`plugins/edgeroute/main.go` (formato de plugin dinâmico `.so` conforme `docs/plugins/writing-go-plugin.mdx`: funções exportadas `Init(config any) error`, `GetName()`, `PreLLMHook`, `PostLLMHook`, `HTTPTransportPreHook`...). [VERIFICADO formato; código = sketch]

```go
package main

import (
	"encoding/json"
	"os"
	"strings"
	"time"

	"github.com/maximhq/bifrost/core/schemas"
)

type edgeCfg struct {
	Header       string   `json:"header"`        // default "X-Bifrost-Route"
	EdgeKeys     []string `json:"edge_keys"`     // prefixos de key name => edge
	EdgeProviders []string `json:"edge_providers"`
}

var cfg = edgeCfg{Header: "X-Bifrost-Route"}

// chaves próprias de contexto (tipo schemas.BifrostContextKey é string)
const (
	edgeRouteKey schemas.BifrostContextKey = "edgeroute-route"
	edgeStartKey schemas.BifrostContextKey = "edgeroute-start"
)

var enc = json.NewEncoder(os.Stdout) // stdout -> Datadog Agent (docker/k8s logs)

func Init(config any) error {
	if b, err := json.Marshal(config); err == nil {
		_ = json.Unmarshal(b, &cfg)
	}
	if cfg.Header == "" {
		cfg.Header = "X-Bifrost-Route"
	}
	return nil
}

func GetName() string { return "edgeroute" }
func Cleanup() error  { return nil }

// 1) Transporte: lê o header antes do core
func HTTPTransportPreHook(ctx *schemas.BifrostContext, req *schemas.HTTPRequest) (*schemas.HTTPResponse, error) {
	v := strings.ToLower(req.CaseInsensitiveHeaderLookup(cfg.Header))
	if v == "edge" || req.CaseInsensitiveHeaderLookup("X-Edge") == "1" {
		ctx.SetValue(edgeRouteKey, "edge")
	}
	return nil, nil
}

func HTTPTransportPostHook(ctx *schemas.BifrostContext, req *schemas.HTTPRequest, resp *schemas.HTTPResponse) error {
	return nil
}

func PreRequestHook(ctx *schemas.BifrostContext, req *schemas.BifrostRequest) error { return nil }

// 2) Marca o span no início da chamada LLM
func PreLLMHook(ctx *schemas.BifrostContext, req *schemas.BifrostRequest) (*schemas.BifrostRequest, *schemas.LLMPluginShortCircuit, error) {
	ctx.SetValue(edgeStartKey, time.Now())
	if r, _ := ctx.Value(edgeRouteKey).(string); r != "" {
		setSpanAttr(ctx, "route", r)
	}
	return req, nil, nil
}

// 3) Resolve rota final, atributo no span + log JSON
func PostLLMHook(ctx *schemas.BifrostContext, resp *schemas.BifrostResponse, bErr *schemas.BifrostError) (*schemas.BifrostResponse, *schemas.BifrostError, error) {
	var info schemas.RoutingInfo
	var latency int64
	if resp != nil {
		if ef := resp.GetExtraFields(); ef != nil {
			info, latency = ef.RoutingInfo, ef.Latency
		}
	}
	route, _ := ctx.Value(edgeRouteKey).(string)
	if route == "" {
		route = "origin"
		if isEdgeTarget(info) {
			route = "edge"
		}
	}
	if latency == 0 {
		if t0, ok := ctx.Value(edgeStartKey).(time.Time); ok {
			latency = time.Since(t0).Milliseconds()
		}
	}
	status := 200
	if bErr != nil {
		status = bErr.EffectiveHTTPStatus()
	}

	setSpanAttr(ctx, "route", route)
	setSpanAttr(ctx, "route.is_fallback", info.IsFallback)

	reqID, _ := ctx.Value(schemas.BifrostContextKeyRequestID).(string)
	_ = enc.Encode(map[string]any{
		"timestamp":  time.Now().UTC().Format(time.RFC3339Nano),
		"service":    "bifrost",
		"message":    "llm_request_completed",
		"route":      route,
		"provider":   string(info.Provider),
		"model":      info.Model,
		"latency_ms": latency,
		"status":     status,
		"is_error":   bErr != nil,
		"request_id": reqID,
		// correlação log<->trace (ver seção 3; [SUPOSIÇÃO] precisa do trace id W3C)
		"dd.trace_id": ctx.Value(schemas.BifrostContextKeyExportTraceID),
	})
	return resp, bErr, nil
}

func isEdgeTarget(info schemas.RoutingInfo) bool {
	for _, p := range cfg.EdgeProviders {
		if string(info.Provider) == p {
			return true
		}
	}
	for _, k := range cfg.EdgeKeys {
		if strings.HasPrefix(info.Key, k) {
			return true
		}
	}
	return false
}

// Escreve atributo no span corrente via Tracer nativo do Bifrost.
func setSpanAttr(ctx *schemas.BifrostContext, k string, v any) {
	tr, _ := ctx.Value(schemas.BifrostContextKeyTracer).(schemas.Tracer)
	tid, _ := ctx.Value(schemas.BifrostContextKeyTraceID).(string)
	if tr == nil || tid == "" {
		return // NoOpTracer / sem trace
	}
	if sid, ok := ctx.Value(schemas.BifrostContextKeySpanID).(string); ok && sid != "" {
		tr.SetAttribute(tr.GetSpanHandleByID(tid, &sid), k, v) // span LLM atual
	}
	tr.SetAttribute(tr.GetSpanHandleByID(tid, nil), k, v) // root span (facilita busca no DD)
}
```
Notas:
- `BifrostContextKeyExportTraceID` existe em `core/schemas/bifrost.go` (string, W3C trace ID, também devolvido no header `x-bifrost-trace-id`) [VERIFICADO]. Para correlação em Datadog, converter os 64 bits baixos para decimal, ou confiar na correlação OTel nativa do DD (`trace_id` hex funciona com OTLP).
- Streaming: `PostLLMHook` roda por chunk; `Latency` do último chunk = total [VERIFICADO comentário]. Para evitar log por chunk, logar só no chunk final (sketch omitido) [SUPOSIÇÃO].
- Alternativa sem `.so`: implementar os mesmos métodos num struct e passar em `schemas.BifrostConfig{LLMPlugins: []schemas.LLMPlugin{p}}` (campo `LLMPlugins []LLMPlugin` verificado em `core/schemas/bifrost.go`) no `bifrost.Init(...)` do Go SDK. Lembrar: hooks HTTP não rodam no SDK — use `ctx.SetValue(edgeRouteKey,"edge")` no código chamador.

#### Registro (config.json) [VERIFICADO formato em docs]
```bash
go build -buildmode=plugin -o edgeroute.so main.go   # build na mesma plataforma/versão do bifrost-http
```
```json
{
  "plugins": [
    { "name": "otel", "enabled": true,
      "config": { "collector_url": "http://datadog-agent:4318",
                  "trace_type": "genai_extension", "protocol": "http",
                  "service_name": "bifrost" } },
    { "name": "edgeroute", "enabled": true, "path": "/plugins/edgeroute.so",
      "config": { "header": "X-Bifrost-Route", "edge_keys": ["edge-"], "edge_providers": [] } }
  ]
}
```
(`grpc` → `"protocol":"grpc"` e `collector_url` `datadog-agent:4317`.) Ordem relativa de plugins: ver `docs/plugins/sequencing.mdx`.

### A.3. Lado Datadog

#### OTLP → Datadog Agent [conhecimento DD; não está no repo]
```bash
DD_API_KEY=<definir no ambiente do Datadog Agent — nunca no repo>
DD_SITE=datadoghq.com
DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_GRPC_ENDPOINT=0.0.0.0:4317
DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT=0.0.0.0:4318
DD_LOGS_ENABLED=true
DD_LOGS_CONFIG_CONTAINER_COLLECT_ALL=true      # coleta stdout JSON do bifrost
DD_APM_ENABLED=true
```
Alternativa: OpenTelemetry Collector com `datadog` exporter / `datadog/connector`.

#### Mapeamento de tags
- `service.name` (resource, de `service_name`) → `service:bifrost`.
- Atributo de span `route` → `@route` (span attribute pesquisável como `@route:edge`). Para virar **tag de métrica APM** (trace metrics `trace.*` por rota) é preciso adicioná-la como *peer/primary tag* ou usar `DD_APM_FEATURES`/span-based metrics (abaixo). [SUPOSIÇÃO de config conforme DD]
- `deployment.environment` (resource) → `env:`; atributos `gen_ai.*` do genai_extension → `@gen_ai.request.model` etc.
- Log JSON: campos viram atributos `@route`, `@provider`, `@model`, `@latency_ms`, `@status` — crie facets (medida para `latency_ms`).

#### Consultas
- Trace search: `service:bifrost @route:edge` · erros: `service:bifrost @route:edge status:error` · comparativo: `service:bifrost @route:(edge OR origin)` agrupando por `@route`.
- Logs: `service:bifrost @message:llm_request_completed @route:edge @status:>=500`; analytics: `p95(@latency_ms) by @route, @provider`.

#### Métricas / monitores
Métrica baseada em spans (APM → Generate Metrics): `bifrost.llm.latency` filtrada `service:bifrost`, group by `@route,@provider,@model` (distribution sobre duração). Ou log-based metric sobre `@latency_ms`.

Monitor p95:
```
p95:bifrost.llm.latency{service:bifrost,route:edge} > 2000   (last 10m)
```
Monitor taxa de erro edge vs origin (log-based metrics `bifrost.llm.requests` e `bifrost.llm.errors` com `@is_error:true`):
```
sum(last_10m):sum:bifrost.llm.errors{route:edge}.as_count() / sum:bifrost.llm.requests{route:edge}.as_count() * 100 > 5
```
Dashboard: timeseries `p95 by {route}` + `errors/requests by {route}` lado a lado.

#### Resumo de verificação
Verificado: interfaces/assinaturas de hooks, `HTTPRequest`, `Tracer` e chaves de contexto, `RoutingInfo`/`Latency`, `EffectiveHTTPStatus`, `LLMPlugins`, formato `.so` + `config.json`, config do plugin otel. Suposições: header/critério de edge, exportação de atributos customizados pelo converter otel, detalhes de config Datadog.

## Feedback do Arthur / Próximos testes

- [ ] Decidir se o Bifrost entra (quem hospeda, onde roda o Datadog Agent), BF-03.
- [ ] Implementar `OPENAI_BASE_URL` opcional para apontar o SDK para o Bifrost, BF-02.
- [ ] Compilar o plugin `edgeroute.so` e validar que atributos customizados saem no span OTLP, BF-03.
- [ ] Ligar `EDGE_ROUTE=true` em homologação e conferir `@route:edge` no Datadog, BF-02.
- Anotações:
