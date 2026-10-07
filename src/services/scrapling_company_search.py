"""Busca publica com Scrapling; nao usa Firecrawl nem LLM para inventar CNPJ."""
from dataclasses import dataclass, field
import base64
import re
import time
from functools import lru_cache
from urllib.parse import urlencode, urlsplit, parse_qs, unquote
from src.utils.company_normalizer import normalize_company_list
from src.services.company_lookup_cache import normalize_lookup_text

@dataclass
class CompanySearchResult:
    empresas: list = field(default_factory=list)
    success: bool = True
    error: str = ''
    credits_used: int = 0
    source: str = 'scrapling'

# Palavras que nao identificam a empresa: sozinhas nao bastam para aceitar um cadastro.
_GENERIC_NAME_TOKENS = {'loja', 'lojas', 'comercio', 'servicos', 'ltda', 'eireli', 'restaurante',
                        'mercado', 'supermercado', 'farmacia', 'drogaria', 'magazine', 'store', 'shop',
                        'brasil', 'empresa', 'cia', 'the'}
# Resultados que nao sao o site da empresa (buscadores, redes que exigem login, marketplaces).
_NOT_COMPANY_SITES = ('google.com', 'google.com.br', 'bing.com', 'microsoft.com', 'youtube.com',
                      'facebook.com', 'instagram.com', 'tiktok.com', 'twitter.com', 'x.com', 'linkedin.com',
                      'wikipedia.org', 'reclameaqui.com.br', 'mercadolivre.com.br', 'gov.br', 'jusbrasil.com.br',
                      'cnpj.biz', 'casadosdados.com.br', 'cnpja.com', 'econodata.com.br', 'cnpj.info',
                      'consultacnpj.com', 'empresascnpj.com')


def _unwrap_result_link(link):
    """Destino real do link do buscador: Google /url?q=..., Bing /ck/a?...&u=a1<base64>."""
    if link.startswith('/url?'):
        return parse_qs(urlsplit(link).query).get('q', [''])[0]
    parsed = urlsplit(link)
    if (parsed.hostname or '').endswith('bing.com') and parsed.path.startswith('/ck/a'):
        encoded = parse_qs(parsed.query).get('u', [''])[0]
        if encoded.startswith('a1'):
            try:
                raw = encoded[2:]
                return base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8', 'ignore')
            except Exception:
                return ''
    return link


class ScraplingCompanySearch:
    allowed_hosts = {'cnpj.biz', 'www.cnpj.biz', 'casadosdados.com.br', 'www.casadosdados.com.br'}
    # Busca profunda (2a tentativa): mais diretorios publicos de CNPJ.
    deep_directory_hosts = allowed_hosts | {'cnpja.com', 'www.cnpja.com', 'econodata.com.br',
                                            'www.econodata.com.br', 'cnpj.info', 'www.cnpj.info',
                                            'consultacnpj.com', 'www.consultacnpj.com',
                                            'empresascnpj.com', 'www.empresascnpj.com'}

    def __init__(self):
        self._company_cache = {}
        self._site_cache = {}
        self._blocked_until = 0

    def _get(self, url, retries=3):
        from scrapling.fetchers import Fetcher
        return Fetcher.get(url, timeout=5, follow_redirects=False, retries=retries)

    def read_company(self, cnpj):
        """Le uma pagina individual descoberta por uma fonte confiavel."""
        cleaned = normalize_company_list([{'cnpj': cnpj}]).companies
        if not cleaned:
            return None
        canonical = cleaned[0]['cnpj']
        cached = self._company_cache.get(canonical)
        if cached and cached[0] > time.monotonic():
            import copy
            return copy.deepcopy(cached[1])
        if self._blocked_until > time.monotonic():
            return None
        url = 'https://cnpj.biz/' + canonical
        try:
            page = self._get(url)
            if page.status in {403, 429}:
                self._blocked_until = time.monotonic() + 60
                return None
            if page.status != 200:
                return None
            text = page.get_all_text(separator=' ', strip=True)
            labels = ('CNPJ|Razão Social|Nome Fantasia|Data da Abertura|Porte|Natureza Jurídica|'
                      'Opção pelo MEI|Opção pelo Simples|Tipo|Situação|Situação Cadastral|'
                      'Logradouro|Bairro|CEP|Município|Cidade|Estado|UF|Para correspondência')
            def field(label):
                match = re.search(r'(?:' + label + r')\s*:\s*(.*?)(?=\s+(?:' + labels + r')\s*:|$)', text, re.I)
                return match.group(1).strip() if match else ''
            found = re.search(r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\b\d{14}\b', field('CNPJ'))
            if not found or re.sub(r'\D', '', found.group()) != canonical:
                return None
            razao = field('Razão Social')
            fantasia = field('Nome Fantasia')
            if not razao:
                return None
            street = field('Logradouro')
            street, separator, number = street.rpartition(', ') if ', ' in street else (street, '', '')
            states = dict(zip(
                ['acre','alagoas','amapa','amazonas','bahia','ceara','distrito federal','espirito santo','goias','maranhao','mato grosso','mato grosso do sul','minas gerais','para','paraiba','parana','pernambuco','piaui','rio de janeiro','rio grande do norte','rio grande do sul','rondonia','roraima','santa catarina','sao paulo','sergipe','tocantins'],
                ['AC','AL','AP','AM','BA','CE','DF','ES','GO','MA','MT','MS','MG','PA','PB','PR','PE','PI','RJ','RN','RS','RO','RR','SC','SP','SE','TO']))
            uf = field('UF') or field('Estado')
            company = {'cnpj': canonical, 'razao_social': razao, 'nome_fantasia': fantasia or razao,
                    'endereco': {'logradouro': street, 'numero': number, 'bairro': field('Bairro'),
                                 'municipio': field('Município|Cidade'), 'cep': field('CEP'),
                                 'uf': states.get(normalize_lookup_text(uf), uf)},
                    'source_url': url, 'public_page_verified': True}
            self._company_cache[canonical] = (time.monotonic() + 3600, company)
            return company
        except Exception:
            return None

    def site_matches_brand(self, domain, brand_tokens):
        """Abre a pagina inicial e confere se a marca aparece nela.

        Devolve 'ok', 'mismatch' (site de outra empresa ou dominio estacionado),
        'blocked' (site ativo que recusa leitura automatica) ou 'unreachable'.
        So segue redirecionamento dentro do mesmo dominio (dominio -> www.dominio).
        """
        cached = self._site_cache.get(domain)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        verdict = self._read_site_verdict(domain, brand_tokens)
        self._site_cache[domain] = (time.monotonic() + (86400 if verdict in {'ok', 'mismatch'} else 600), verdict)
        return verdict

    def _read_site_verdict(self, domain, brand_tokens):
        url = 'https://' + domain + '/'
        try:
            for _ in range(3):
                page = self._get(url, retries=1)  # uma tentativa: o dropdown nao espera por site lento
                if page.status in {301, 302, 303, 307, 308}:
                    headers = {str(k).lower(): v for k, v in (getattr(page, 'headers', None) or {}).items()}
                    target = urlsplit(str(headers.get('location') or ''))
                    host = target.hostname or ''
                    if target.scheme != 'https' or not (host == domain or host.endswith('.' + domain)):
                        return 'mismatch'  # leva a outro site: nao e a casa da marca
                    url = 'https://' + host + (target.path or '/')
                    continue
                if page.status in {401, 403, 429, 503}:
                    return 'blocked'
                if page.status != 200:
                    return 'unreachable'
                titles = ' '.join(page.css('title::text').getall())
                text = normalize_lookup_text(titles + ' ' + page.get_all_text(separator=' ', strip=True)[:6000])
                compact = re.sub(r'[^a-z0-9]', '', text)
                tokens = [t for t in brand_tokens if len(t) >= 3]
                found = bool(tokens) and (all(t in text for t in tokens) or ''.join(tokens) in compact)
                return 'ok' if found else 'mismatch'
            return 'unreachable'
        except Exception:
            return 'unreachable'

    def find_company_favicon(self, name, website=''):
        """Mantem o contrato faviconUrl sem consumir Firecrawl."""
        if website:
            parsed = urlsplit(website if '://' in website else 'https://' + website)
            if parsed.scheme in {'http', 'https'} and parsed.hostname:
                return 'https://' + parsed.netloc + '/favicon.ico'
        try:
            page = self._get('https://www.google.com/search?' + urlencode({'q': name + ' site oficial', 'num': 5}))
            for anchor in page.css('a'):
                link = anchor.attrib.get('href', '')
                if link.startswith('/url?'):
                    link = parse_qs(urlsplit(link).query).get('q', [''])[0]
                parsed = urlsplit(link)
                if parsed.scheme == 'https' and parsed.hostname and not parsed.username:
                    if parsed.hostname not in self.allowed_hosts and not any(
                        parsed.hostname == host or parsed.hostname.endswith('.' + host)
                        for host in ('google.com', 'youtube.com', 'facebook.com', 'instagram.com')):
                        return 'https://' + parsed.netloc + '/favicon.ico'
        except Exception:
            pass
        return None

    def search_cnpj(self, company_name, location='', segment=''):
        query = ' '.join(filter(None, [company_name, location, segment, 'CNPJ', '(site:cnpj.biz OR site:casadosdados.com.br)']))
        try:
            page = self._get('https://www.google.com/search?' + urlencode({'q': query, 'num': 10}))
            if page.status != 200:
                return CompanySearchResult(success=False, error=f'search_http_{page.status}')
            urls = []
            for anchor in page.css('a'):
                link = anchor.attrib.get('href', '')
                if link.startswith('/url?'):
                    link = parse_qs(urlsplit(link).query).get('q', [''])[0]
                parsed = urlsplit(link)
                if parsed.scheme == 'https' and parsed.hostname in self.allowed_hosts and not parsed.username and link not in urls:
                    urls.append(link)
            if not urls:
                page = self._get('https://www.bing.com/search?' + urlencode({'q': query}))
                for anchor in page.css('a'):
                    link = anchor.attrib.get('href', '')
                    parsed = urlsplit(link)
                    if parsed.scheme == 'https' and parsed.hostname in self.allowed_hosts and not parsed.username and link not in urls:
                        urls.append(link)
            companies = []
            for url in urls[:5]:
                try:
                    document = self._get(url)
                    if document.status != 200:
                        continue
                    company = self._company_from_directory_page(document, url, company_name, location)
                    if company:
                        companies.append(company)
                except Exception:
                    continue
            return CompanySearchResult(empresas=companies)
        except Exception as exc:
            return CompanySearchResult(success=False, error=type(exc).__name__)

    def _company_from_directory_page(self, document, url, company_name, location='', strict=True):
        """Extrai somente do documento consultado, nunca do nome informado.

        strict=True e a regra da primeira busca: todos os termos do nome no titulo e
        todos os termos do local no texto. A busca profunda (strict=False) aceita um
        termo relevante do nome e deixa o filtro de cidade para localize_companies.
        """
        text = document.get_all_text(separator=' ', strip=True)
        headings = document.css('h1::text').getall()
        name = ' '.join(headings).strip()
        cnpjs = re.findall(r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b', text)
        if not cnpjs or not name:
            return None
        name = re.split(r'\s+-\s+CNPJ', name, flags=re.I)[0]
        requested = [token for token in re.findall(r'[a-z0-9]+', normalize_lookup_text(company_name)) if len(token) > 2]
        found_name = normalize_lookup_text(name)
        if strict and not all(token in found_name for token in requested):
            return None
        if not strict and requested and not any(token in found_name for token in requested
                                                if token not in _GENERIC_NAME_TOKENS):
            return None
        # Local e segmento refinam a descoberta; endereco so e extraido
        # de rotulos presentes no documento, nao copiado da consulta.
        address = {}
        for key, label in [('municipio', 'Munic[ií]pio|Cidade'), ('bairro', 'Bairro'), ('uf', 'UF|Estado'), ('logradouro', 'Logradouro')]:
            match = re.search(r'(?:' + label + r')\s*:\s*([^:]{2,100}?)(?=\s+(?:Bairro|CEP|UF|Estado|Munic[ií]pio|Cidade|Número|Complemento)\s*:|$)', text, re.I)
            if match:
                address[key] = match.group(1).strip()
        if strict:
            normalized_text = normalize_lookup_text(text)
            location_tokens = [t for t in re.findall(r'[a-z0-9]+', normalize_lookup_text(location)) if len(t) > 2 and t not in {'shopping', 'bairro'}]
            if location_tokens and not all(t in normalized_text for t in location_tokens):
                return None
        cleaned = normalize_company_list([{'cnpj': cnpjs[0], 'razao_social': name, 'nome_fantasia': name}]).companies
        return {**cleaned[0], 'endereco': address, 'source_url': url} if cleaned else None

    def _result_links(self, query):
        """Links https dos resultados (Google e Bing), sem credencial na URL."""
        links = []
        for engine in ('https://www.google.com/search?' + urlencode({'q': query, 'num': 10}),
                       'https://www.bing.com/search?' + urlencode({'q': query, 'count': 20, 'cc': 'BR',
                                                                   'setlang': 'pt-BR', 'mkt': 'pt-BR'})):
            try:
                page = self._get(engine, retries=1)
                if page.status != 200:
                    continue
                for anchor in page.css('a'):
                    link = _unwrap_result_link(anchor.attrib.get('href', ''))
                    parsed = urlsplit(link)
                    if (parsed.scheme == 'https' and parsed.hostname and not parsed.username and link not in links
                            and not parsed.hostname.endswith(('bing.com', 'google.com', 'microsoft.com'))):
                        links.append(link)
            except Exception:
                continue
        return links

    def deep_search_cnpj(self, company_name, queries, budget_s=25.0, max_pages=10, exclude_cnpjs=(), sites=()):
        """Segunda tentativa: varias consultas, mais diretorios e o rodape do site oficial.

        Nunca inventa CNPJ: ou ele vem de um diretorio de CNPJ, ou aparece impresso
        no site da empresa e e conferido na pagina publica do cadastro (read_company).
        """
        deadline = time.monotonic() + budget_s
        excluded = {re.sub(r'\D', '', str(c)) for c in exclude_cnpjs or ()}
        brand = [token for token in re.findall(r'[a-z0-9]+', normalize_lookup_text(company_name))
                 if len(token) > 2 and token not in _GENERIC_NAME_TOKENS]
        directory_urls, site_urls, companies, seen = [], list(sites or ()), [], set(excluded)
        if self._blocked_until > time.monotonic():
            return CompanySearchResult(success=False, error='search_blocked')
        try:
            for query in queries:
                if time.monotonic() > deadline:
                    break
                for link in self._result_links(query):
                    host = urlsplit(link).hostname or ''
                    if host in self.deep_directory_hosts:
                        if link not in directory_urls:
                            directory_urls.append(link)
                    elif (any(token in host.replace('-', '') for token in brand)
                          and not any(host == blocked or host.endswith('.' + blocked) for blocked in _NOT_COMPANY_SITES)):
                        # Site com a marca no dominio: candidato a site oficial (CNPJ no rodape).
                        root = 'https://' + host + '/'
                        if root not in site_urls:
                            site_urls.append(root)
            for url in directory_urls[:max_pages]:
                if time.monotonic() > deadline:
                    break
                try:
                    document = self._get(url, retries=1)
                    if document.status != 200:
                        continue
                    company = self._company_from_directory_page(document, url, company_name, strict=False)
                    if company and company['cnpj'] not in seen:
                        seen.add(company['cnpj'])
                        companies.append(company)
                except Exception:
                    continue
            # Site oficial / pagina da loja: o CNPJ costuma estar no rodape.
            for url in site_urls[:3]:
                if time.monotonic() > deadline or len(companies) >= 8:
                    break
                try:
                    document = self._get(url, retries=1)
                    if document.status != 200:
                        continue
                    text = document.get_all_text(separator=' ', strip=True)
                    title = re.split(r'\s+[|\-–:]\s+', ' '.join(document.css('title::text').getall()).strip())[0]
                    for raw in re.findall(r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b', text)[:3]:
                        cnpj = re.sub(r'\D', '', raw)
                        if cnpj in seen or time.monotonic() > deadline:
                            continue
                        seen.add(cnpj)
                        company = self.read_company(cnpj)
                        if company:
                            companies.append({**company, 'site': url})
                            continue
                        # Diretorio bloqueado: o CNPJ impresso no proprio site da marca
                        # entra sem endereco (o cliente confirma); nunca vira "verificado".
                        cleaned = normalize_company_list([{'cnpj': cnpj, 'nome_fantasia': title or company_name}]).companies
                        if cleaned:
                            companies.append({**cleaned[0], 'endereco': {}, 'site': url, 'source_url': url,
                                              'public_page_verified': False})
                except Exception:
                    continue
            return CompanySearchResult(empresas=companies, source='scrapling_deep')
        except Exception as exc:
            return CompanySearchResult(success=False, error=type(exc).__name__, source='scrapling_deep')

@lru_cache(maxsize=1)
def get_scrapling_company_search():
    return ScraplingCompanySearch()
