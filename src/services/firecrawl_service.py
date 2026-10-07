# -*- coding: utf-8 -*-
"""
Servico Firecrawl - Busca de CNPJ na web via Agent mode.

Usa o Firecrawl Agent (modelo spark-1-mini) para buscar informacoes
de empresas na web a partir de nome, CNPJ parcial ou outros dados.
Retorna dados estruturados: nome da empresa, CNPJ e endereco.
"""

from typing import Optional, List, Dict
from dataclasses import dataclass, field
from urllib.parse import urlparse
from pydantic import BaseModel, Field
import requests as http_requests
from config import get_settings

settings = get_settings()


# ============================================================
# Schema Pydantic para saida estruturada do Firecrawl Agent
# ============================================================

class CompanyAddress(BaseModel):
    """Endereco da empresa."""
    logradouro: Optional[str] = Field(None, description="Logradouro (rua, avenida, etc)")
    numero: Optional[str] = Field(None, description="Numero do endereco")
    bairro: Optional[str] = Field(None, description="Bairro")
    municipio: Optional[str] = Field(None, description="Cidade/Municipio")
    uf: Optional[str] = Field(None, description="Estado (sigla UF, ex: SP, RJ)")
    cep: Optional[str] = Field(None, description="CEP")


class CompanyInfo(BaseModel):
    """Informacoes de uma empresa encontrada na web."""
    razao_social: Optional[str] = Field(None, description="Razao social da empresa")
    nome_fantasia: Optional[str] = Field(None, description="Nome fantasia da empresa")
    cnpj: Optional[str] = Field(None, description="CNPJ da empresa (formato XX.XXX.XXX/XXXX-XX)")
    endereco: Optional[CompanyAddress] = Field(None, description="Endereco da empresa")
    situacao_cadastral: Optional[str] = Field(None, description="Situacao cadastral (Ativa, Baixada, etc)")
    atividade_principal: Optional[str] = Field(None, description="Atividade principal (CNAE)")


class CompanySearchWebResult(BaseModel):
    """Resultado da busca de empresas na web via Firecrawl Agent."""
    empresas: List[CompanyInfo] = Field(default_factory=list, description="Lista de empresas encontradas")


class CompanyContact(BaseModel):
    """Um contato encontrado para a empresa."""
    type: str = Field(description="Tipo do contato: 'email', 'phone' ou 'cellphone'")
    content: str = Field(description="O contato em si (ex: juridico@empresa.com.br ou 1133334444)")
    department: Optional[str] = Field(None, description="Departamento (ex: juridico, ouvidoria, SAC, comercial, geral)")
    source: Optional[str] = Field(None, description="De onde veio o contato (ex: site oficial, Reclame Aqui, Google)")


class CompanyContactsResult(BaseModel):
    """Resultado da busca de contatos de uma empresa."""
    company_name: Optional[str] = Field(None, description="Nome da empresa encontrado")
    website: Optional[str] = Field(None, description="URL do site oficial da empresa (ex: https://www.empresa.com.br)")
    contacts: List[CompanyContact] = Field(default_factory=list, description="Lista de contatos encontrados")


# ============================================================
# Resultado interno (dataclass)
# ============================================================

@dataclass
class FirecrawlSearchResult:
    """Resultado da busca via Firecrawl Agent."""
    success: bool
    empresas: list = field(default_factory=list)
    credits_used: Optional[int] = None
    error: Optional[str] = None


@dataclass
class FirecrawlContactResult:
    """Resultado da busca de contatos via Firecrawl Agent."""
    success: bool
    contacts: list = field(default_factory=list)
    website: Optional[str] = None
    credits_used: Optional[int] = None
    error: Optional[str] = None


# ============================================================
# Servico Firecrawl
# ============================================================

class FirecrawlService:
    """
    Servico para busca de empresas na web usando Firecrawl Agent.

    Usa o modelo spark-1-mini para busca precisa de CNPJ,
    razao social, nome fantasia e endereco.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.FIRECRAWL_API_KEY
        self._client = None
        # Cache de favicons por CNPJ (in-memory, perdido em restart)
        self._favicon_cache: Dict[str, Optional[str]] = {}

        if self.api_key:
            # Nem prefixo: o log so precisa dizer se a chave esta configurada.
            print("[FIRECRAWL] Inicializado com API key configurada")
        else:
            print("[FIRECRAWL] AVISO: FIRECRAWL_API_KEY nao configurada")

    def _get_client(self):
        """Retorna o cliente Firecrawl (lazy init)."""
        if self._client is None:
            if not self.api_key:
                raise ValueError("FIRECRAWL_API_KEY nao configurada")
            from firecrawl import Firecrawl
            self._client = Firecrawl(api_key=self.api_key)
        return self._client

    def search_company(
        self,
        prompt: str,
        urls: Optional[List[str]] = None,
        model: str = "spark-1-mini",
        max_credits: Optional[int] = None,
    ) -> FirecrawlSearchResult:
        """
        Busca informacoes de empresa na web via Firecrawl Agent.

        O agent() do SDK ja faz polling ate completed/failed e retorna
        um AgentResponse (Pydantic model) com:
          .success, .status, .data, .credits_used, .error, .expires_at, .id

        Args:
            prompt: Descricao da busca (ex: "Encontre o CNPJ da empresa Petrobras")
            urls: URLs opcionais para focar a busca
            model: Modelo a usar (spark-1-mini default, custo-eficiente)
            max_credits: Limite de creditos para a busca

        Returns:
            FirecrawlSearchResult com empresas encontradas
        """
        try:
            client = self._get_client()

            kwargs = {
                "prompt": prompt,
                "schema": CompanySearchWebResult,
                "model": model,
            }
            if max_credits is not None:
                kwargs["max_credits"] = max_credits
            if urls:
                kwargs["urls"] = urls

            print(f"[FIRECRAWL] Buscando: '{prompt[:100]}...' (model={model})")

            # agent() retorna AgentResponse (Pydantic) - acessar atributos diretamente
            response = client.agent(**kwargs)

            print(f"[FIRECRAWL] Resultado: success={response.success}, status={response.status}, credits={response.credits_used}")

            # Tentar extrair empresas do data SEMPRE (mesmo com status=failed)
            # Quando o agent bate max_credits, status vem "failed" mas pode
            # ter dados parciais em response.data com empresas ja encontradas.
            empresas = []
            if response.data:
                data = response.data
                # data pode ser dict (quando schema e usado, o Agent retorna JSON parseado)
                empresas_raw = data.get("empresas", []) if isinstance(data, dict) else []

                for emp in empresas_raw:
                    if isinstance(emp, dict):
                        endereco_raw = emp.get("endereco") or {}
                        empresas.append({
                            "razao_social": emp.get("razao_social"),
                            "nome_fantasia": emp.get("nome_fantasia"),
                            "cnpj": emp.get("cnpj"),
                            "endereco": {k: endereco_raw.get(k) for k in ["logradouro", "numero", "bairro", "municipio", "uf", "cep"]} if endereco_raw else {},
                            "situacao_cadastral": emp.get("situacao_cadastral"),
                            "atividade_principal": emp.get("atividade_principal"),
                        })

            if empresas:
                if response.status != "completed":
                    print(f"[FIRECRAWL] Resultados parciais: {len(empresas)} empresas (status={response.status}, error={response.error})")
                else:
                    print(f"[FIRECRAWL] Encontradas: {len(empresas)} empresas")

                return FirecrawlSearchResult(
                    success=True,
                    empresas=empresas,
                    credits_used=response.credits_used,
                )
            else:
                error_msg = response.error or f"Status: {response.status}"
                print(f"[FIRECRAWL] Sem resultados: {error_msg}")
                return FirecrawlSearchResult(
                    success=False,
                    error=error_msg,
                    credits_used=response.credits_used,
                )

        except ValueError as e:
            print(f"[FIRECRAWL] Erro de configuracao: {e}")
            return FirecrawlSearchResult(success=False, error=str(e))
        except Exception as e:
            print(f"[FIRECRAWL] Erro inesperado: {e}")
            return FirecrawlSearchResult(success=False, error=f"Erro: {str(e)}")

    # URLs de fontes brasileiras para consulta de CNPJ por nome/razao social.
    # O agent usa essas URLs como ponto de partida, evitando a fase de
    # "descoberta" de fontes e acelerando significativamente a busca.
    CNPJ_SEARCH_URLS = [
        "https://www.google.com",
        "https://casadosdados.com.br",
        "https://cnpj.biz",
        "https://www.econodata.com.br/consulta-empresa",
        "https://consultacnpj.com",
        "https://cnpj.info",
        "https://cnpja.com",
        "https://www.cnpj.ws",
        "https://consultarcnpj.com.br",
    ]

    # Limite de creditos para busca de CNPJ
    # Default do Firecrawl e 2500; 800 e suficiente para buscas web desse tipo.
    CNPJ_SEARCH_MAX_CREDITS = 800

    # Schema JSON para extracao estruturada via Firecrawl Search
    SEARCH_JSON_SCHEMA = {
        "type": "object",
        "required": [],
        "properties": {
            "company_name": {"type": "string"},
            "company_cnpj": {"type": "string"},
            "company_address": {"type": "string"},
            "company_website": {"type": "string"},
        },
    }

    # URL da API REST do Firecrawl Search
    FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"

    def _pre_search(
        self,
        query: str,
        limit: int = 5,
        location: Optional[str] = None,
    ) -> dict:
        """
        Pre-busca via Firecrawl Search REST API (/v2/search) com JSON schema.

        Usa requests direto (nao o SDK) porque o SDK nao suporta o formato
        complexo de formats com type+schema para extracao JSON estruturada.

        Args:
            query: Query de busca em linguagem natural
            limit: Numero maximo de resultados
            location: Localizacao para refinar busca

        Returns:
            Dict com "empresas" (lista de dicts) e "urls" (lista de URLs)
        """
        if not self.api_key:
            print("[FIRECRAWL] Pre-search: API key nao configurada")
            return {"empresas": [], "urls": []}

        try:
            payload = {
                "query": query,
                "sources": ["web"],
                "limit": limit,
                "scrapeOptions": {
                    "onlyMainContent": False,
                    "maxAge": 172800000,  # 2 dias - cache para paginas ja vistas
                    "formats": [
                        {
                            "type": "json",
                            "schema": self.SEARCH_JSON_SCHEMA,
                        }
                    ],
                },
            }
            if location:
                payload["location"] = location

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            print(f"[FIRECRAWL] Pre-search JSON: '{query[:80]}...' (limit={limit})")

            resp = http_requests.post(
                self.FIRECRAWL_SEARCH_URL,
                json=payload,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()

            empresas = []
            urls = []

            if result.get("success") and result.get("data"):
                items = result["data"]
                # data pode ser lista (com scrapeOptions) ou dict com chave "web"
                if isinstance(items, dict):
                    items = items.get("web", [])

                for item in items:
                    url = item.get("url")
                    json_data = item.get("json") or {}

                    if url:
                        urls.append(url)

                    # Extrair empresa do campo json (so se tiver CNPJ)
                    if isinstance(json_data, dict) and json_data.get("company_cnpj"):
                        empresas.append({
                            "razao_social": json_data.get("company_name"),
                            "nome_fantasia": None,
                            "cnpj": json_data["company_cnpj"],
                            "site": json_data.get("company_website") or "",
                            "endereco": {
                                "logradouro": json_data.get("company_address", ""),
                                "numero": "",
                                "bairro": "",
                                "municipio": "",
                                "uf": "",
                                "cep": "",
                            },
                            "situacao_cadastral": None,
                            "atividade_principal": None,
                        })

            print(f"[FIRECRAWL] Pre-search: {len(empresas)} empresas com CNPJ, {len(urls)} URLs")
            for emp in empresas:
                print(f"  -> {emp.get('razao_social')} | CNPJ: {emp.get('cnpj')} | Site: {emp.get('site') or 'N/A'}")

            return {"empresas": empresas, "urls": urls}

        except Exception as e:
            print(f"[FIRECRAWL] Pre-search erro (fallback para agent): {e}")
            return {"empresas": [], "urls": []}

    # Dominios de fontes de consulta de CNPJ - NUNCA usar como site da empresa
    FAVICON_DOMAIN_BLACKLIST = {
        "serasaexperian.com.br",
        "econodata.com.br",
        "cnpj.biz",
        "cnpjcheck.com.br",
        "consultacnpj.com",
        "cnpja.com",
        "cnpj.ws",
        "cnpj.info",
        "casadosdados.com.br",
        "consultarcnpj.com.br",
        "google.com",
        "google.com.br",
        "reclameaqui.com.br",
        "consumidor.gov.br",
    }

    def find_company_favicon(
        self,
        company_name: str,
        cnpj: Optional[str] = None,
    ) -> Optional[str]:
        """
        Busca o favicon do site oficial de uma empresa via Firecrawl Search.

        Faz 1 chamada ao /v2/search com query "{nome} site oficial" e
        scrapeOptions=markdown para receber metadata da pagina (favicon).
        Aplica blacklist de dominios de fontes de consulta - se o primeiro
        resultado cair em uma fonte (Serasa, Econodata, etc), retorna None.

        Resultado e cacheado em memoria por CNPJ para evitar chamadas
        repetidas em conversas diferentes.

        Args:
            company_name: Nome (fantasia ou razao social) da empresa
            cnpj: CNPJ para chave de cache (opcional)

        Returns:
            URL do favicon (metadata.favicon do scrape ou Google S2)
            ou None se nao for possivel determinar com seguranca.
        """
        # Cache lookup
        cache_key = cnpj or company_name
        if cache_key and cache_key in self._favicon_cache:
            return self._favicon_cache[cache_key]

        if not self.api_key or not company_name:
            self._favicon_cache[cache_key] = None
            return None

        try:
            payload = {
                "query": f"{company_name} site oficial",
                "sources": ["web"],
                "limit": 1,
                "scrapeOptions": {
                    "onlyMainContent": False,
                    "maxAge": 172800000,
                    "formats": ["markdown"],
                },
            }
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            resp = http_requests.post(
                self.FIRECRAWL_SEARCH_URL,
                json=payload,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()

            if not (result.get("success") and result.get("data")):
                self._favicon_cache[cache_key] = None
                return None

            items = result["data"]
            if isinstance(items, dict):
                items = items.get("web", [])

            if not items:
                self._favicon_cache[cache_key] = None
                return None

            first = items[0]
            url = first.get("url") or ""
            metadata = first.get("metadata") or {}

            if not url:
                self._favicon_cache[cache_key] = None
                return None

            domain = urlparse(url).netloc.lower()
            # any(b in domain) ja casa subdominios via substring (ex: empresas.serasaexperian.com.br)
            if any(b in domain for b in self.FAVICON_DOMAIN_BLACKLIST):
                print(f"[FIRECRAWL] Favicon descartado (fonte de consulta): {domain} para '{company_name}'")
                self._favicon_cache[cache_key] = None
                return None

            # Prioridade: favicon do metadata (declarado no HTML da empresa).
            # Fallback: Google S2 do dominio real (descoberto agora, nao chutado).
            favicon = metadata.get("favicon") or ""
            if not favicon and domain:
                favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=64"

            favicon = favicon or None
            self._favicon_cache[cache_key] = favicon
            print(f"[FIRECRAWL] Favicon para '{company_name}' ({cnpj or 'sem CNPJ'}): {favicon}")
            return favicon

        except Exception as e:
            print(f"[FIRECRAWL] Erro buscando favicon para '{company_name}': {e}")
            self._favicon_cache[cache_key] = None
            return None

    def search_cnpj(
        self,
        company_name: str,
        location: Optional[str] = None,
        segment: Optional[str] = None,
        model: str = "spark-1-mini",
        max_credits: Optional[int] = None,
    ) -> FirecrawlSearchResult:
        """
        Busca CNPJ de uma empresa pelo nome na web.

        Metodo de conveniencia que monta o prompt automaticamente.
        Usa URLs especificas de fontes de CNPJ para acelerar a busca.

        Args:
            company_name: Nome da empresa
            location: Localizacao (cidade/estado) para refinar a busca
            segment: Segmento/ramo de atividade
            model: Modelo Firecrawl
            max_credits: Limite de creditos (default: CNPJ_SEARCH_MAX_CREDITS)

        Returns:
            FirecrawlSearchResult com empresas encontradas
        """
        location_str = f" que fica em {location}" if location else ""
        segment_str = f", do segmento de {segment}" if segment else ""

        # Prompt simples para pre-search (query style)
        search_query = f"Qual o CNPJ da empresa {company_name}{segment_str}{location_str}?"

        # Pre-busca: search rapido com JSON schema
        pre = self._pre_search(query=search_query, limit=5, location=location)

        # Se search ja encontrou empresas com CNPJ, retorna direto (sem agent)
        if pre["empresas"]:
            print(f"[FIRECRAWL] Pre-search suficiente: {len(pre['empresas'])} empresas - pulando agent")
            return FirecrawlSearchResult(
                success=True,
                empresas=pre["empresas"],
                credits_used=None,
            )

        # Prompt mais elaborado para o agent (sem instrucoes de validacao)
        location_agent = f" na regiao de {location}" if location else " no Brasil"
        agent_prompt = (
            f"Busque empresas com nome parecido com '{company_name}'{segment_str}{location_agent}. "
            f"O nome informado pode ser um apelido, nome fantasia ou nome parcial, entao traga "
            f"ate 5 empresas da mesma regiao e segmento que tenham nome similar ou relacionado."
        )

        # Passa URLs do search ao agent (ou fallback para predefinidas)
        urls = pre["urls"] if pre["urls"] else self.CNPJ_SEARCH_URLS
        print(f"[FIRECRAWL] Pre-search sem CNPJ, acionando agent com {len(urls)} URLs")

        return self.search_company(
            prompt=agent_prompt,
            urls=urls,
            model=model,
            max_credits=max_credits or self.CNPJ_SEARCH_MAX_CREDITS,
        )

    # URLs de fontes para busca de contatos juridicos/ouvidoria
    CONTACT_SEARCH_URLS = [
        "https://www.google.com",
        "https://www.reclameaqui.com.br",
        "https://www.consumidor.gov.br",
    ]

    # Limite de creditos para busca de contatos
    CONTACT_SEARCH_MAX_CREDITS = 500

    def search_company_contacts(
        self,
        company_name: str,
        cnpj: Optional[str] = None,
        model: str = "spark-1-mini",
        max_credits: Optional[int] = None,
    ) -> FirecrawlContactResult:
        """
        Busca contatos juridicos/ouvidoria de uma empresa na web.

        Usa Firecrawl Agent para navegar sites da empresa, Reclame Aqui
        e outras fontes, extraindo emails e telefones de departamentos
        relevantes (juridico, ouvidoria, SAC, comercial).

        Args:
            company_name: Nome da empresa
            cnpj: CNPJ da empresa (para refinar a busca)
            model: Modelo Firecrawl
            max_credits: Limite de creditos

        Returns:
            FirecrawlContactResult com contatos encontrados
        """
        cnpj_str = f" (CNPJ: {cnpj})" if cnpj else ""
        prompt = (
            f"Encontre todos os contatos da empresa '{company_name}'{cnpj_str} no Brasil. "
            f"Busque no site oficial da empresa, no Reclame Aqui, Consumidor.gov.br e outras fontes. "
            f"Preciso especificamente de: "
            f"1. A URL do site oficial da empresa (campo website); "
            f"2. Emails do juridico, ouvidoria, SAC, atendimento e contato geral; "
            f"3. Telefones do SAC, ouvidoria e comercial; "
            f"4. Numeros de celular/WhatsApp de atendimento. "
            f"Inclua a fonte de cada contato encontrado."
        )

        try:
            client = self._get_client()

            kwargs = {
                "prompt": prompt,
                "schema": CompanyContactsResult,
                "model": model,
                "max_credits": max_credits or self.CONTACT_SEARCH_MAX_CREDITS,
                "urls": self.CONTACT_SEARCH_URLS,
            }

            print(f"[FIRECRAWL] Buscando contatos: '{company_name}' (model={model})")

            response = client.agent(**kwargs)

            print(f"[FIRECRAWL] Contatos resultado: success={response.success}, "
                  f"status={response.status}, credits={response.credits_used}")

            contacts = []
            website = None
            if response.data:
                data = response.data
                if isinstance(data, dict):
                    website = data.get("website") or None
                    if website:
                        print(f"[FIRECRAWL] Website encontrado: {website}")
                contacts_raw = data.get("contacts", []) if isinstance(data, dict) else []
                for c in contacts_raw:
                    if isinstance(c, dict) and c.get("content"):
                        contacts.append({
                            "type": c.get("type", "email"),
                            "content": c["content"],
                            "department": c.get("department"),
                            "source": c.get("source"),
                        })

            if contacts or website:
                print(f"[FIRECRAWL] Encontrados: {len(contacts)} contatos para {company_name}")
                return FirecrawlContactResult(
                    success=True,
                    contacts=contacts,
                    website=website,
                    credits_used=response.credits_used,
                )
            else:
                error_msg = response.error or f"Status: {response.status}"
                print(f"[FIRECRAWL] Sem contatos para {company_name}: {error_msg}")
                return FirecrawlContactResult(
                    success=False,
                    error=error_msg,
                    credits_used=response.credits_used,
                )

        except ValueError as e:
            print(f"[FIRECRAWL] Erro de configuracao (contatos): {e}")
            return FirecrawlContactResult(success=False, error=str(e))
        except Exception as e:
            print(f"[FIRECRAWL] Erro ao buscar contatos: {e}")
            return FirecrawlContactResult(success=False, error=f"Erro: {str(e)}")

    def agent_raw(
        self,
        prompt: str,
        urls: Optional[List[str]] = None,
        schema=None,
        model: str = "spark-1-mini",
        max_credits: Optional[int] = None,
    ):
        """
        Executa o Firecrawl Agent diretamente (sem schema padrao).

        Util para buscas genericas que nao sao de CNPJ.

        Args:
            prompt: Descricao da busca
            urls: URLs opcionais
            schema: Schema Pydantic opcional para saida estruturada
            model: Modelo Firecrawl
            max_credits: Limite de creditos

        Returns:
            AgentResponse (Pydantic model) com .success, .status, .data, .credits_used, .error
        """
        try:
            client = self._get_client()

            kwargs = {
                "prompt": prompt,
                "model": model,
            }
            if max_credits is not None:
                kwargs["max_credits"] = max_credits
            if urls:
                kwargs["urls"] = urls
            if schema:
                kwargs["schema"] = schema

            print(f"[FIRECRAWL] Agent raw: '{prompt[:100]}...'")

            response = client.agent(**kwargs)

            print(f"[FIRECRAWL] Agent raw: success={response.success}, status={response.status}, credits={response.credits_used}")
            return response

        except Exception as e:
            print(f"[FIRECRAWL] Agent raw erro: {e}")
            raise


# Instancia singleton
firecrawl_service: Optional[FirecrawlService] = None


def get_firecrawl_service() -> FirecrawlService:
    """Retorna a instancia singleton do FirecrawlService."""
    global firecrawl_service
    if firecrawl_service is None:
        firecrawl_service = FirecrawlService()
    return firecrawl_service
