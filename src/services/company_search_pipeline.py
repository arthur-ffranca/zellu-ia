"""Descoberta contratada e leitura publica com Scrapling, com tempo limitado.

Localidade: a cidade so vira filtro rigido quando e um municipio real
(src/utils/data/municipios_br.json). "Barra da Tijuca" ou "Shopping Dom Pedro"
sao referencias de endereco, nao municipios - mesmo existindo Barra/BA e
Dom Pedro/MA. Quando a cidade e conhecida, unidade de outro municipio nunca
chega ao dropdown. Lista vazia vem com o motivo, que decide a proxima pergunta:

- `no_matches_in_requested_city`: cliente deu cidade e UF (ou "em <cidade>") e nao ha unidade la;
- `city_unclear`: so um nome que pode ser cidade ou bairro ("Lapa") e nao ha unidade nessa cidade;
- `city_needed`: sem cidade, candidatos em varias cidades e nada no local aponta um deles.
"""
import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.services.casa_dos_dados_search import search_companies
from src.services.scrapling_company_search import get_scrapling_company_search, CompanySearchResult
from src.utils.company_normalizer import normalize_company_list, normalize_cnpj

_STATE_NAMES = {
    'acre':'AC','alagoas':'AL','amapa':'AP','amazonas':'AM','bahia':'BA','ceara':'CE',
    'distrito federal':'DF','espirito santo':'ES','goias':'GO','maranhao':'MA',
    'mato grosso':'MT','mato grosso do sul':'MS','minas gerais':'MG','para':'PA',
    'paraiba':'PB','parana':'PR','pernambuco':'PE','piaui':'PI','rio de janeiro':'RJ',
    'rio grande do norte':'RN','rio grande do sul':'RS','rondonia':'RO','roraima':'RR',
    'santa catarina':'SC','sao paulo':'SP','sergipe':'SE','tocantins':'TO',
}
_UFS = set(_STATE_NAMES.values())
_GENERIC_LOCATION = {
    'shopping','shop','center','centre','mall','loja','lojas','unidade','filial','agencia',
    'bairro','rua','avenida','av','rodovia','estrada','praca','centro','area','rural',
    'cidade','municipio','estado','regiao','zona','interior','capital','perto','proximo',
    'proxima','dentro','frente','lado','mesmo','mesma','aqui','ali','la',
}
_STOPWORDS = {'no','na','nos','nas','em','de','do','da','dos','das','e','a','o','ao','que','fica','foi'}
_ONLINE = {'site','online','internet','app','aplicativo','ecommerce','commerce','virtual','whatsapp',
           'telefone','celular','instagram','marketplace','web'}
# "Shopping Dom Pedro", "Rua Santos Dumont", "Barra Shopping": o nome que acompanha
# essas palavras e do lugar, nao da cidade - mesmo existindo Dom Pedro/MA.
_VENUE_BEFORE = {
    'shopping','shop','mall','parque','jardim','jd','vila','vl','setor','bairro','rua','r','avenida','av',
    'alameda','travessa','largo','praca','rodovia','estrada','estacao','terminal','aeroporto','hospital',
    'galeria','mercado','feira','condominio','edificio','conjunto','residencial','loteamento','outlet',
    'plaza','boulevard','center','centro','praia','morro','ponte','viaduto','tunel',
}
_VENUE_AFTER = {'shopping','shop','mall','plaza','center','outlet','boulevard','park'}
_CITY_AND_STATE = {'sao paulo': 'SP', 'rio de janeiro': 'RJ'}


def _norm(value):
    """Minusculo, sem acento e sem pontuacao: 'Mogi-Guaçu' == 'mogi guacu'."""
    value = unicodedata.normalize('NFKD', str(value or ''))
    value = ''.join(c for c in value if not unicodedata.combining(c)).lower()
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', value)).strip()


@lru_cache(maxsize=1)
def _municipios():
    """Municipio normalizado -> UFs onde existe. Falha de leitura nao derruba o chat."""
    try:
        base = Path(__file__).resolve().parents[1] / 'utils'
        # O repo versiona o arquivo em utils/ (upload sem pasta); a pasta data/ e a original.
        path = next((p for p in (base / 'data' / 'municipios_br.json', base / 'municipios_br.json') if p.exists()),
                    base / 'data' / 'municipios_br.json')
        return {name: tuple(ufs) for name, ufs in json.loads(path.read_text(encoding='utf-8')).items()}
    except Exception as exc:
        print(f'[COMPANY-SEARCH] lista de municipios indisponivel: {type(exc).__name__}')
        return {}


def city_ufs(city):
    """UFs em que existe um municipio com esse nome (vazio se nao for municipio)."""
    return _municipios().get(_norm(city), ())


@dataclass
class Locality:
    city: str = ''            # municipio confirmado: filtro rigido
    uf: str = ''
    online: bool = False      # compra por site/app: nao ha unidade fisica para localizar
    explicit: bool = False    # cliente deu UF ou escreveu "em <cidade>": nao ha duvida de que e cidade

    @property
    def search_uf(self):
        """UF para a consulta: a informada ou a unica onde a cidade existe."""
        if self.uf:
            return self.uf
        ufs = city_ufs(self.city)
        return ufs[0] if len(ufs) == 1 else ''


def _is_municipio(name, uf=''):
    ufs = city_ufs(name)
    return bool(ufs) and (not uf or uf in ufs)


def _segments(text):
    """Palavras agrupadas pelos separadores que o cliente digitou (virgula, barra, ' - ')."""
    text = re.sub(r'[()\[\]{}!?"“”;|]', ' ', str(text or ''))
    return [seg.split() for seg in re.split(r'\s*(?:[,/]|\s[-–]\s)\s*', text) if seg.split()]


def _trailing_uf(segments):
    """Tira a UF do fim ('Campinas/SP', 'campinas sp', 'Campinas-SP') e a devolve."""
    last = segments[-1]
    word = last[-1].rstrip('.')
    glued = re.fullmatch(r'(.+)[-–]([A-Za-z]{2})', word)
    code = (glued.group(2) if glued else word).upper()
    if code not in _UFS or (not glued and len(word) != 2):
        return ''
    before = last[:-1] + ([glued.group(1)] if glued else [])
    alone = len(segments) == 1 and not before
    own_segment = not before and len(segments) > 1          # "Campinas, SP" / "Campinas/SP"
    upper = (glued.group(2) if glued else word).isupper()
    after_city = any(_is_municipio(' '.join(before[-size:]), code) for size in range(1, len(before) + 1))
    if not (alone and (upper or len(word) == 2) or own_segment or upper or after_city):
        return ''
    if before:
        segments[-1] = before
    else:
        segments.pop()
    return code


def _trailing_state(segments, has_city_before):
    """Estado por extenso no fim. Devolve (uf, nome da cidade quando o estado e tambem a cidade)."""
    last = segments[-1]
    for state_name, code in sorted(_STATE_NAMES.items(), key=lambda kv: -len(kv[0])):
        size = len(state_name.split())
        if len(last) < size or _norm(' '.join(last[-size:])) != state_name:
            continue
        if any(_is_municipio(' '.join(last[-longer:])) for longer in range(size + 1, min(len(last), size + 5) + 1)):
            return '', ''                 # "Alto Paraíso de Goiás" e um municipio, nao "..., Goiás"
        head = last[:-size]
        while head and _norm(head[-1]) in {'de', 'do', 'da', 'em', 'no', 'na', 'estado', 'interior'}:
            head = head[:-1]
        if state_name in _CITY_AND_STATE and not head and not has_city_before(code):
            return code, ' '.join(last[-size:])   # "São Paulo", "Barra da Tijuca, Rio de Janeiro": a capital
        if head:
            segments[-1] = head
        else:
            segments.pop()
        return code, ''
    return '', ''


def resolve_locality(location='', city='', uf='', exclude=''):
    """Extrai cidade/UF do que o cliente ja informou, sem chamar a LLM de novo.

    `exclude` e o nome da empresa: "Riachuelo" tambem e municipio de Sergipe.
    """
    city = str(city or '').strip()
    uf = str(uf or '').strip().upper()
    uf = uf if uf in _UFS else ''
    raw = str(location or '').strip()[:300]
    online = bool(set(_norm(raw).split()) & _ONLINE) or bool(re.search(r'\.com\b|www\.', raw, re.I))
    if city:
        return Locality(city=city, uf=uf, online=online, explicit=True)
    segments = _segments(raw)
    if not segments:
        return Locality(uf=uf, online=online)
    explicit = bool(uf)
    excluded = set(_norm(exclude).split())

    def cities_in(words, code=''):
        """Municipios dentro de um trecho: (nome, e_nome_de_lugar, veio_com_'em')."""
        found, index = [], 0
        while index < len(words):
            for size in range(min(6, len(words) - index), 0, -1):
                name = ' '.join(words[index:index + size])
                normalized = _norm(name)
                if (not normalized or normalized in _GENERIC_LOCATION or normalized in _STOPWORDS
                        or set(normalized.split()) <= excluded or not _is_municipio(name, code)):
                    continue
                before = _norm(words[index - 1]) if index else ''
                before2 = _norm(' '.join(words[max(index - 2, 0):index]))
                after = _norm(words[index + size]) if index + size < len(words) else ''
                # "Barra da Tijuca": o municipio Barra/BA e so o comeco do nome de um bairro.
                venue = before in _VENUE_BEFORE or after in _VENUE_AFTER or after in {'de', 'do', 'da', 'dos', 'das'}
                stated = before == 'em' or before2 in {'cidade de', 'cidade do', 'municipio de'}
                found.append((name, venue and not stated, stated))
                index += size - 1
                break
            index += 1
        return found

    code = _trailing_uf(segments)
    if code:
        uf, explicit = uf or code, True
    if segments and not code:
        state, capital = _trailing_state(
            segments, lambda c: any(not venue for seg in segments[:-1] for _, venue, _s in cities_in(seg, c)))
        if capital:
            return Locality(city=capital, uf=uf or state, online=online, explicit=True)
        if state:
            uf, explicit = uf or state, True

    matches = [m for seg in segments for m in cities_in(seg, uf)]
    stated = [m for m in matches if m[2]]
    plain = [m for m in matches if not m[1]]
    if stated or plain:
        name = (stated or plain)[-1][0]
        own_state = _CITY_AND_STATE.get(_norm(name), '')
        return Locality(city=name, uf=uf or own_state, online=online, explicit=explicit or bool(stated))
    # So nomes de lugar ("Shopping Dom Pedro", "Barra da Tijuca"): referencia de endereco.
    return Locality(uf=uf, online=online, explicit=explicit)


def infer_locality(location='', city='', uf=''):
    """Compatibilidade: (cidade confirmada, UF)."""
    locality = resolve_locality(location, city, uf)
    return locality.city, locality.uf


def _address_value(company, key):
    value = company.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    address = company.get('endereco')
    if isinstance(address, dict):
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _specific_location_tokens(location, *known):
    ignored = _GENERIC_LOCATION | _STOPWORDS | _ONLINE | {u.lower() for u in _UFS}
    for value in known:
        ignored |= set(_norm(value).split())
    for state_name in _STATE_NAMES:
        if re.search(r'\b' + state_name + r'\b', _norm(location)):
            ignored |= set(state_name.split())
    return [token for token in _norm(location).split() if len(token) > 2 and token not in ignored]


def _empty_city_reason(locality):
    """Sem unidade na cidade. "Lapa" sozinho pode ser Lapa/PR ou o bairro: nesse caso, perguntar."""
    return 'no_matches_in_requested_city' if locality.explicit else 'city_unclear'


def localize_companies(companies, locality, location=''):
    """Devolve (candidatos compativeis com o local, motivo quando a lista fica vazia)."""
    cleaned = normalize_company_list(companies or []).companies
    if not cleaned:
        return [], (_empty_city_reason(locality) if locality.city else 'no_matches')
    if locality.uf:
        cleaned = [c for c in cleaned if _address_value(c, 'uf').upper() in {'', locality.uf}]

    def municipio(company):
        return _norm(_address_value(company, 'municipio'))

    city = _norm(locality.city)
    if city:
        # Outro municipio nunca entra; cadastro sem municipio fica por ultimo.
        cleaned = [c for c in cleaned if municipio(c) in {'', city}]
        if not cleaned:
            return [], _empty_city_reason(locality)
    tokens = _specific_location_tokens(location, city)

    def relevance(company):
        address = _norm(' '.join(_address_value(company, key) for key in
                                 ('bairro', 'logradouro', 'complemento', 'municipio')))
        name = _norm(' '.join(str(company.get(key) or '') for key in ('nome_fantasia', 'razao_social')))
        return sum(token in address for token in tokens), sum(token in name for token in tokens)

    if not city:
        if locality.online:
            # Compra online: a matriz responde, nao uma loja fisica qualquer. Havendo
            # matriz entre os candidatos, as lojas saem da lista.
            headquarters = [c for c in cleaned if str(c.get('cnpj', ''))[8:12] == '0001']
            return headquarters or cleaned, ''
        matched = [c for c in cleaned if any(relevance(c))]
        if matched:
            cleaned = matched
        elif len({(municipio(c), _address_value(c, 'uf').upper()) for c in cleaned}) > 1:
            # Unidades espalhadas e nada no relato aponta uma delas: perguntar a
            # cidade e melhor do que oferecer lojas de outro estado.
            return [], 'city_needed'
    cleaned.sort(key=lambda c: (bool(municipio(c)), *relevance(c)), reverse=True)
    return cleaned, ''


def filter_companies_by_locality(companies, city='', uf='', location='', exclude=''):
    """Never present branches from an unrelated city when the city is known."""
    locality = resolve_locality(location, city, uf, exclude)
    return localize_companies(companies, locality, location)[0]


async def _read_public_pages(service, companies):
    """Chamadas independentes em paralelo; API mantida quando pagina bloqueada."""
    pages = await asyncio.gather(*(asyncio.to_thread(service.read_company, company['cnpj'])
                                   for company in companies[:2]), return_exceptions=True)
    pages += [None] * (len(companies) - len(pages))
    for company, page in zip(companies, pages):
        if company.get('public_page_verified') and company.get('source_url'):
            continue  # ja veio da pagina publica (busca profunda)
        if isinstance(page, dict) and page.get('cnpj') == company['cnpj']:
            company['source_url'] = page['source_url']
            company['public_page_verified'] = True
            # API e a autoridade dos dados; pagina preenche somente lacunas.
            company.setdefault('endereco', {})
            for key, value in page.get('endereco', {}).items():
                if not company['endereco'].get(key) and value:
                    company['endereco'][key] = value
        else:
            company['public_page_verified'] = False


async def search_external_companies(name, location='', segment='', city='', uf=''):
    service = get_scrapling_company_search()
    locality = resolve_locality(location, city, uf, exclude=name)
    online = locality.online and not locality.city
    result = None
    if online:
        # Site/app: pedir so matrizes; as 5 primeiras de uma busca geral seriam lojas.
        result = await search_companies(name, '', locality.search_uf, location, matriz_only=True)
    if result is None or not result.empresas:
        result = await search_companies(name, locality.city, locality.search_uf, location)
    companies, reason = localize_companies(result.empresas, locality, location)
    if not result.success:
        reason = 'no_matches'  # provedor fora do ar nao diz nada sobre a cidade

    if not companies and reason != 'city_needed':
        try:
            public = await asyncio.wait_for(
                asyncio.to_thread(service.search_cnpj, name, location, segment), timeout=12
            )
        except asyncio.TimeoutError:
            return CompanySearchResult(success=False, error='public_search_timeout')
        public.empresas, public_reason = localize_companies(public.empresas, locality, location)
        if not public.empresas:
            # O motivo ligado ao local ("sem unidade nessa cidade") vale mais que o
            # erro tecnico do buscador publico: e ele que decide a proxima pergunta.
            local = next((r for r in (public_reason, reason) if r not in {'', 'no_matches'}), '')
            public.error = local or public.error or 'no_matches'
        return public
    if not companies:
        return CompanySearchResult(empresas=[], source=result.source, error=reason)

    companies = companies[:5]
    await _read_public_pages(service, companies)
    # Reaplica o local depois do enriquecimento: a pagina pode revelar o municipio
    # de um cadastro que veio incompleto da API.
    companies, reason = localize_companies(companies, locality, location)
    companies = companies[:5]
    source = 'casadosdados+scrapling' if any(c.get('public_page_verified') for c in companies) else 'casadosdados'
    return CompanySearchResult(empresas=companies, source=source, error='' if companies else reason)


# --------------------------------------------------------------------------- busca profunda
# 2a tentativa da escada (cliente recusou os candidatos ou a 1a nao achou nada):
# mais variacoes de consulta, mais fontes, orcamento maior. Nao repete consultas ja
# feitas (skip_queries) e nunca devolve CNPJ ja recusado (exclude_cnpjs).

_NAME_NOISE = {'loja', 'lojas', 'unidade', 'filial', 'a', 'o', 'da', 'do', 'de', 'das', 'dos', 'e'}
_PLACE_CUES = re.compile(r'\b(?:rua|r|av|avenida|alameda|rodovia|estrada|praca|bairro|centro|shopping|perto|'
                         r'proximo|proxima|lado|frente|esquina|cep|cidade|piso|andar|galeria|terminal|'
                         r'estacao|mercado|posto|em|no|na)\b|\d')


def _deep_budget():
    try:
        from config import get_settings
        return float(getattr(get_settings(), 'COMPANY_DEEP_SEARCH_TIMEOUT_S', 30) or 30)
    except Exception:
        return 30.0


def split_reference_text(text):
    """Texto livre de referencia: (nome impresso, referencia de local).

    Curto e sem cara de endereco ("Mega Calcados Centro Ltda") e o nome como aparece
    na nota; o resto ("do lado do Extra da Av. Brasil") e referencia de local.
    """
    text = re.sub(r'\s+', ' ', str(text or '')).strip()[:200]
    if not text:
        return '', ''
    if len(text.split()) <= 6 and not _PLACE_CUES.search(_norm(text)):
        return text, ''
    return '', text


def _name_variants(name, reference):
    """Nome fantasia x razao social: o nome do relato, sem ruido, e o que a referencia trouxe."""
    variants = []

    def add(value):
        value = re.sub(r'\s+', ' ', str(value or '')).strip(' .,-@')
        if len(value) >= 3 and _norm(value) not in {_norm(v) for v in variants}:
            variants.append(value[:80])

    for value in (reference.get('printed_name'), reference.get('instagram'), reference.get('site_name')):
        if value:
            add(re.sub(r'[._-]+', ' ', value))
    add(name)
    add(' '.join(w for w in str(name or '').split() if _norm(w) not in _NAME_NOISE))
    return variants[:4]


def _brand_from_narrative(narrative, name, city):
    """Marca/produto citado com inicial maiuscula no meio da frase ("um Galaxy S23")."""
    skip = set(_norm(name).split()) | set(_norm(city).split()) | _STOPWORDS | _GENERIC_LOCATION
    found = []
    for sentence in re.split(r'[.!?\n]+', str(narrative or '')[:1500]):
        words = sentence.split()
        for word in words[1:]:
            clean = word.strip('.,;:()"\'')
            if (len(clean) >= 3 and clean[:1].isupper() and not clean.isupper()
                    and _norm(clean) not in skip and not city_ufs(clean) and clean not in found):
                found.append(clean)
    return ' '.join(found[:2])


def _web_queries(names, locality, place, segment, narrative, reference):
    where = ' '.join(filter(None, [locality.city, locality.search_uf or locality.uf])) or place
    main = names[0] if names else ''
    queries = []

    def add(*parts):
        query = re.sub(r'\s+', ' ', ' '.join(p for p in parts if p)).strip()
        if query and query not in queries:
            queries.append(query[:200])

    if reference.get('site'):
        add('site:' + reference['site'], 'CNPJ')
    if reference.get('instagram'):
        add('"' + reference['instagram'] + '"', 'CNPJ')
    if reference.get('place'):
        add('"' + main + '"', reference['place'], 'CNPJ')
    for variant in names[:3]:
        add('"' + variant + '"', where, 'CNPJ')
    add('"' + main + '"', place, segment, 'endereço telefone')
    brand = _brand_from_narrative(narrative, main, locality.city)
    if brand:
        add('"' + main + '"', brand, where)
    add('"' + main + '"', where, 'razão social')
    add('"' + main + '"', where, 'instagram')
    return queries


async def deep_search_companies(name, location='', segment='', narrative='', reference=None,
                                exclude_cnpjs=(), skip_queries=()):
    """Busca profunda. Devolve (CompanySearchResult, chaves das consultas feitas agora)."""
    reference = reference or {}
    budget = _deep_budget()
    place = ', '.join(filter(None, [location, reference.get('place', '')])).strip(', ')
    locality = resolve_locality(place, exclude=name)
    excluded = {normalize_cnpj(c) for c in exclude_cnpjs or () if c}
    skip = set(skip_queries or ())
    names = _name_variants(name, reference)
    done = []

    calls = []
    # Casa dos Dados em modo radical: aceita razao social diferente do nome fantasia
    # e, havendo cidade, tambem consulta so pela UF (a cidade pode ter vindo errada).
    for variant in names:
        for city in ([locality.city, ''] if locality.city else ['']):
            key = f'casa|radical|{_norm(variant)}|{_norm(city)}|{locality.search_uf}'
            if key in skip or key in done or len(calls) >= 4:
                continue
            done.append(key)
            calls.append(search_companies(variant, city, locality.search_uf, place,
                                          fuzzy=True, limit=15, timeout=min(20.0, budget)))
    queries = [q for q in _web_queries(names, locality, place, segment, narrative, reference)
               if 'web|' + q not in skip][:6]
    done += ['web|' + q for q in queries]
    service = get_scrapling_company_search()
    site = reference.get('site') or ''
    # Dominio sem www costuma redirecionar para www (o leitor nao segue redirect).
    sites = tuple(dict.fromkeys(['https://' + site + '/', 'https://www.' + site.removeprefix('www.') + '/'])) if site else ()
    if queries or sites:
        calls.append(asyncio.to_thread(service.deep_search_cnpj, name, queries, max(budget - 3, 5),
                                       10, tuple(excluded), sites))
    if not calls:
        return CompanySearchResult(empresas=[], source='deep', error='no_new_queries'), done

    tasks = [asyncio.ensure_future(call) for call in calls]
    finished, unfinished = await asyncio.wait(tasks, timeout=budget)
    for task in unfinished:
        task.cancel()  # thread do Scrapling termina sozinha; o resultado e descartado
    merged, sources = [], []
    for task in finished:
        result = None if task.cancelled() or task.exception() else task.result()
        if isinstance(result, CompanySearchResult) and result.empresas:
            sources.append(result.source)
            merged += result.empresas
    merged = [c for c in merged if isinstance(c, dict) and normalize_cnpj(c.get('cnpj')) not in excluded]
    companies, reason = localize_companies(merged, locality, place)
    companies = companies[:5]
    if companies:
        await _read_public_pages(service, companies)
        companies, reason = localize_companies(companies, locality, place)
        companies = companies[:5]
    source = 'deep:' + '+'.join(sorted(set(sources))) if sources else 'deep'
    return CompanySearchResult(empresas=companies, source=source,
                               error='' if companies else (reason or 'no_matches')), done
