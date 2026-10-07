"""Consulta contratada: credenciais apenas no ambiente, nunca no repositorio."""
import os
import re
from urllib.parse import urlsplit
import httpx
from src.services.scrapling_company_search import CompanySearchResult
from src.services.company_lookup_cache import normalize_lookup_text
from config import get_settings


async def search_companies(name, city='', uf='', location='', matriz_only=False, fuzzy=False, limit=5, timeout=15):
    """fuzzy/limit/timeout ampliam a busca profunda (2a tentativa); padrao = 1a tentativa."""
    if not name or not re.search(r'[\w]', name):
        return CompanySearchResult(source='casadosdados', success=False, error='empty_company_name')
    url = os.getenv('API_CNPJ_CONNECTION_URL', '').rstrip('/')
    key = os.getenv('API_CNPJ_CONNECTION_KEY', '')
    if not url or not key:
        settings = get_settings()
        url = url or settings.API_CNPJ_CONNECTION_URL.rstrip('/')
        key = key or settings.API_CNPJ_CONNECTION_KEY
    if not url or not key:
        return CompanySearchResult(source='casadosdados', success=False, error='not_configured')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'api.casadosdados.com.br' or parsed.username or parsed.query:
        return CompanySearchResult(source='casadosdados', success=False, error='invalid_provider_url')
    payload = {
        'busca_textual': [{'texto': [name], 'tipo_busca': 'radical' if fuzzy else 'exata',
                           'razao_social': True, 'nome_fantasia': True}],
        'limite': 25, 'pagina': 1, 'situacao_cadastral': ['ATIVA'],
    }
    if city:
        # Formato documentado pelo provedor: minusculo e sem acento ("sao paulo").
        payload['municipio'] = [normalize_lookup_text(city)]
    if uf:
        payload['uf'] = [uf]
    if matriz_only:
        payload['matriz_filial'] = 'MATRIZ'  # compra por site/app: quem responde e a sede
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(url + '/v5/cnpj/pesquisa',
                                         params={'tipo_resultado': 'completo'},
                                         headers={'api-key': key}, json=payload)
        if response.status_code != 200:
            return CompanySearchResult(source='casadosdados', success=False,
                                       error=f'http_{response.status_code}')
        data = response.json()
        companies = data.get('cnpjs', data.get('empresas', []))
        if not isinstance(companies, list):
            companies = []
        tokens = re.findall(r'[a-z0-9]+', normalize_lookup_text(name))
        # Busca radical: basta um termo relevante do nome (razao social pode ser outra).
        relevant = ([t for t in tokens if len(t) > 2] or tokens) if fuzzy else tokens
        matches = any if fuzzy else all
        companies = [company for company in companies if isinstance(company, dict) and
                     matches(token in normalize_lookup_text(' '.join([
                         company.get('razao_social') or '', company.get('nome_fantasia') or '']))
                         for token in relevant)]
        def relevance(company):
            names = [normalize_lookup_text(company.get(key) or '')
                     for key in ('nome_fantasia', 'razao_social')]
            # Prioriza o nome do estabelecimento, nao lojas que apenas o citam.
            prefix = any(value.split()[:1] == tokens[:1] for value in names)
            address = normalize_lookup_text(' '.join(str(v) for v in (company.get('endereco') or {}).values()
                                                     if isinstance(v, str)))
            local_tokens = [t for t in normalize_lookup_text(location).split()
                            if t not in {'shopping','no','na','em','bairro','cidade','de','do','da'}
                            and t not in normalize_lookup_text(city).split()]
            local_score = sum(t in address for t in local_tokens)
            scores = [len(tokens) / max(len(value.split()), 1)
                      for value in names if all(token in value for token in tokens)]
            return local_score, prefix, max(scores, default=0)
        companies.sort(key=relevance, reverse=True)
        companies = companies[:limit]
        return CompanySearchResult(empresas=companies, source='casadosdados')
    except Exception as exc:
        return CompanySearchResult(source='casadosdados', success=False, error=type(exc).__name__)
