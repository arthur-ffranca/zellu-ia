# -*- coding: utf-8 -*-
"""
Cliente para busca de empresas no backend Zellu.

Usa o endpoint POST /api/ai/validate-company para buscar empresas
por CNPJ, Nome da Empresa ou Nome Fantasia.
"""

import httpx
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from config import get_settings

settings = get_settings()


@dataclass
class CompanySearchResult:
    """Resultado da busca de empresa."""
    found: bool
    company_id: Optional[str] = None
    cnpj: Optional[str] = None
    company_name: Optional[str] = None
    trade_name: Optional[str] = None
    segment_name: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None

    @property
    def display_name(self) -> str:
        """Retorna o nome para exibição (tradeName ou companyName)."""
        return self.trade_name or self.company_name or ""


@dataclass
class AdvancedSearchResult:
    """Resultado da busca avancada de empresa (search-cnpj-advanced) - V5."""
    success: bool
    results: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    page: int = 1
    limit: int = 10
    error: Optional[str] = None
    request_id: Optional[str] = None
    duration_ms: Optional[int] = None


@dataclass
class CreateCompanyResult:
    """Resultado da criacao de empresa a partir do CNPJ."""
    success: bool
    company_id: Optional[str] = None
    cnpj: Optional[str] = None
    razao_social: Optional[str] = None
    nome_fantasia: Optional[str] = None
    situacao_cadastral: Optional[str] = None
    # Quem respondeu a consulta ao cache de CNPJ (contrato de 2026-09-18):
    #   "mongo"       -> o cache respondeu, tendo achado o CNPJ ou nao
    #   "unavailable" -> o cache nao respondeu; ninguem leu a situacao
    #   None          -> backend antigo, que ainda nao manda o campo
    situacao_cadastral_source: Optional[str] = None
    status: Optional[str] = None
    uf: Optional[str] = None
    municipio: Optional[str] = None
    created: bool = False
    error: Optional[str] = None

    @property
    def situacao_cadastral_confiavel(self) -> bool:
        """
        True so quando a consulta aconteceu E trouxe um valor real.

        "DESCONHECIDA" nunca e confiavel: ou o cache respondeu e nao tinha o
        CNPJ, ou nem respondeu. Nos dois casos ninguem leu a situacao, entao
        nada pode ser afirmado ao cliente sobre ela.

        Com o campo ausente (backend antigo) valemos o que o valor diz: um
        "BAIXADA" de la continua sendo um BAIXADA lido de verdade, e silencia-lo
        ate o campo novo subir seria pior do que o alarme falso que ele corrige.
        """
        if self.situacao_cadastral_source == "unavailable":
            return False
        situacao = (self.situacao_cadastral or "").strip().upper()
        return bool(situacao) and situacao != "DESCONHECIDA"


@dataclass
class ContactPointSearchResult:
    """Resultado da busca de pontos de contato."""
    success: bool
    has_contacts: bool = False
    contacts: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    error: Optional[str] = None


@dataclass
class ContactPointUpsertResult:
    """Resultado do upsert de pontos de contato."""
    success: bool
    company_id: Optional[str] = None
    created: int = 0
    updated: int = 0
    total: int = 0
    error: Optional[str] = None


@dataclass
class ContactAttemptResult:
    """Resultado do log de tentativa de contato."""
    success: bool
    contact_point_id: Optional[str] = None
    attempt_number: Optional[int] = None
    number_of_tries: Optional[int] = None
    is_valid: Optional[bool] = None
    error: Optional[str] = None


class CompanySearchClient:
    """
    Cliente para buscar empresas no backend Zellu.

    Endpoints:
    - POST /api/ai/validate-company (busca simples)
    - POST /api/ai/search-cnpj-advanced (busca avancada)
    - POST /api/ai/create-company-from-cnpj (criar empresa)
    - POST /api/ai/company-contact-points (upsert contatos)
    - POST /api/ai/company-contact-points/search (buscar contatos)
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 10.0
    ):
        """
        Inicializa o cliente de busca de empresas.

        Args:
            base_url: URL base do backend Zellu (ex: https://api.zellu.com.br)
            api_key: API Key para autenticação
            timeout: Timeout em segundos para a requisição
        """
        self.base_url = (base_url or settings.ZELLU_BACKEND_URL).rstrip('/')
        self.api_key = api_key or settings.ZELLU_API_KEY
        self.timeout = timeout
        self.endpoint = "/api/ai/validate-company"

        print(f"[COMPANY-SEARCH] Inicializado - URL: {self.base_url}{self.endpoint}")

    async def search(self, identifier: str) -> CompanySearchResult:
        """
        Busca uma empresa por CNPJ, Nome ou Nome Fantasia.

        Args:
            identifier: CNPJ (formatado ou não), Nome da Empresa ou Nome Fantasia

        Returns:
            CompanySearchResult com os dados da empresa ou erro
        """
        if not identifier or not identifier.strip():
            return CompanySearchResult(
                found=False,
                error="Identificador vazio"
            )

        identifier = identifier.strip()
        url = f"{self.base_url}{self.endpoint}"

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key
        }

        payload = {
            "identifier": identifier
        }

        print(f"[COMPANY-SEARCH] Buscando: '{identifier}'")

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.post(url, json=payload, headers=headers)

                print(f"[COMPANY-SEARCH] Status: {response.status_code}")

                if response.status_code == 200:
                    data = response.json()

                    if data.get("success") and data.get("data"):
                        company = data["data"]

                        result = CompanySearchResult(
                            found=True,
                            company_id=company.get("id"),
                            cnpj=company.get("cnpj"),
                            company_name=company.get("companyName"),
                            trade_name=company.get("tradeName"),
                            segment_name=company.get("segmentName"),
                            status=company.get("status")
                        )

                        print(f"[COMPANY-SEARCH] Encontrada: {result.display_name} (CNPJ: {result.cnpj})")
                        return result
                    else:
                        # success: false ou sem data
                        error_msg = data.get("error", "Empresa não encontrada")
                        print(f"[COMPANY-SEARCH] Não encontrada: {error_msg}")
                        return CompanySearchResult(
                            found=False,
                            error=error_msg
                        )

                elif response.status_code == 400:
                    data = response.json()
                    error_msg = data.get("error", "Empresa não encontrada na base")
                    print(f"[COMPANY-SEARCH] Não encontrada (400): {error_msg}")
                    return CompanySearchResult(
                        found=False,
                        error=error_msg
                    )

                elif response.status_code == 401:
                    print(f"[COMPANY-SEARCH] Erro de autenticação (401)")
                    return CompanySearchResult(
                        found=False,
                        error="Erro de autenticação com o backend"
                    )

                else:
                    print(f"[COMPANY-SEARCH] Erro HTTP: {response.status_code}")
                    return CompanySearchResult(
                        found=False,
                        error=f"Erro HTTP {response.status_code}"
                    )

        except httpx.TimeoutException:
            print(f"[COMPANY-SEARCH] Timeout ao buscar empresa")
            return CompanySearchResult(
                found=False,
                error="Timeout na busca"
            )
        except httpx.RequestError as e:
            print(f"[COMPANY-SEARCH] Erro de conexão: {e}")
            return CompanySearchResult(
                found=False,
                error=f"Erro de conexão: {str(e)}"
            )
        except Exception as e:
            print(f"[COMPANY-SEARCH] Erro inesperado: {e}")
            return CompanySearchResult(
                found=False,
                error=f"Erro inesperado: {str(e)}"
            )

    @staticmethod
    def _build_busca_textual(
        texto: str,
        tipo_busca: str = "radical",
        razao_social: bool = True,
        nome_fantasia: bool = True,
        nome_socio: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Helper para construir o parametro busca_textual V5.

        Args:
            texto: Termo de busca (ex: "Petrobras")
            tipo_busca: "radical" (parcial) ou "exata"
            razao_social: Buscar na razao social
            nome_fantasia: Buscar no nome fantasia
            nome_socio: Buscar no nome de socios
        """
        entry: Dict[str, Any] = {
            "texto": [texto],
            "tipo_busca": tipo_busca,
            "razao_social": razao_social,
            "nome_fantasia": nome_fantasia,
        }
        if nome_socio:
            entry["nome_socio"] = True
        return [entry]

    @staticmethod
    def _normalize_v5_result(raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normaliza resultado V5 (campos aninhados) para formato flat.

        A V5 retorna objetos aninhados (endereco, situacao_cadastral, porte_empresa,
        atividade_principal). Este metodo extrai os campos para nivel flat para
        manter compatibilidade com o intake_agent.
        """
        normalized = dict(raw)

        # Extrair endereco para campos flat
        endereco = raw.get("endereco", {})
        if isinstance(endereco, dict) and endereco:
            normalized["uf"] = endereco.get("uf", "")
            normalized["municipio"] = endereco.get("municipio", "")
            normalized["bairro"] = endereco.get("bairro", "")
            normalized["logradouro"] = endereco.get("logradouro", "")
            normalized["numero"] = endereco.get("numero", "")
            normalized["cep"] = endereco.get("cep", "")

        # Extrair situacao_cadastral de objeto para string
        sit = raw.get("situacao_cadastral", {})
        if isinstance(sit, dict):
            normalized["situacao_cadastral"] = sit.get("situacao_atual", "")

        # Extrair porte_empresa de objeto para strings
        porte = raw.get("porte_empresa", {})
        if isinstance(porte, dict):
            normalized["porte"] = porte.get("descricao", "")
            normalized["porte_codigo"] = porte.get("codigo", "")

        # Extrair atividade principal
        atv = raw.get("atividade_principal", {})
        if isinstance(atv, dict):
            normalized["cnae_principal"] = atv.get("codigo", "")
            normalized["cnae_principal_descricao"] = atv.get("descricao", "")

        return normalized

    async def advanced_search(
        self,
        busca_textual: Optional[List[Dict[str, Any]]] = None,
        cnpj: Optional[List[str]] = None,
        cnpj_raiz: Optional[List[str]] = None,
        uf: Optional[List[str]] = None,
        municipio: Optional[List[str]] = None,
        bairro: Optional[List[str]] = None,
        cep: Optional[List[str]] = None,
        endereco_numero: Optional[List[str]] = None,
        ddd: Optional[List[str]] = None,
        telefone: Optional[List[str]] = None,
        situacao_cadastral: Optional[List[str]] = None,
        matriz_filial: Optional[str] = None,
        codigo_atividade_principal: Optional[List[str]] = None,
        codigo_atividade_secundaria: Optional[List[str]] = None,
        incluir_atividade_secundaria: Optional[bool] = None,
        codigo_natureza_juridica: Optional[List[str]] = None,
        porte_empresa: Optional[Dict[str, Any]] = None,
        capital_social: Optional[Dict[str, Any]] = None,
        data_abertura: Optional[Dict[str, Any]] = None,
        mei: Optional[Dict[str, Any]] = None,
        simples: Optional[Dict[str, Any]] = None,
        mais_filtros: Optional[Dict[str, Any]] = None,
        excluir: Optional[Dict[str, Any]] = None,
        limite: int = 10,
        pagina: int = 1,
        tipo_resultado: Optional[str] = None,
    ) -> AdvancedSearchResult:
        """
        Busca avancada de empresas via API Casa dos Dados V5.

        Endpoint: POST /api/ai/search-cnpj-advanced

        Args:
            busca_textual: Busca por nome (radical ou exata). Use _build_busca_textual() helper.
                Ex: [{"texto": ["Petrobras"], "tipo_busca": "radical", "razao_social": True}]
            cnpj: Lista de CNPJs especificos (ex: ["33000167000101"])
            cnpj_raiz: Raiz do CNPJ - 8 primeiros digitos (ex: ["33000167"])
            uf: Lista de estados (ex: ["SP", "RJ"])
            municipio: Lista de municipios (ex: ["sao paulo"])
            bairro: Lista de bairros (ex: ["centro"])
            cep: Lista de CEPs (ex: ["01310100"])
            endereco_numero: Numeros de endereco (ex: ["100"])
            ddd: Lista de DDDs (ex: ["11", "21"])
            telefone: Lista de telefones (ex: ["5135277255"])
            situacao_cadastral: Situacao em MAIUSCULA (ex: ["ATIVA"])
            matriz_filial: "MATRIZ" ou "FILIAL" (MAIUSCULA)
            codigo_atividade_principal: CNAEs principais (ex: ["6201500"])
            codigo_atividade_secundaria: CNAEs secundarias
            incluir_atividade_secundaria: Incluir CNAEs secundarias na busca
            codigo_natureza_juridica: Codigos de natureza juridica
            porte_empresa: Porte como objeto (ex: {"codigos": ["01"]})
            capital_social: Range de capital (ex: {"minimo": 100000, "maximo": 1000000})
            data_abertura: Filtro de data (ex: {"inicio": "2020-01-01", "fim": "2024-12-31"})
            mei: Filtro MEI como objeto (ex: {"optante": True})
            simples: Filtro Simples Nacional como objeto (ex: {"optante": True})
            mais_filtros: Filtros avancados (ex: {"somente_matriz": True, "com_email": True})
            excluir: CNPJs a excluir (ex: {"cnpj": ["00000000000191"]})
            limite: Resultados por pagina (max 1000, default 10)
            pagina: Numero da pagina (default 1)
            tipo_resultado: "simples" (4 campos) ou "completo" (20+ campos)

        Returns:
            AdvancedSearchResult com lista de empresas encontradas (campos normalizados)
        """
        url = f"{self.base_url}/api/ai/search-cnpj-advanced"

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key
        }

        # Montar payload V5
        payload: Dict[str, Any] = {}
        if busca_textual:
            payload["busca_textual"] = busca_textual
        if cnpj:
            payload["cnpj"] = cnpj
        if cnpj_raiz:
            payload["cnpj_raiz"] = cnpj_raiz
        if uf:
            payload["uf"] = uf
        if municipio:
            payload["municipio"] = municipio
        if bairro:
            payload["bairro"] = bairro
        if cep:
            payload["cep"] = cep
        if endereco_numero:
            payload["endereco_numero"] = endereco_numero
        if ddd:
            payload["ddd"] = ddd
        if telefone:
            payload["telefone"] = telefone
        if situacao_cadastral:
            payload["situacao_cadastral"] = situacao_cadastral
        if matriz_filial:
            payload["matriz_filial"] = matriz_filial
        if codigo_atividade_principal:
            payload["codigo_atividade_principal"] = codigo_atividade_principal
        if codigo_atividade_secundaria:
            payload["codigo_atividade_secundaria"] = codigo_atividade_secundaria
        if incluir_atividade_secundaria is not None:
            payload["incluir_atividade_secundaria"] = incluir_atividade_secundaria
        if codigo_natureza_juridica:
            payload["codigo_natureza_juridica"] = codigo_natureza_juridica
        if porte_empresa:
            payload["porte_empresa"] = porte_empresa
        if capital_social:
            payload["capital_social"] = capital_social
        if data_abertura:
            payload["data_abertura"] = data_abertura
        if mei:
            payload["mei"] = mei
        if simples:
            payload["simples"] = simples
        if mais_filtros:
            payload["mais_filtros"] = mais_filtros
        if excluir:
            payload["excluir"] = excluir
        payload["limite"] = limite
        payload["pagina"] = pagina
        if tipo_resultado:
            payload["tipo_resultado"] = tipo_resultado

        # Log resumido
        busca_texto = ""
        if busca_textual and len(busca_textual) > 0:
            busca_texto = ", ".join(busca_textual[0].get("texto", []))
        cnpj_log = f", cnpj={cnpj}" if cnpj else ""
        cnpj_raiz_log = f", cnpj_raiz={cnpj_raiz}" if cnpj_raiz else ""
        print(f"[COMPANY-SEARCH-ADV] V5 Buscando: texto='{busca_texto}'{cnpj_log}{cnpj_raiz_log}, uf='{uf}', municipio='{municipio}', matriz_filial='{matriz_filial}', situacao='{situacao_cadastral}'")

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.post(url, json=payload, headers=headers)

                print(f"[COMPANY-SEARCH-ADV] Status: {response.status_code}")

                if response.status_code == 200:
                    data = response.json()

                    if data.get("success"):
                        # V5: resultados dentro de data.results
                        data_wrapper = data.get("data", {})
                        if isinstance(data_wrapper, dict):
                            raw_results = data_wrapper.get("results", [])
                            total = data_wrapper.get("total", len(raw_results))
                            resp_page = data_wrapper.get("page", pagina)
                            resp_limit = data_wrapper.get("limit", limite)
                        elif isinstance(data_wrapper, list):
                            # Fallback se data vier como lista direta
                            raw_results = data_wrapper
                            total = data.get("total", len(raw_results))
                            resp_page = pagina
                            resp_limit = limite
                        else:
                            raw_results = []
                            total = 0
                            resp_page = pagina
                            resp_limit = limite

                        # Normalizar resultados V5 para formato flat
                        results = [self._normalize_v5_result(r) for r in raw_results]

                        print(f"[COMPANY-SEARCH-ADV] Encontradas: {total} empresas")
                        return AdvancedSearchResult(
                            success=True,
                            results=results,
                            total=total,
                            page=resp_page,
                            limit=resp_limit,
                            request_id=data.get("request_id"),
                            duration_ms=data.get("duration_ms"),
                        )
                    else:
                        error_msg = data.get("error", "Nenhuma empresa encontrada")
                        print(f"[COMPANY-SEARCH-ADV] Nao encontrada: {error_msg}")
                        return AdvancedSearchResult(
                            success=True,
                            results=[],
                            total=0,
                            error=error_msg
                        )

                elif response.status_code == 400:
                    data = response.json()
                    error_msg = data.get("error", "Parametros invalidos")
                    print(f"[COMPANY-SEARCH-ADV] Erro 400: {error_msg}")
                    return AdvancedSearchResult(success=False, error=error_msg)

                elif response.status_code == 401:
                    print(f"[COMPANY-SEARCH-ADV] Erro de autenticacao (401)")
                    return AdvancedSearchResult(success=False, error="Erro de autenticacao")

                elif response.status_code == 403:
                    data = response.json()
                    error_msg = data.get("error", "Permissao insuficiente")
                    print(f"[COMPANY-SEARCH-ADV] Erro 403: {error_msg}")
                    return AdvancedSearchResult(success=False, error=error_msg)

                elif response.status_code == 429:
                    print(f"[COMPANY-SEARCH-ADV] Rate limit excedido (429)")
                    return AdvancedSearchResult(success=False, error="Rate limit excedido. Aguarde 60s.")

                else:
                    print(f"[COMPANY-SEARCH-ADV] Erro HTTP: {response.status_code}")
                    return AdvancedSearchResult(success=False, error=f"Erro HTTP {response.status_code}")

        except httpx.TimeoutException:
            print(f"[COMPANY-SEARCH-ADV] Timeout")
            return AdvancedSearchResult(success=False, error="Timeout na busca avancada")
        except httpx.RequestError as e:
            print(f"[COMPANY-SEARCH-ADV] Erro de conexao: {e}")
            return AdvancedSearchResult(success=False, error=f"Erro de conexao: {str(e)}")
        except Exception as e:
            print(f"[COMPANY-SEARCH-ADV] Erro inesperado: {e}")
            return AdvancedSearchResult(success=False, error=f"Erro inesperado: {str(e)}")

    async def create_company_from_cnpj(self, cnpj: str) -> CreateCompanyResult:
        """
        Cria ou busca empresa a partir do CNPJ.

        Endpoint: POST /api/ai/create-company-from-cnpj

        Args:
            cnpj: CNPJ da empresa (formatado ou nao)

        Returns:
            CreateCompanyResult com dados da empresa criada/encontrada
        """
        url = f"{self.base_url}/api/ai/create-company-from-cnpj"

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key
        }

        payload = {"cnpj": cnpj}

        print(f"[COMPANY-CREATE] Criando empresa: CNPJ={cnpj}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.post(url, json=payload, headers=headers)

                print(f"[COMPANY-CREATE] Status: {response.status_code}")

                if response.status_code == 200 or response.status_code == 201:
                    data = response.json()

                    if data.get("success"):
                        company = data.get("data", data)

                        # Normalizar situacao_cadastral (V5 retorna objeto, V2 retorna string)
                        raw_situacao = company.get("situacaoCadastral") or company.get("situacao_cadastral")
                        if isinstance(raw_situacao, dict):
                            situacao_str = raw_situacao.get("situacao_atual", "")
                        else:
                            situacao_str = raw_situacao

                        # Quem respondeu a consulta ao cache de CNPJ.
                        # O backend nunca manda situacao_cadastral null; se vier
                        # vazio, tratamos como DESCONHECIDA em vez de deixar None
                        # circular pelo fluxo e virar texto na tela.
                        source = (
                            company.get("situacao_cadastral_source")
                            or company.get("situacaoCadastralSource")
                        )
                        if not (situacao_str or "").strip():
                            situacao_str = "DESCONHECIDA"

                        # Normalizar endereco (V5 retorna objeto aninhado)
                        raw_uf = company.get("uf")
                        raw_municipio = company.get("municipio")
                        endereco = company.get("endereco", {})
                        if isinstance(endereco, dict) and endereco:
                            raw_uf = raw_uf or endereco.get("uf", "")
                            raw_municipio = raw_municipio or endereco.get("municipio", "")

                        result = CreateCompanyResult(
                            success=True,
                            company_id=company.get("id") or company.get("companyId"),
                            cnpj=company.get("cnpj"),
                            razao_social=company.get("razaoSocial") or company.get("razao_social") or company.get("companyName"),
                            nome_fantasia=company.get("nomeFantasia") or company.get("nome_fantasia") or company.get("tradeName"),
                            situacao_cadastral=situacao_str,
                            situacao_cadastral_source=source,
                            status=company.get("status"),
                            uf=raw_uf,
                            municipio=raw_municipio,
                            # `created` vem DENTRO de `data`. O fallback por 201 nunca
                            # entrou: a rota sempre respondeu 200 nos dois caminhos de
                            # sucesso (confirmado pela plataforma em 2026-09-18).
                            created=bool(company.get("created", data.get("created", False)))
                        )

                        display_name = result.nome_fantasia or result.razao_social or ""
                        print(f"[COMPANY-CREATE] Sucesso: {display_name} (created={result.created}, situacao={result.situacao_cadastral}, source={result.situacao_cadastral_source})")
                        return result
                    else:
                        error_msg = data.get("error", "Erro ao criar empresa")
                        print(f"[COMPANY-CREATE] Falha: {error_msg}")
                        return CreateCompanyResult(success=False, error=error_msg)

                elif response.status_code == 400:
                    data = response.json()
                    error_msg = data.get("error", "CNPJ invalido")
                    print(f"[COMPANY-CREATE] Erro 400: {error_msg}")
                    return CreateCompanyResult(success=False, error=error_msg)

                elif response.status_code == 404:
                    print(f"[COMPANY-CREATE] CNPJ nao encontrado na Receita (404)")
                    return CreateCompanyResult(success=False, error="CNPJ nao encontrado na Receita Federal")

                elif response.status_code == 401:
                    print(f"[COMPANY-CREATE] Erro de autenticacao (401)")
                    return CreateCompanyResult(success=False, error="Erro de autenticacao")

                else:
                    print(f"[COMPANY-CREATE] Erro HTTP: {response.status_code}")
                    return CreateCompanyResult(success=False, error=f"Erro HTTP {response.status_code}")

        except httpx.TimeoutException:
            print(f"[COMPANY-CREATE] Timeout")
            return CreateCompanyResult(success=False, error="Timeout ao criar empresa")
        except httpx.RequestError as e:
            print(f"[COMPANY-CREATE] Erro de conexao: {e}")
            return CreateCompanyResult(success=False, error=f"Erro de conexao: {str(e)}")
        except Exception as e:
            print(f"[COMPANY-CREATE] Erro inesperado: {e}")
            return CreateCompanyResult(success=False, error=f"Erro inesperado: {str(e)}")

    async def search_contact_points(
        self,
        cnpj: Optional[str] = None,
        cnpj_raiz: Optional[str] = None,
        only_valid: bool = True,
        only_active: bool = True,
        only_best_contact: bool = False,
        include_attempt_history: bool = False,
    ) -> ContactPointSearchResult:
        """
        Busca pontos de contato de uma empresa pelo CNPJ ou CNPJ raiz.

        Endpoint: POST /api/ai/company-contact-points/search

        Args:
            cnpj: CNPJ completo para busca exata (14 digitos)
            cnpj_raiz: Primeiros 8 digitos - retorna matriz + filiais
            only_valid: Filtrar apenas contatos validos
            only_active: Filtrar apenas contatos ativos
            only_best_contact: Filtrar apenas o melhor contato de cada tipo
            include_attempt_history: Incluir historico de tentativas (max 10 por contato)

        Returns:
            ContactPointSearchResult com contatos encontrados

        Note:
            Um de cnpj ou cnpj_raiz e obrigatorio.
        """
        if not cnpj and not cnpj_raiz:
            return ContactPointSearchResult(success=False, error="cnpj ou cnpj_raiz e obrigatorio")

        url = f"{self.base_url}/api/ai/company-contact-points/search"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key
        }
        payload: Dict[str, Any] = {
            "onlyValid": only_valid,
            "onlyActive": only_active,
            "onlyBestContact": only_best_contact,
            "includeAttemptHistory": include_attempt_history,
        }
        if cnpj:
            payload["cnpj"] = cnpj
        if cnpj_raiz:
            payload["cnpjRaiz"] = cnpj_raiz

        search_by = f"CNPJ={cnpj}" if cnpj else f"CNPJRaiz={cnpj_raiz}"
        print(f"[CONTACT-POINTS] Buscando contatos: {search_by}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.post(url, json=payload, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        companies = data.get("data", {}).get("companies", [])
                        all_contacts = []
                        for company in companies:
                            for cp in company.get("contactPoints", []):
                                all_contacts.append(cp)

                        has_contacts = len(all_contacts) > 0
                        print(f"[CONTACT-POINTS] Encontrados: {len(all_contacts)} contatos")
                        return ContactPointSearchResult(
                            success=True,
                            has_contacts=has_contacts,
                            contacts=all_contacts,
                            total=len(all_contacts),
                        )
                    else:
                        return ContactPointSearchResult(
                            success=True,
                            has_contacts=False,
                            error=data.get("error"),
                        )

                elif response.status_code == 401:
                    print(f"[CONTACT-POINTS] Erro de autenticacao (401)")
                    return ContactPointSearchResult(success=False, error="API key invalida ou ausente")

                elif response.status_code == 404:
                    print(f"[CONTACT-POINTS] Empresa nao encontrada (404)")
                    return ContactPointSearchResult(success=True, has_contacts=False)

                else:
                    print(f"[CONTACT-POINTS] Erro HTTP: {response.status_code}")
                    return ContactPointSearchResult(
                        success=False,
                        error=f"Erro HTTP {response.status_code}"
                    )

        except httpx.TimeoutException:
            print(f"[CONTACT-POINTS] Timeout na busca de contatos")
            return ContactPointSearchResult(success=False, error="Timeout")
        except Exception as e:
            print(f"[CONTACT-POINTS] Erro: {e}")
            return ContactPointSearchResult(success=False, error=str(e))

    @staticmethod
    def _sanitize_contacts(contacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Valida e sanitiza contatos antes de enviar para a API.

        Aplica limites da API:
        - Max 50 contatos por request
        - content: max 500 chars
        - notes: max 1000 chars
        - type deve ser 'email', 'phone' ou 'cellphone'
        - source deve ser um dos valores pre-definidos (fallback 'unknown')
        - isValid e obrigatorio
        """
        valid_types = {"email", "phone", "cellphone"}
        valid_sources = {"official_website", "government", "external", "unknown"}
        sanitized = []

        for c in contacts[:50]:
            contact_type = c.get("type", "").lower()
            if contact_type not in valid_types:
                continue

            content = str(c.get("content", "")).strip()
            if not content:
                continue

            source = str(c.get("source", "") or "").lower()
            if source not in valid_sources:
                source = "unknown"

            sanitized.append({
                "type": contact_type,
                "content": content[:500],
                "source": source,
                "isValid": c.get("isValid", True),
                "isBestContact": c.get("isBestContact", False),
                "isActive": c.get("isActive", True),
                "notes": str(c.get("notes", ""))[:1000] if c.get("notes") else None,
            })

        return sanitized

    async def upsert_contact_points(
        self,
        cnpj: str,
        contacts: List[Dict[str, Any]],
    ) -> ContactPointUpsertResult:
        """
        Cria/atualiza pontos de contato de uma empresa.

        Endpoint: POST /api/ai/company-contact-points

        Aplica validacao/sanitizacao antes de enviar:
        - Max 50 contatos, content max 500 chars, notes max 1000 chars
        - Tipos aceitos: 'email', 'phone', 'cellphone'

        Args:
            cnpj: CNPJ da empresa (14 digitos, com ou sem pontuacao)
            contacts: Lista de contatos:
                [{"type": "email", "content": "x@y.com", "isValid": True,
                  "isBestContact": False, "isActive": True, "notes": "..."}]

        Returns:
            ContactPointUpsertResult com contagem de criados/atualizados
        """
        sanitized = self._sanitize_contacts(contacts)
        if not sanitized:
            return ContactPointUpsertResult(success=False, error="Nenhum contato valido para enviar")

        url = f"{self.base_url}/api/ai/company-contact-points"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key
        }
        payload = {
            "cnpj": cnpj,
            "contacts": sanitized,
        }

        print(f"[CONTACT-POINTS] Upserting {len(sanitized)} contatos para CNPJ={cnpj}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.post(url, json=payload, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        result_data = data.get("data", {})
                        print(f"[CONTACT-POINTS] Upsert OK: created={result_data.get('created')}, "
                              f"updated={result_data.get('updated')}")
                        return ContactPointUpsertResult(
                            success=True,
                            company_id=result_data.get("companyId"),
                            created=result_data.get("created", 0),
                            updated=result_data.get("updated", 0),
                            total=result_data.get("total", 0),
                        )
                    else:
                        error_msg = data.get("error", "Erro desconhecido")
                        print(f"[CONTACT-POINTS] Upsert falha: {error_msg}")
                        return ContactPointUpsertResult(success=False, error=error_msg)

                elif response.status_code == 400:
                    data = response.json()
                    error_msg = data.get("error", "Dados invalidos")
                    print(f"[CONTACT-POINTS] Upsert erro 400: {error_msg}")
                    return ContactPointUpsertResult(success=False, error=error_msg)

                elif response.status_code == 401:
                    print(f"[CONTACT-POINTS] Upsert erro de autenticacao (401)")
                    return ContactPointUpsertResult(success=False, error="API key invalida ou ausente")

                elif response.status_code == 404:
                    print(f"[CONTACT-POINTS] Upsert empresa nao encontrada (404)")
                    return ContactPointUpsertResult(success=False, error="Empresa nao encontrada no backend")

                else:
                    print(f"[CONTACT-POINTS] Upsert erro HTTP: {response.status_code}")
                    return ContactPointUpsertResult(
                        success=False,
                        error=f"Erro HTTP {response.status_code}",
                    )

        except httpx.TimeoutException:
            print(f"[CONTACT-POINTS] Upsert timeout")
            return ContactPointUpsertResult(success=False, error="Timeout")
        except Exception as e:
            print(f"[CONTACT-POINTS] Upsert erro: {e}")
            return ContactPointUpsertResult(success=False, error=str(e))

    async def log_contact_attempt(
        self,
        contact_point_id: str,
        attempt_result: str,
        result_details: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ContactAttemptResult:
        """
        Registra uma tentativa de contato e atualiza o ponto de contato.

        Endpoint: POST /api/ai/company-contact-points/log-attempt

        Auto-invalidacao: se attemptResult for 'invalid' ou 'wrong_number',
        o campo isValid do contato e automaticamente setado como false.

        Args:
            contact_point_id: UUID do ponto de contato
            attempt_result: Resultado da tentativa:
                'success' | 'failed' | 'no_answer' | 'busy' |
                'invalid' | 'voicemail' | 'wrong_number'
            result_details: Detalhes em texto (max 1000 chars)
            metadata: JSON arbitrario (ex: {"agent": "AI-001", "channel": "email"})

        Returns:
            ContactAttemptResult com dados atualizados do contato
        """
        valid_results = {"success", "failed", "no_answer", "busy", "invalid", "voicemail", "wrong_number"}
        if attempt_result not in valid_results:
            return ContactAttemptResult(
                success=False,
                error=f"attemptResult invalido: {attempt_result}. Aceitos: {', '.join(valid_results)}"
            )

        url = f"{self.base_url}/api/ai/company-contact-points/log-attempt"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key
        }
        payload: Dict[str, Any] = {
            "contactPointId": contact_point_id,
            "attemptResult": attempt_result,
        }
        if result_details:
            payload["resultDetails"] = result_details[:1000]
        if metadata:
            payload["metadata"] = metadata

        print(f"[CONTACT-POINTS] Log attempt: contactPointId={contact_point_id}, result={attempt_result}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.post(url, json=payload, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        result_data = data.get("data", {})
                        cp = result_data.get("contactPoint", {})
                        attempt_log = result_data.get("attemptLog", {})
                        print(f"[CONTACT-POINTS] Log attempt OK: tries={cp.get('numberOfTries')}, "
                              f"valid={cp.get('isValid')}")
                        return ContactAttemptResult(
                            success=True,
                            contact_point_id=cp.get("id"),
                            attempt_number=attempt_log.get("attemptNumber"),
                            number_of_tries=cp.get("numberOfTries"),
                            is_valid=cp.get("isValid"),
                        )
                    else:
                        return ContactAttemptResult(
                            success=False,
                            error=data.get("error", "Erro desconhecido"),
                        )

                elif response.status_code == 400:
                    data = response.json()
                    error_msg = data.get("error", "Dados invalidos")
                    print(f"[CONTACT-POINTS] Log attempt erro 400: {error_msg}")
                    return ContactAttemptResult(success=False, error=error_msg)

                elif response.status_code == 401:
                    print(f"[CONTACT-POINTS] Log attempt erro de autenticacao (401)")
                    return ContactAttemptResult(success=False, error="API key invalida ou ausente")

                elif response.status_code == 404:
                    data = response.json()
                    error_msg = data.get("error", "Contact point nao encontrado")
                    print(f"[CONTACT-POINTS] Log attempt 404: {error_msg}")
                    return ContactAttemptResult(success=False, error=error_msg)

                else:
                    print(f"[CONTACT-POINTS] Log attempt erro HTTP: {response.status_code}")
                    return ContactAttemptResult(
                        success=False,
                        error=f"Erro HTTP {response.status_code}",
                    )

        except httpx.TimeoutException:
            print(f"[CONTACT-POINTS] Log attempt timeout")
            return ContactAttemptResult(success=False, error="Timeout")
        except Exception as e:
            print(f"[CONTACT-POINTS] Log attempt erro: {e}")
            return ContactAttemptResult(success=False, error=str(e))


# Instância global (será inicializada no main.py)
company_search_client: Optional[CompanySearchClient] = None


def get_company_search_client() -> CompanySearchClient:
    """Retorna a instância global do cliente de busca de empresas."""
    global company_search_client
    if company_search_client is None:
        company_search_client = CompanySearchClient()
    return company_search_client
