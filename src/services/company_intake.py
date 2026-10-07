"""Company discovery and form operations, independent of conversational agents."""
from src.llm import ChatModel
from typing import List, Optional, Dict, Any
from agents.state import ConversationState
import re
import json
import asyncio
import unicodedata
import hashlib
from datetime import datetime
from src.utils.intake_dates import TZ, resolve_date_mentions, temporal_prompt
from urllib.parse import urlparse
from src.company_search_client import CompanySearchClient
from src.services.company_lookup_cache import save_company_lookup_results, find_company_lookup_results, find_company_by_cnpj
from src.services.scrapling_company_search import get_scrapling_company_search, CompanySearchResult
from src.services.company_icon import favicon_for, resolve_company_icons
from src.utils.company_normalizer import normalize_cnpj, normalize_company_list, normalize_company_name, is_valid_cnpj
from src.utils.phone import (
    PHONE_FORMAT_HINT,
    PHONE_FORM_PATTERN,
    is_valid_phone,
    normalize_phone,
)

# Mapeamento de nomes de estado (sem acento) para sigla UF
ESTADO_PARA_UF = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM",
    "bahia": "BA", "ceara": "CE", "distrito federal": "DF", "espirito santo": "ES",
    "goias": "GO", "maranhao": "MA", "mato grosso": "MT", "mato grosso do sul": "MS",
    "minas gerais": "MG", "para": "PA", "paraiba": "PB", "parana": "PR",
    "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
    "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO",
    "roraima": "RR", "santa catarina": "SC", "sao paulo": "SP",
    "sergipe": "SE", "tocantins": "TO",
}
UF_VALIDAS = set(ESTADO_PARA_UF.values())


def _remove_accents(text: str) -> str:
    """Remove acentos de um texto para comparacao."""
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


class CompanyIntakeService:
    def __init__(self, llm=None, company_search_client=None):
        self.llm = llm
        self.company_search_client = company_search_client
        self.required_fields = []
        self.name = 'intake'

    def log_decision(self, event, data=None):
        import logging
        logging.getLogger(__name__).info('company operation: %s', event)

    def _build_dynamic_form(self, form_name: str, fields: List[Dict[str, Any]], submit_label: str = "Enviar") -> Dict[str, Any]:
        """
        Monta o objeto event_type para formulários dinâmicos.

        Args:
            form_name: Nome identificador do formulário
            fields: Lista de campos do formulário
            submit_label: Texto do botão de submit

        Returns:
            Dict com a estrutura event_type para o messagedata
        """
        return {
            "type": "form",
            "mode": "editable",
            "form_name": form_name,
            "submit_label": submit_label,
            "fields": fields
        }


    def _build_company_select_form(self, companies: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Dropdown de filial sem expor CNPJ no payload público.

        O front recebe um candidate_id opaco, nome/local e ícone. O CNPJ fica
        somente em company_search_results no backend e é resolvido após o clique.
        """
        cleaned = normalize_company_list(companies)
        options = []
        for company in cleaned.companies:
            cnpj = company.get("cnpj", "")
            if not cnpj:
                continue
            razao_social = company.get("razao_social") or company.get("razaoSocial") or company.get("companyName") or ""
            nome_fantasia = company.get("nome_fantasia") or company.get("nomeFantasia") or company.get("tradeName") or ""
            display_name = nome_fantasia or razao_social or "Empresa"
            # Nome da empresa + local (bairro/logradouro e cidade/UF) em toda opcao.
            label = self._format_company_option_label(display_name, company)
            candidate_id = company.get("candidate_id") or ("cmp_" + hashlib.sha256(
                (cnpj + "|" + label).encode("utf-8")
            ).hexdigest()[:16])
            company["candidate_id"] = candidate_id

            favicon_url = (company.get("favicon_url") or company.get("faviconUrl")
                           or favicon_for(company.get("site") or company.get("website") or company.get("homepage")))
            # faviconUrl e o campo do contrato; os demais sao o mesmo valor para
            # renderizadores que leem logoUrl/iconUrl/imageUrl.
            options.append({
                "value": candidate_id,
                "label": label,
                "faviconUrl": favicon_url,
                "logoUrl": favicon_url,
                "iconUrl": favicon_url,
                "imageUrl": favicon_url,
                "companyName": display_name,
            })

        options.append({
            "value": "none",
            "label": "Nenhuma dessas é a loja",
            "faviconUrl": None,
            "logoUrl": None,
            "iconUrl": None,
        })
        fields = [{
            "name": "selected_company_id",
            "type": "select",
            "label": "Qual dessas unidades é a correta?",
            "options": options,
            "required": True,
        }]
        return self._build_dynamic_form("company_select", fields, "Selecionar")


    def _company_field(self, company: Dict[str, Any], key: str) -> str:
        value = company.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

        endereco = company.get("endereco")
        if isinstance(endereco, dict):
            value = endereco.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        return ""


    def _format_company_option_label(self, display_name: str, company: Dict[str, Any]) -> str:
        # O cadastro da Receita vem em caixa alta ("VILA BRANDINA"); no dropdown
        # o endereco segue a mesma caixa do nome da empresa.
        def place(key):
            value = self._company_field(company, key)
            return normalize_company_name(value) if value.isupper() else value

        municipio = place("municipio")
        uf = self._company_field(company, "uf").upper()
        bairro = place("bairro")
        logradouro = place("logradouro")
        numero = self._company_field(company, "numero")

        # UI do select corta texto longo: mostrar apenas nome e local.
        local_parts = []
        if bairro:
            local_parts.append(bairro)
        elif logradouro and numero:
            local_parts.append(f"{logradouro}, {numero}")
        elif logradouro:
            local_parts.append(logradouro)

        location = f"{municipio}/{uf}" if municipio and uf else municipio or uf
        if location:
            local_parts.append(location)

        local = " - ".join(local_parts)
        parts = [part for part in (display_name, local) if part]
        return " - ".join(parts)


    def _build_company_refine_form(self) -> Dict[str, Any]:
        """Form: Coletar dados adicionais para refinar busca de empresa."""
        fields = [
            {
                "name": "company_segment",
                "type": "text",
                "label": "Qual o segmento/ramo da empresa?",
                "placeholder": "Ex: loja de roupas, restaurante, banco",
                "required": False,
                "tooltip": "Informe o tipo de atividade da empresa"
            },
            {
                "name": "company_location",
                "type": "text",
                "label": "Em qual cidade/estado fica a empresa?",
                "placeholder": "Ex: Sao Paulo/SP",
                "required": False,
                "tooltip": "Informe a cidade e estado para refinar a busca"
            }
        ]
        return self._build_dynamic_form("company_refine", fields, "Buscar")


    def _build_cnpj_input_form(self) -> Dict[str, Any]:
        """Form: Input de CNPJ manual."""
        fields = [
            {
                "name": "opposing_party_cnpj",
                "type": "text",
                "label": "CNPJ da empresa",
                "placeholder": "00.000.000/0000-00",
                "required": True,
                "tooltip": "Informe o CNPJ no formato 00.000.000/0000-00",
                "validation": {
                    "pattern": r"^\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}$",
                    "message": "CNPJ invalido. Use o formato 00.000.000/0000-00"
                }
            }
        ]
        return self._build_dynamic_form("cnpj_input", fields, "Confirmar")


    def _build_company_confirm_form(self, company: Dict[str, Any]) -> Dict[str, Any]:
        """Confirma a unidade pelo nome/local, sem expor CNPJ ao cliente."""
        nome = company.get("nome_fantasia") or company.get("razao_social") or "Empresa"
        if nome.lower().startswith('empresa cnpj'):
            nome = 'empresa do cadastro informado'
        uf = company.get("uf", "")
        municipio = company.get("municipio", "")
        endereco = company.get("logradouro", "")
        numero = company.get("numero", "")
        bairro = company.get("bairro", "")
        details = []
        if bairro:
            details.append(bairro)
        if endereco:
            details.append(f"{endereco}{', ' + numero if numero else ''}")
        if municipio and uf:
            details.append(f"{municipio}/{uf}")
        elif municipio:
            details.append(municipio)
        detail_str = " · ".join(details)
        label = f"Sim, é a {nome}" + (f" · {detail_str}" if detail_str else "")
        favicon_url = (company.get("favicon_url") or company.get("faviconUrl")
                       or favicon_for(company.get("site") or company.get("website")))
        fields = [{
            "name": "company_confirmed",
            "type": "radio",
            "label": "É essa unidade?",
            "options": [
                {"value": "yes", "label": label, "faviconUrl": favicon_url, "logoUrl": favicon_url,
                 "iconUrl": favicon_url, "imageUrl": favicon_url, "companyName": nome},
                {"value": "no", "label": "Não, é outra unidade"},
            ],
            "required": True,
        }]
        return self._build_dynamic_form("company_confirm", fields, "Confirmar")


    def _build_cnpj_or_retry_form(self) -> Dict[str, Any]:
        """Form: Opcoes quando busca refinada nao encontrou a empresa."""
        fields = [
            {
                "name": "cnpj_or_retry_choice",
                "type": "select",
                "label": "Como deseja prosseguir?",
                "options": [
                    {"value": "provide_cnpj", "label": "Vou informar o CNPJ"},
                    {"value": "retry_search", "label": "Tentar com outro nome"},
                    {"value": "skip_cnpj", "label": "Prosseguir sem CNPJ"}
                ],
                "required": True
            }
        ]
        return self._build_dynamic_form("cnpj_or_retry", fields, "Continuar")


    def _set_company_names(self, state: ConversationState, trade_name: str = "", razao_social: str = "", company_name: str = ""):
        """Popula os campos de nome da empresa de forma consistente.

        Args:
            state: Estado da conversa
            trade_name: Nome fantasia (prioridade para display)
            razao_social: Razão social
            company_name: Nome genérico (fallback se trade_name e razao_social vazios)
        """
        trade = (trade_name or "").strip()
        razao = (razao_social or "").strip()
        fallback = (company_name or "").strip()

        state['opposing_party_trade_name'] = trade or None
        state['opposing_party_razao_social'] = razao or None
        # Display name: nome fantasia > razao social > fallback
        state['opposing_party_name'] = trade or razao or fallback


    def _set_pending_form(self, state: ConversationState, form_name: str, event_type: Dict[str, Any]) -> None:
        """Define um formulário como pendente e configura event_type para resposta."""
        state['pending_form'] = form_name
        state['pending_form_event_type'] = event_type
        # Keep a private copy after the outbound event is consumed by main.py.
        # This lets the backend validate legacy text-only form submissions.
        state['pending_form_snapshot'] = event_type
        self.log_decision("pending_form_set", {"form_name": form_name})


    def _emit_company_confirmation(self, state: ConversationState, company: Dict[str, Any], message: str) -> None:
        """
        Emite form de confirmacao de empresa e cacheia dados para quando o usuario confirmar.

        Args:
            company: Dict com dados da empresa (cnpj, nome_fantasia, razao_social, uf, municipio, etc)
            message: Mensagem a enviar junto com o form
        """
        state['pending_company_confirm'] = company
        state['company_confirmed'] = False

        confirm_form = self._build_company_confirm_form(company)
        self._set_pending_form(state, "company_confirm", confirm_form)

        state['messages'].append({
            'role': 'assistant',
            'content': message,
            'agent': self.name
        })

        self.log_decision("company_confirm_emitted", {
            "company_name": company.get("nome_fantasia") or company.get("razao_social"),
            "cnpj": company.get("cnpj")
        })


    def _build_company_confirm_message(self, company: Dict[str, Any]) -> str:
        """Mensagem humana para confirmar uma única unidade, sem mostrar CNPJ."""
        nome = company.get("nome_fantasia") or company.get("razao_social") or "a empresa"
        if nome.lower().startswith('empresa cnpj'):
            return 'Encontrei apenas um cadastro mínimo, sem dados suficientes para identificar a unidade. O cadastro informado corresponde à empresa do seu caso?'
        uf = company.get("uf", "")
        municipio = company.get("municipio", "")
        endereco = company.get("logradouro", "")
        numero = company.get("numero", "")
        bairro = company.get("bairro", "")
        location = []
        if bairro:
            location.append(bairro)
        if endereco:
            location.append(f"{endereco}{', ' + numero if numero else ''}")
        if municipio and uf:
            location.append(f"{municipio}/{uf}")
        elif municipio:
            location.append(municipio)
        where = " · ".join(location)
        if where:
            return f'Encontrei uma unidade da {nome} em {where}. É essa a loja onde aconteceu?'
        return f'Encontrei a {nome}. É essa a empresa onde aconteceu?'


    async def _create_company_from_cnpj(self, state: ConversationState) -> str:
        """Use the idempotent platform ladder for a known CNPJ, without paid search."""
        cnpj = normalize_cnpj(state.get('opposing_party_cnpj'))
        if not is_valid_cnpj(cnpj):
            return "invalid"
        state['opposing_party_cnpj'] = cnpj
        client = self.company_search_client
        if not client or not hasattr(client, 'create_company_from_cnpj'):
            return "error"
        try:
            result = await client.create_company_from_cnpj(cnpj)
        except Exception as exc:
            self.log_decision('create_company_error', {'error_type': type(exc).__name__})
            return "error"
        if not result.success:
            error = _remove_accents(result.error or '').lower()
            if 'invalido' in error or 'digito verificador' in error:
                return "invalid"
            return "error"
        self._set_company_names(state, trade_name=result.nome_fantasia, razao_social=result.razao_social)
        state['opposing_party_company_id'] = result.company_id
        state['opposing_party_situacao_cadastral'] = result.situacao_cadastral
        state['opposing_party_situacao_cadastral_source'] = result.situacao_cadastral_source
        state['opposing_party_found_in_db'] = True
        name = state.get('opposing_party_name', '')
        state['company_minimal_registration'] = not name or name.lower().startswith('empresa cnpj')
        if state['company_minimal_registration']:
            state['company_confirmed'] = False
        if 'opposing_party_cnpj' in self.required_fields:
            self.required_fields.remove('opposing_party_cnpj')
        if result.situacao_cadastral_confiavel and (result.situacao_cadastral or '').upper() != 'ATIVA':
            return "inactive"
        return "created" if result.created else "exists"


    def _parse_cnpj_type(self, cnpj: str) -> Dict[str, Any]:
        """
        Detecta se um CNPJ e de MATRIZ ou FILIAL.

        Formato CNPJ: XX.XXX.XXX/YYYY-ZZ
        - YYYY = 0001 significa MATRIZ
        - YYYY >= 0002 significa FILIAL

        Returns:
            {
                "is_filial": bool,
                "is_matriz": bool,
                "order": str (4 digitos),
                "radical": str (8 primeiros digitos)
            }
        """
        digits = normalize_cnpj(cnpj)
        if len(digits) != 14:
            return {"is_filial": False, "is_matriz": False, "order": "0000", "radical": ""}

        radical = digits[:8]
        order = digits[8:12]

        return {
            "is_filial": order != "0001",
            "is_matriz": order == "0001",
            "order": order,
            "radical": radical
        }


    async def _set_matriz_company(self, state: ConversationState, company: Dict[str, Any]) -> str:
        """
        Salva dados da empresa MATRIZ no state e cria no Zellu se necessario.

        Args:
            company: Dict da empresa (resultado da busca avancada)

        Returns:
            "matriz_found" | "error"
        """
        cnpj = company.get("cnpj", "")
        trade = company.get("nome_fantasia") or company.get("nomeFantasia") or company.get("tradeName") or ""
        razao = company.get("razao_social") or company.get("razaoSocial") or company.get("companyName") or ""
        display_name = trade or razao
        uf = company.get("uf", "")
        municipio = company.get("municipio", "")

        state['opposing_party_matriz_cnpj'] = cnpj
        state['opposing_party_matriz_name'] = display_name
        # Guardar dados completos da matriz para uso em forms de confirmacao
        state['_enriched_company_data'] = company

        # Criar/encontrar a matriz no Zellu
        if not self.company_search_client or not hasattr(self.company_search_client, 'create_company_from_cnpj'):
            # Sem client, preencher com dados da busca mesmo assim
            state['opposing_party_cnpj'] = cnpj
            self._set_company_names(state, trade_name=trade, razao_social=razao)
            state['opposing_party_found_in_db'] = True
            if "opposing_party_cnpj" in self.required_fields:
                self.required_fields.remove("opposing_party_cnpj")
            return "matriz_found"

        try:
            create_result = await self.company_search_client.create_company_from_cnpj(cnpj)

            if create_result.success:
                state['opposing_party_cnpj'] = cnpj
                state['opposing_party_name'] = display_name or state.get('opposing_party_name', '')
                state['opposing_party_company_id'] = create_result.company_id
                state['opposing_party_situacao_cadastral'] = create_result.situacao_cadastral
                state['opposing_party_situacao_cadastral_source'] = create_result.situacao_cadastral_source
                state['opposing_party_found_in_db'] = True

                if "opposing_party_cnpj" in self.required_fields:
                    self.required_fields.remove("opposing_party_cnpj")

                self.log_decision("matriz_company_set", {
                    "matriz_cnpj": cnpj,
                    "matriz_name": display_name,
                    "company_id": create_result.company_id,
                    "location": f"{municipio}/{uf}"
                })
                return "matriz_found"
            else:
                self.log_decision("matriz_company_create_failed", {
                    "cnpj": cnpj,
                    "error": create_result.error
                })
                return "error"

        except Exception as e:
            self.log_decision("matriz_set_error", {"error": str(e)})
            return "error"


    async def _search_matriz_for_filial(self, state: ConversationState) -> str:
        """
        Busca a empresa MATRIZ quando o CNPJ informado e de uma FILIAL.

        Consulta o CNPJ 0001 da mesma raiz no Mongo; em caso de ausência,
        usa a escada idempotente da plataforma para esse CNPJ exato.

        Returns:
            "matriz_found" | "matriz_not_found" | "error"
        """
        filial_cnpj = state.get('opposing_party_filial_cnpj') or state.get('opposing_party_cnpj', '')
        cnpj_info = self._parse_cnpj_type(filial_cnpj)
        radical = cnpj_info.get('radical', '')

        if not radical:
            return "error"

        # Matriz 0001 da mesma raiz: consulta exata indexada no Mongo primeiro.
        for first in range(10):
            for second in range(10):
                candidate = radical + '0001' + str(first) + str(second)
                if is_valid_cnpj(candidate):
                    cached = await asyncio.to_thread(find_company_by_cnpj, candidate)
                    if cached:
                        state['company_lookup_source'] = 'mongo'
                        return await self._set_matriz_company(state, cached)
                    break
            else:
                continue
            break

        client = self.company_search_client
        if not client or not hasattr(client, 'create_company_from_cnpj'):
            return "matriz_not_found"
        try:
            result = await client.create_company_from_cnpj(candidate)
            name = result.nome_fantasia or result.razao_social or ''
            returned = normalize_cnpj(result.cnpj or candidate)
            if (not result.success or not name or name.lower().startswith('empresa cnpj')
                    or returned != candidate):
                return "matriz_not_found"
            return await self._set_matriz_company(state, {
                'cnpj': returned, 'nome_fantasia': result.nome_fantasia,
                'razao_social': result.razao_social, 'uf': result.uf, 'municipio': result.municipio,
            })
        except Exception as exc:
            self.log_decision('matriz_lookup_error', {'error_type': type(exc).__name__})
            return "matriz_not_found"


    async def _resolve_favicons(self, companies, searched_name=''):
        """Preserva icones conhecidos e resolve os demais pelo dominio da empresa."""
        try:
            await resolve_company_icons(companies, searched_name)
        except Exception as exc:  # icone e enfeite: nunca pode derrubar o dropdown
            self.log_decision('company_icon_error', {'error_type': type(exc).__name__})


    async def _process_firecrawl_results(self, state: ConversationState, result, auto_confirm: bool = False,
                                         select_message: Optional[str] = None,
                                         confirm_prefix: str = '') -> ConversationState:
        """Processa resultados do Firecrawl e monta forms de selecao/confirmacao."""
        # Escada de busca: CNPJ recusado pelo cliente nunca volta ao dropdown.
        rejected = set(state.get('company_rejected_cnpjs') or [])
        empresas = [e for e in result.empresas if normalize_cnpj(e.get('cnpj')) not in rejected]

        self.log_decision("company_search_results", {
            "count": len(empresas),
            "source": getattr(result, 'source', 'unknown')
        })

        # Achatar o endereco para o formato esperado pelos forms
        normalized = []
        for emp in empresas:
            endereco = emp.get("endereco") or {}
            normalized.append({
                "cnpj": emp.get("cnpj", ""),
                "nome_fantasia": emp.get("nome_fantasia") or emp.get("razao_social", ""),
                "razao_social": emp.get("razao_social", ""),
                "uf": endereco.get("uf") or emp.get("uf", ""),
                "municipio": endereco.get("municipio") or emp.get("municipio", ""),
                "logradouro": endereco.get("logradouro") or emp.get("logradouro", ""),
                "numero": endereco.get("numero") or emp.get("numero", ""),
                "site": emp.get("site") or emp.get("homepage") or emp.get("url") or "",
                "contato_email": emp.get("contato_email") or emp.get("email"),
                "bairro": endereco.get("bairro") or emp.get("bairro", ""),
                "favicon_url": emp.get("favicon_url"),
                "source_url": emp.get("source_url"),
                "public_page_verified": emp.get("public_page_verified", False),
                "candidate_id": emp.get("candidate_id"),
            })

        # Normalizar (CNPJ so em digitos, nomes em Title Case), descartar CNPJ
        # invalido/ausente e deduplicar pelo CNPJ. Roda ANTES do corte em 5 -
        # senao as repeticoes da mesma empresa ocupariam as vagas da lista - e
        # antes da busca de favicon, que gasta 1 chamada Firecrawl por empresa.
        cleaned = normalize_company_list(normalized)
        normalized = cleaned.companies[:5]

        if cleaned.invalid_cnpjs or cleaned.duplicates_removed:
            self.log_decision("firecrawl_results_deduped", {
                "received": len(empresas),
                "kept": len(normalized),
                "duplicates_removed": cleaned.duplicates_removed,
                "invalid_cnpjs": cleaned.invalid_cnpjs,
            })

        await self._resolve_favicons(normalized, state.get('opposing_party_name') or '')
        for company in normalized:
            # E-mail do cadastro serve so para achar o dominio; nao fica na sessao.
            company.pop('contato_email', None)
            label = self._format_company_option_label(
                company.get('nome_fantasia') or company.get('razao_social') or 'Empresa', company
            )
            company['candidate_id'] = company.get('candidate_id') or (
                'cmp_' + hashlib.sha256((company.get('cnpj', '') + '|' + label).encode('utf-8')).hexdigest()[:16]
            )

        if len(normalized) == 1 and auto_confirm:
            # Compra online e uma unica sede: registrar direto, sem perguntar.
            from src.open_dots.intake import confirm_company
            await confirm_company(state, normalized[0], self)
            state['company_auto_selected'] = True
            self.log_decision("company_auto_selected_online", {"cnpj": normalized[0].get("cnpj")})
            return state

        if len(normalized) == 1:
            # 1 resultado - confirmar com usuario
            company_data = normalized[0]
            msg = self._build_company_confirm_message(company_data)
            if confirm_prefix:
                msg = confirm_prefix + (msg[:1].lower() + msg[1:] if confirm_prefix.endswith(', ') else msg)
            self._emit_company_confirmation(state, company_data, msg)
            state['step'] += 1
            return state

        elif len(normalized) >= 2:
            # Multiplos resultados - selecionar
            state['company_search_results'] = normalized
            select_form = self._build_company_select_form(normalized)
            self._set_pending_form(state, "company_select", select_form)
            state['messages'].append({
                'role': 'assistant',
                'content': select_message or "Encontrei algumas empresas com essas informacoes. Selecione a empresa correta.",
                'agent': self.name
            })
            state['step'] += 1
            return state

        return None


    def _firecrawl_fallback_no_results(self, state: ConversationState, error_info: str = "no_matches") -> ConversationState:
        """Fallback quando a busca nao encontra resultados."""
        self.log_decision("firecrawl_search_no_results", {"error": error_info})
        state.setdefault('messages', [])
        name = state.get('opposing_party_name') or 'essa empresa'

        from src.services.company_search_pipeline import resolve_locality
        city = resolve_locality(state.get('company_location_input') or '', exclude=name).city
        if city.islower() or city.isupper():
            city = normalize_company_name(city)
        asked = int(state.get('company_city_asked') or 0)
        if error_info in {'city_needed', 'city_unclear'} and asked < 2:
            # Ha unidades em varias cidades e o relato nao aponta uma - ou o local
            # pode ser cidade ou bairro ("Lapa"): perguntar em vez de oferecer lojas
            # de outros estados. Depois de duas perguntas, segue o caminho do CNPJ.
            state['company_city_asked'] = asked + 1
            state['company_city_needed'] = True
            self._set_pending_form(state, "company_refine", self._build_company_refine_form())
            if error_info == 'city_unclear' and city:
                content = (f"Nao encontrei unidade de {name} na cidade de {city}. Se {city} for um bairro "
                           "ou shopping, me diga em qual cidade e estado fica a unidade.")
            else:
                content = (f"Encontrei unidades de {name} em cidades diferentes. "
                           "Em qual cidade e estado fica a unidade do seu caso?")
        else:
            if "opposing_party_cnpj" not in self.required_fields:
                self.required_fields.append("opposing_party_cnpj")
            self._set_pending_form(state, "cnpj_or_retry", self._build_cnpj_or_retry_form())
            if error_info in {'no_matches_in_requested_city', 'city_unclear'} and city:
                content = f"Nao encontrei unidade de {name} em {city} na nossa base nem nas outras fontes que consultei."
            else:
                content = "Nao encontrei essa empresa na nossa base nem nas outras fontes que consultei."
        state['messages'].append({'role': 'assistant', 'content': content, 'agent': self.name})
        state['step'] = state.get('step', 0) + 1
        return state


    async def _search_via_firecrawl(self, state):
        """Nome legado preservado para sessoes antigas; busca usa Scrapling."""
        from src.services.company_search_pipeline import (
            search_external_companies, resolve_locality, localize_companies,
        )
        from src.services.intake_progress import (
            MSG_BEFORE_COMPANY_SEARCH, MSG_CACHE_MISS, send_progress,
        )
        await send_progress(state, MSG_BEFORE_COMPANY_SEARCH)
        name = state.get('opposing_party_name', '')
        location = state.get('company_location_input', '')
        segment = state.get('company_segment', '')
        # Tentativa 1 da escada (relato do cliente). Recusados continuam excluidos.
        state['company_search_attempt'] = 1
        state['company_awaiting_reference'] = False
        state['company_search_queries'] = ((state.get('company_search_queries') or [])
                                           + ['attempt1|' + '|'.join(_remove_accents(str(v)).lower().strip()
                                                                     for v in (name, location, segment))])[-40:]
        # A cidade sai sempre do local atual: reaproveitar a cidade de uma busca
        # anterior prenderia o cliente nela depois de corrigir o endereco.
        locality = resolve_locality(location, exclude=name)
        if locality.city:
            state['problem_location_city'] = locality.city
        if locality.search_uf:
            state['problem_location_uf'] = locality.search_uf
        # Compra por site/app: "site" nao e endereco; procurar na base so pelo nome.
        base_location = '' if locality.online and not locality.city else location
        cached = await asyncio.to_thread(find_company_lookup_results, name, base_location, segment, 10)
        cached, reason = localize_companies(cached, locality, location)
        result = CompanySearchResult(empresas=cached, source='mongo', error=reason)
        if not cached:
            await send_progress(state, MSG_CACHE_MISS)
            result = await search_external_companies(name, location, segment)
        state['company_lookup_source'] = result.source
        if result.source != 'mongo' and result.empresas:
            saved = await asyncio.to_thread(
                save_company_lookup_results, result.empresas,
                searched_name=name,
                searched_location=location,
                searched_segment=segment,
                source=result.source)
            state['company_lookup_saved_to_mongo'] = bool(saved)
        if result.empresas:
            state.pop('company_city_needed', None)
            state['company_city_asked'] = 0
            online = locality.online and not locality.city
            processed = await self._process_firecrawl_results(state, result, auto_confirm=online) if online \
                else await self._process_firecrawl_results(state, result)
            if processed is not None:
                return processed
        error = result.error or 'no_matches'
        city_question = error in {'city_needed', 'city_unclear'} and int(state.get('company_city_asked') or 0) < 2
        if not city_question and self._deep_search_on_empty():
            # Nada para o cliente escolher: a escada sobe direto para a busca profunda.
            from src.services.company_search_ladder import escalate_company_search
            return await escalate_company_search(state, self, rejected=False)
        return self._firecrawl_fallback_no_results(state, error)


    def _deep_search_on_empty(self) -> bool:
        try:
            from config import get_settings
            return bool(getattr(get_settings(), 'COMPANY_DEEP_SEARCH_ON_EMPTY', True))
        except Exception:
            return True


    async def _deep_search_company(self, state, reference=None, select_message=None, confirm_prefix='') -> bool:
        """Tentativas 2/3 da escada. True quando ha candidatos na tela para o cliente."""
        from src.services.company_search_pipeline import (
            deep_search_companies, resolve_locality, localize_companies,
        )
        reference = reference or {}
        name = state.get('opposing_party_name', '')
        location = state.get('company_location_input', '')
        segment = state.get('company_segment', '')
        rejected = list(state.get('company_rejected_cnpjs') or [])
        place = ', '.join(filter(None, [location, reference.get('place', '')]))
        try:
            # Mongo primeiro com o nome que a referencia trouxe: barato e sem rede externa.
            result = None
            for variant in filter(None, [reference.get('printed_name'), reference.get('instagram'),
                                         reference.get('site_name')]):
                cached = await asyncio.to_thread(find_company_lookup_results, variant, '', segment, 10)
                cached = [c for c in cached or [] if normalize_cnpj(c.get('cnpj')) not in rejected]
                cached, _ = localize_companies(cached, resolve_locality(place, exclude=name), place)
                if cached:
                    result = CompanySearchResult(empresas=cached, source='mongo')
                    break
            if result is None:
                result, done = await deep_search_companies(
                    name, location, segment, narrative=state.get('problem_description') or '',
                    reference=reference, exclude_cnpjs=rejected,
                    skip_queries=state.get('company_search_queries') or [])
                state['company_search_queries'] = ((state.get('company_search_queries') or []) + done)[-40:]
        except Exception as exc:
            self.log_decision('company_deep_search_error', {'error_type': type(exc).__name__})
            return False
        state['company_lookup_source'] = result.source
        self.log_decision('company_deep_search', {'count': len(result.empresas), 'source': result.source,
                                                  'attempt': state.get('company_search_attempt')})
        if result.source != 'mongo' and result.empresas:
            saved = await asyncio.to_thread(
                save_company_lookup_results, result.empresas,
                searched_name=name,
                searched_location=location,
                searched_segment=segment,
                source=result.source)
            state['company_lookup_saved_to_mongo'] = bool(saved)
        if not result.empresas:
            return False
        processed = await self._process_firecrawl_results(state, result, select_message=select_message,
                                                          confirm_prefix=confirm_prefix)
        return processed is not None


    async def _persist_confirmed_company(self, state):
        selected = state.get('_company_to_persist')
        if not selected:
            return
        saved = await asyncio.to_thread(
            save_company_lookup_results, [selected],
            searched_name=state.get('opposing_party_name', ''),
            searched_location=state.get('company_location_input', ''),
            searched_segment=state.get('company_segment', ''),
            source=state.get('company_lookup_source', 'confirmed_selection'))
        state['company_selection_saved_to_mongo'] = bool(saved)
        if saved:
            state.pop('_company_to_persist', None)
        self.log_decision('company_selection_persistence', {'saved': bool(saved)})


    def _latest_user_message_text(self, state: ConversationState) -> str:
        for msg in reversed(state.get('messages') or []):
            if msg.get('role') == 'user':
                return str(msg.get('content') or '').strip()
        return ''


    def _remember_intake_input(self, state, reference=None):
        messages = state.get('messages') or []
        if not messages or messages[-1].get('role') != 'user':
            return
        text = str(messages[-1].get('content') or '')
        key = f"{len(messages)}:{hashlib.sha256(text.encode()).hexdigest()}"
        if state.get('intake_last_input_key') == key:
            return
        reference = reference or datetime.now(TZ)
        state['intake_last_input_key'] = key
        state['intake_reference_timestamp'] = reference.isoformat()
        mentions = resolve_date_mentions(text, reference)
        state.setdefault('case_date_mentions', []).extend(
            {**mention, 'message_index': len(messages) - 1} for mention in mentions)
        if not state.get('problem_description') and self._looks_like_problem_report(text):
            state['problem_description_original'] = text
            state['problem_description'] = text


    def _looks_like_problem_report(self, text: str) -> bool:
        text = (text or '').strip()
        if len(text) < 20:
            return False

        normalized = _remove_accents(text.lower())
        words = re.findall(r'\w+', normalized)
        if len(words) < 4:
            return False

        short_non_reports = {
            'shopping center', 'shopping centre', 'loja fisica', 'loja online',
            'nao sei', 'não sei', 'sim', 'nao', 'não',
        }
        if normalized in short_non_reports:
            return False

        report_cues = (
            'aconteceu', 'problema', 'tive', 'fui', 'cai', 'cair', 'queda',
            'acidente', 'machuquei', 'lesao', 'feri', 'ferimento', 'ambulancia',
            'sem ajuda', 'nao tive', 'nao recebi', 'me cobraram', 'cobranca',
            'cobrado', 'paguei', 'gastei', 'comprei', 'contratei', 'defeito',
            'quebrou', 'atrasou', 'cancelou', 'recusou', 'negou', 'demitido',
            'assedio', 'ameaca', 'agressao', 'testemunha', 'foto', 'prova',
        )
        if any(cue in normalized for cue in report_cues):
            return True

        return len(words) >= 10


    async def _merge_descriptions(self, state):
        """Mantem relato e complemento integrais, sem resumo destrutivo pela LLM."""
        self._remember_intake_input(state)
        new_info = self._latest_user_message_text(state)
        previous = state.get('problem_description') or (
            (state.get('previous_descriptions') or [''])[-1])
        state.setdefault('problem_description_original', previous or new_info)
        if new_info and new_info not in previous:
            state['problem_description'] = previous + "\n\nComplemento do cliente:\n" + new_info
        else:
            state['problem_description'] = previous or new_info


