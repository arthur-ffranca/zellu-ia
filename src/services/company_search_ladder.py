"""Escada de busca da empresa: relato -> web profunda -> referencia do cliente.

1. Tentativa 1 (CompanyIntakeService._search_via_firecrawl): Mongo -> Casa dos Dados
   -> Scrapling, com o que o cliente contou.
2. Cliente recusa os candidatos (ou a 1a nao acha nada): busca profunda na web,
   sem repetir consultas e sem reoferecer CNPJ recusado.
3. Nada de novo: pedir uma referencia (CNPJ, site/Instagram, endereco, nome na nota,
   foto da nota) e buscar com ela. A pergunta muda a cada vez e tem teto; esgotado,
   cai no formulario cnpj_or_retry que ja existia.

Estado (sobrevive aos turnos): company_search_attempt, company_rejected_cnpjs,
company_search_queries, company_awaiting_reference, company_reference_asks,
company_reference_hints.
"""
import asyncio
import re
import unicodedata

from src.services.intake_progress import MSG_DEEP_SEARCH, MSG_REFERENCE_SEARCH, send_progress
from src.services.staged_intake_adapter import emit
from src.utils.company_normalizer import normalize_cnpj, is_valid_cnpj

DEEP_SELECT = 'Procurei mais a fundo e achei estas outras opções. Alguma delas é a certa?'
DEEP_CONFIRM_PREFIX = 'Procurei mais a fundo. '
REFERENCE_SELECT = 'Com essa referência achei estas opções. Qual delas é a certa?'
REFERENCE_CONFIRM_PREFIX = 'Com essa referência, '
REFERENCE_ASKS = (
    'Não achei a unidade certa ainda. Tem outra referência que ajude? Pode ser o CNPJ, o site ou '
    'Instagram da loja, o endereço ou bairro, o nome como aparece na nota, ou uma foto da nota fiscal.',
    'Ainda não encontrei. Se tiver a nota fiscal ou o comprovante, manda uma foto — o CNPJ costuma vir '
    'impresso. Ou me diz um ponto de referência perto da loja.',
    'Quase lá! Me passa o nome da loja exatamente como aparece no recibo, ou o @ do Instagram ou o site dela.',
)
FILE_WITHOUT_CNPJ = ('Recebi o arquivo, mas não consegui achar o CNPJ nele. Consegue me dizer o nome da loja '
                     'como aparece no recibo, ou o endereço?')
CNPJ_UNAVAILABLE = 'Não consegui consultar esse CNPJ agora. Confere o número pra mim?'
EXHAUSTED = (
    'Não consegui encontrar a empresa nem com as referências que você passou. Como prefere seguir?',
    'Continuo sem achar essa empresa. Escolhe abaixo como quer seguir.',
)

_CNPJ_RE = re.compile(r'(?<!\d)\d{2}[.\s]?\d{3}[.\s]?\d{3}[/\s]?\d{4}[-\s]?\d{2}(?!\d)')  # nao pega trecho da chave da NF-e
_INSTAGRAM_RE = re.compile(r'instagram\.com/([A-Za-z0-9_.]{2,30})|(?<![\w.])@([A-Za-z0-9_.]{2,30})', re.I)
_SITE_RE = re.compile(r'(?<![@\w.])(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+(?:com|net|org|store|shop|br)'
                      r'(?:\.br)?)\b', re.I)
_SOCIAL = ('instagram.com', 'facebook.com', 'tiktok.com', 'wa.me', 'whatsapp.com', 'google.com')
_FILLER = {'o', 'a', 'e', 'eh', 'site', 'insta', 'instagram', 'perfil', 'pagina', 'cnpj', 'dela', 'dele',
           'da', 'do', 'loja', 'empresa', 'numero', 'aqui', 'ta', 'esta', 'segue', 'tem', 'no', 'na', 'sim'}
_DECLINED = {'nao sei', 'nao tenho', 'nao lembro', 'sei nao', 'nao', 'n', 'nenhuma', 'nenhum', 'nao faco ideia'}
# Recusa em texto com um formulario de empresa na tela. Ancorada no inicio:
# "não tenho nenhuma nota fiscal" e relato, nao recusa.
_REJECTION_LEADS = (
    r'nenhum[a]?(?: (?:dessas|desses|delas|deles|das opcoes|das lojas|das unidades))?'
    r'(?: (?:e|eh) (?:a )?(?:loja|unidade|certa|correta|essa))?',
    r'nao (?:e|eh|era|foi|sao) (?:nenhum[a]?|essa|esta|essas|estas|ela|elas|isso|aqui|nessa|nesta|'
    r'a certa|a correta|essa loja|essa unidade)(?: (?:dessas|delas|loja|unidade))?',
    r'(?:e |eh )?outra (?:loja|unidade|empresa|filial)',
    r'(?:(?:tao|estao|ta|esta) )?(?:todas )?errad[ao]s?',
)


def _plain(text):
    text = unicodedata.normalize('NFKD', str(text or ''))
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', text)).strip()


def rejection_remainder(text):
    """None quando o texto nao recusa os candidatos; senao o que veio depois da recusa."""
    raw = str(text or '').rsplit('?:', 1)[-1].strip()  # "<pergunta>?: <resposta>" do renderer legado
    plain = _plain(raw)
    if not plain or raw.startswith('['):
        return None
    lead = ''
    for pattern in _REJECTION_LEADS:
        match = re.match(r'(?:' + pattern + r')(?= |$)', plain)
        if match and len(match.group(0)) > len(lead):
            lead = match.group(0)
    if not lead:
        if plain in {'nao', 'n', 'nao e'}:
            return ''
        # "Não, é a do shopping Iguatemi": recusa seguida de referencia.
        if re.match(r'^\s*n[aã]o\s*[,.;!–-]', raw, re.I):
            lead = 'nao'
        else:
            return None
    words = [w for w in re.split(r'[\s,.;:!?–-]+', raw) if w]
    rest = ' '.join(words[len(lead.split()):])
    return re.sub(r'^(?:e|é|eh|mas)\s+', '', rest, flags=re.I).strip()


def record_rejection(state):
    """Guarda os CNPJs oferecidos e recusados; nenhuma tentativa volta a oferece-los."""
    offered = [c.get('cnpj') for c in state.get('company_search_results') or [] if isinstance(c, dict)]
    offered.append((state.get('pending_company_confirm') or {}).get('cnpj'))
    rejected = list(state.get('company_rejected_cnpjs') or [])
    for cnpj in offered:
        cnpj = normalize_cnpj(cnpj)
        if is_valid_cnpj(cnpj) and cnpj not in rejected:
            rejected.append(cnpj)
    state['company_rejected_cnpjs'] = rejected[-50:]


def reset_candidates(state):
    from src.open_dots.intake import clear_form
    clear_form(state)
    state.update(company_confirmed=False, company_search_results=[], pending_company_confirm=None,
                 selected_company_record=None, opposing_party_cnpj=None)
    staged = state.setdefault('staged_intake', {})
    staged['selected_company'] = None
    staged['company_candidates'] = []


def _cnpj_from_evidence(state):
    """CNPJ impresso na foto/PDF da nota enviada neste turno (o do emitente vem primeiro)."""
    if not state.get('files'):
        return ''
    rejected = set(state.get('company_rejected_cnpjs') or [])
    for record in reversed(state.get('case_evidence') or []):
        for raw in _CNPJ_RE.findall(str(record.get('text') or '')[:20000]):
            cnpj = normalize_cnpj(raw)
            if is_valid_cnpj(cnpj) and cnpj not in rejected and len(re.sub(r'\D', '', raw)) == 14:
                return cnpj
    return ''


def extract_reference(state, text):
    """{'cnpj', 'site', 'site_name', 'instagram', 'printed_name', 'place'} do que o cliente mandou."""
    from src.services.company_search_pipeline import split_reference_text
    raw = str(text or '').strip()
    reference = {}
    for match in _CNPJ_RE.findall(raw):
        cnpj = normalize_cnpj(match)
        if is_valid_cnpj(cnpj):
            reference['cnpj'] = cnpj
            break
    if not reference.get('cnpj'):
        found = _cnpj_from_evidence(state)
        if found:
            reference['cnpj'] = found
    insta = _INSTAGRAM_RE.search(raw)
    if insta:
        reference['instagram'] = (insta.group(1) or insta.group(2)).strip('.')
    for match in _SITE_RE.finditer(raw):
        host = match.group(1).lower()
        if not any(host == s or host.endswith('.' + s) for s in _SOCIAL):
            reference['site'] = host
            reference['site_name'] = host.split('.')[0]
            break
    if raw and not raw.startswith('[') and _plain(raw) not in _DECLINED:
        free = _INSTAGRAM_RE.sub(' ', _SITE_RE.sub(' ', _CNPJ_RE.sub(' ', raw)))
        # "o site é ...", "o CNPJ é ...": sobra so moldura, nao e nome nem local.
        if set(_plain(free).split()) <= _FILLER:
            free = ''
        printed, place = split_reference_text(free)
        if printed:
            reference['printed_name'] = printed
        if place:
            reference['place'] = place
    return reference


def _flat(company):
    """Formulario de confirmacao le municipio/uf/bairro no topo do dict."""
    flat = dict(company or {})
    for key, value in (flat.get('endereco') or {}).items():
        if value and not flat.get(key):
            flat[key] = value
    return flat


def ask_reference(state, companies, message=None):
    """Pede uma referencia nova; frase diferente a cada vez e teto antes do formulario."""
    asks = int(state.get('company_reference_asks') or 0)
    state['company_reference_asks'] = asks + 1
    if asks >= len(REFERENCE_ASKS):
        state['company_awaiting_reference'] = False
        companies._firecrawl_fallback_no_results(state, 'no_matches')
        state['messages'][-1]['content'] = EXHAUSTED[(asks - len(REFERENCE_ASKS)) % len(EXHAUSTED)]
        return state
    state['company_awaiting_reference'] = True
    state['company_search_attempt'] = 3
    return emit(state, message or REFERENCE_ASKS[asks])


async def search_by_cnpj(state, companies, cnpj):
    """CNPJ informado (texto ou nota): mesma escada do formulario cnpj_input."""
    from src.services.company_lookup_cache import find_company_by_cnpj
    from src.services.scrapling_company_search import get_scrapling_company_search
    await send_progress(state, MSG_REFERENCE_SEARCH)
    try:
        company = await asyncio.to_thread(find_company_by_cnpj, cnpj)
    except Exception:
        company = None
    if not company:
        state['opposing_party_cnpj'] = cnpj
        status = await companies._create_company_from_cnpj(state)
        if status in {'created', 'exists', 'inactive'}:
            company = {'cnpj': cnpj, 'nome_fantasia': state.get('opposing_party_name')}
        else:
            try:
                company = await asyncio.to_thread(get_scrapling_company_search().read_company, cnpj)
            except Exception:
                company = None
    if not company:
        state['company_awaiting_reference'] = True  # mesma pergunta nao conta como novo pedido
        return emit(state, CNPJ_UNAVAILABLE)
    company = _flat(company)
    companies._emit_company_confirmation(state, company, companies._build_company_confirm_message(company))
    return state


async def search_with_reference(state, companies, reference):
    state['company_search_attempt'] = 3
    hint = ' '.join(str(reference.get(k) or '') for k in ('printed_name', 'place', 'site', 'instagram')).strip()
    if hint:
        state['company_reference_hints'] = ((state.get('company_reference_hints') or []) + [hint[:200]])[-10:]
    await send_progress(state, MSG_REFERENCE_SEARCH)
    if await companies._deep_search_company(state, reference, REFERENCE_SELECT, REFERENCE_CONFIRM_PREFIX):
        return state
    return ask_reference(state, companies)


async def escalate_company_search(state, companies, remainder='', rejected=True):
    """Cliente recusou os candidatos (rejected) ou a 1a tentativa veio vazia."""
    if rejected:
        record_rejection(state)
    reset_candidates(state)
    reference = extract_reference(state, remainder) if remainder else {}
    if reference.get('cnpj'):
        return await search_by_cnpj(state, companies, reference['cnpj'])
    attempt = int(state.get('company_search_attempt') or 1)
    if attempt < 2:
        state['company_search_attempt'] = 2
        await send_progress(state, MSG_DEEP_SEARCH)
        if await companies._deep_search_company(state, reference, DEEP_SELECT, DEEP_CONFIRM_PREFIX):
            return state
        return ask_reference(state, companies)
    if reference:
        return await search_with_reference(state, companies, reference)
    return ask_reference(state, companies)


async def handle_company_reference(state, companies):
    """Resposta ao pedido de referencia. Sempre aguarda o cliente (form ou pergunta)."""
    state['company_awaiting_reference'] = False
    text = companies._latest_user_message_text(state)
    reference = extract_reference(state, text)
    if reference.get('cnpj'):
        return await search_by_cnpj(state, companies, reference['cnpj'])
    if not reference:
        if state.get('files'):
            return ask_reference(state, companies, FILE_WITHOUT_CNPJ)
        return ask_reference(state, companies)
    return await search_with_reference(state, companies, reference)
