"""Icone da empresa para o dropdown de selecao.

A Casa dos Dados nao entrega logo. O icone e da marca, entao vale para a matriz e
todas as filiais (mesma raiz de CNPJ). Ordem:

1. `favicon_url` ja gravado no cadastro;
2. icone gravado para qualquer unidade da mesma raiz de CNPJ (Mongo) - a primeira
   unidade confirmada de uma marca resolve o icone das demais;
3. site do cadastro;
4. dominio do e-mail do cadastro, quando o dominio carrega o nome da marca
   (descarta webmail e e-mail do contador);
5. dominio deduzido do nome (riachuelo.com.br): precisa resolver no DNS para um
   endereco publico e o Scrapling confere se a marca aparece na pagina inicial.

A URL final segue o contrato de docs/20260505-payload-empresa-tradename-favicon.md
(Google Favicon API, 64px): e estavel para ficar gravada, ao contrario do arquivo
de icone que o proprio site publica, cujo endereco muda a cada deploy.
Sem evidencia de dominio o icone fica None e o front usa as iniciais.
"""
import asyncio
import ipaddress
import os
import re
import socket
import unicodedata
from urllib.parse import urlparse

FAVICON_URL = 'https://www.google.com/s2/favicons?domain={domain}&sz=64'
_FREE_MAIL = {
    'gmail', 'hotmail', 'outlook', 'yahoo', 'live', 'msn', 'icloud', 'uol', 'bol', 'terra',
    'ig', 'globo', 'globomail', 'r7', 'zipmail', 'oi', 'aol', 'protonmail', 'proton', 'me',
}
_CORPORATE = {'ltda', 'sa', 's', 'a', 'me', 'epp', 'eireli', 'mei', 'cia', 'ss', 'spe', 'filial', 'matriz'}
_STOPWORDS = {'de', 'do', 'da', 'dos', 'das', 'e', 'em', 'no', 'na'}
_GENERIC = {'loja', 'lojas', 'shopping', 'center', 'comercio', 'servicos', 'grupo', 'industria',
            'empresa', 'condominio', 'civil'}
_SUFFIXES = ('com.br', 'net.br', 'org.br', 'ind.br', 'art.br', 'eco.br', 'app.br', 'com', 'net', 'org', 'br', 'io', 'co')
_NOT_COMPANY_SITES = {
    'cnpj.biz', 'casadosdados.com.br', 'google.com', 'instagram.com', 'facebook.com', 'fb.com',
    'linktr.ee', 'wa.me', 'whatsapp.com', 'linkedin.com', 'youtube.com', 'twitter.com', 'x.com',
    'tiktok.com', 'ifood.com.br', 'mercadolivre.com.br', 'shopee.com.br',
}
_ACCOUNTING = ('contab', 'contador', 'assessoria', 'escritorio', 'consult', 'advoc', 'fiscal')
_dns_cache = {}


def _tokens(text):
    text = unicodedata.normalize('NFKD', str(text or ''))
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    return [t for t in re.findall(r'[a-z0-9]+', text) if t not in _CORPORATE]


def _hostname(value):
    value = str(value or '').strip().lower()
    if not value:
        return ''
    try:
        host = urlparse(value if '://' in value else 'https://' + value).hostname or ''
    except ValueError:
        return ''
    bare = host.removeprefix('www.')
    if bare in _NOT_COMPANY_SITES or any(bare.endswith('.' + site) for site in _NOT_COMPANY_SITES):
        return ''  # consulta de CNPJ, rede social ou marketplace nao e o site da empresa
    return host if re.fullmatch(r'[a-z0-9-]+(\.[a-z0-9-]+)+', host) else ''


def favicon_for(site):
    host = _hostname(site)
    return FAVICON_URL.format(domain=host) if host else None


def _brand_label(domain):
    """'nfe.riachuelo.com.br' -> 'riachuelo'."""
    for suffix in _SUFFIXES:
        if domain.endswith('.' + suffix):
            return domain[:-len(suffix) - 1].split('.')[-1]
    return domain.split('.')[0]


def _email_domains(company):
    emails = company.get('contato_email') or []
    if not isinstance(emails, (list, tuple)):
        emails = [emails]
    emails = list(emails) + [company.get('email')]
    for item in emails:
        if isinstance(item, dict):
            item = item.get('dominio') or item.get('email')
        domain = _hostname(str(item or '').rsplit('@', 1)[-1])
        if domain:
            yield domain


def domain_from_email(company, brand_tokens):
    """Dominio do e-mail so vale quando carrega a marca: contador e webmail ficam fora."""
    distinct = [t for t in brand_tokens if t not in _GENERIC | _STOPWORDS]
    slugs = {''.join(brand_tokens), ''.join(distinct), ''.join(t for t in brand_tokens if t not in _STOPWORDS)}
    strong = [t for t in distinct if len(t) >= 5]
    for domain in _email_domains(company):
        label = _brand_label(domain).replace('-', '')
        if label in _FREE_MAIL or len(label) < 3 or any(stem in label for stem in _ACCOUNTING):
            continue
        # O dominio e a marca ("riachuelo") ou comeca por ela ("riachuelomoda");
        # "casadocontador" nao e a Casa Bahia so por conter "casa".
        if label in slugs or any(label.startswith(token) for token in strong):
            return domain
    return ''


def guessed_domains(brand_tokens):
    full = ''.join(brand_tokens)
    distinct = ''.join(t for t in brand_tokens if t not in _GENERIC | _STOPWORDS)
    slugs = [s for s in dict.fromkeys([full, distinct]) if 4 <= len(s) <= 40]
    return [f'{slug}.{tld}' for slug in slugs for tld in ('com.br', 'com')]


async def _resolves(domain):
    """Dominio existe e aponta para endereco publico (nunca rede interna)."""
    if domain not in _dns_cache:
        try:
            infos = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(domain, 443, type=socket.SOCK_STREAM), 1.5)
            _dns_cache[domain] = bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)
        except Exception:  # inclui nome invalido para DNS (UnicodeError) e timeout
            _dns_cache[domain] = False
    return _dns_cache[domain]


async def _guess_domain(brand):
    """Dominio deduzido do nome, aceito so com DNS publico e pagina que cita a marca.

    'blocked' (site ativo com protecao contra robos) e aceito: e o padrao de marca
    grande, nao de dominio estacionado. 'mismatch' e 'unreachable' sao recusados.
    """
    from src.services.scrapling_company_search import get_scrapling_company_search
    service = get_scrapling_company_search()
    distinct = [t for t in brand if t not in _GENERIC | _STOPWORDS] or list(brand)
    for candidate in guessed_domains(brand):
        if not await _resolves(candidate):
            continue
        verdict = await asyncio.to_thread(service.site_matches_brand, candidate, distinct)
        if verdict in {'ok', 'blocked'}:
            return candidate
    return ''


async def resolve_company_icons(companies, searched_name=''):
    """Preenche favicon_url/site em cada empresa; nunca atrasa o dropdown alem de ~4s."""
    from src.services.company_lookup_cache import find_brand_icon
    guess_enabled = os.getenv('COMPANY_ICON_GUESS_DOMAIN', '1').lower() not in {'0', 'false', 'no', 'off'}
    searched = _tokens(searched_name)
    by_brand, by_root = {}, {}

    async def resolve(company):
        if company.get('favicon_url') or company.get('faviconUrl'):
            company['favicon_url'] = company.get('favicon_url') or company.get('faviconUrl')
            return
        root = str(company.get('cnpj') or '')[:8]
        if len(root) == 8:
            if root not in by_root:
                by_root[root] = await asyncio.to_thread(find_brand_icon, root)
            stored = by_root[root]
            if stored:
                company['favicon_url'] = stored['favicon_url']
                company['site'] = company.get('site') or stored.get('site') or ''
                return
        site = next((company.get(k) for k in ('site', 'website', 'homepage') if _hostname(company.get(k))), '')
        if site:
            company['favicon_url'] = favicon_for(site)
            return
        names = _tokens(company.get('nome_fantasia')) or _tokens(company.get('razao_social'))
        # O nome que o cliente digitou e a marca; o cadastro traz "Lojas X Filial 12".
        brand = searched if searched and all(t in names for t in searched) else names
        if not brand:
            return
        domain = domain_from_email(company, brand)
        if not domain and guess_enabled:
            key = tuple(brand)
            if key not in by_brand:
                by_brand[key] = ''  # se o tempo acabar no meio, nao repete nesta busca
                by_brand[key] = await _guess_domain(brand)
            domain = by_brand[key]
        if domain:
            company['site'] = company.get('site') or 'https://' + domain
            company['favicon_url'] = favicon_for(domain)

    async def resolve_all():
        # Em sequencia: filiais da mesma marca reaproveitam a primeira resposta.
        for company in companies:
            await resolve(company)

    try:
        # A leitura do site continua em segundo plano e fica em cache: se o tempo
        # acabar, este dropdown sai sem icone e a proxima busca da marca ja o tem.
        await asyncio.wait_for(resolve_all(), 4)
    except asyncio.TimeoutError:
        pass
    # Mesma marca, mesmo icone: uma filial sem e-mail herda o da irma.
    known = {}
    for company in companies:
        root = str(company.get('cnpj') or '')[:8]
        if company.get('favicon_url') and root:
            known.setdefault(root, (company['favicon_url'], company.get('site')))
    for company in companies:
        inherited = known.get(str(company.get('cnpj') or '')[:8])
        if inherited and not company.get('favicon_url'):
            company['favicon_url'], company['site'] = inherited[0], company.get('site') or inherited[1]
