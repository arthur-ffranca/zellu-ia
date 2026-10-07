"""Relato de aeroporto numa cidade sem aeroporto comercial: avisar em vez de buscar a empresa.

Base: src/utils/aeroportos_br.json (aeroportos com codigo IATA, gerada por
scripts/build_aeroportos_br.py). Cobre aeroportos com voos comerciais; campos
de pouso pequenos sem IATA nao entram - por isso o texto diz "nao encontrei" e
devolve a pergunta ao cliente, e o aviso so sai UMA vez por cidade (se ele
insistir, o fluxo segue normalmente). Nunca levanta excecao.
"""
import json
from functools import lru_cache
from pathlib import Path

from src.services.company_search_pipeline import _norm, city_ufs, resolve_locality

_MAX_SUGGESTIONS = 5


@lru_cache(maxsize=1)
def _base():
    base = Path(__file__).resolve().parents[1] / 'utils'
    for path in (base / 'data' / 'aeroportos_br.json', base / 'aeroportos_br.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return data.get('aeroportos') or [], set(data.get('apelidos') or [])
        except Exception:
            continue
    return [], set()


def mentions_airport(*texts):
    return any('aeroporto' in _norm(t).split() or 'aeroportos' in _norm(t).split() for t in texts if t)


def _pretty(city):
    return city.title() if city.islower() or city.isupper() else city


def airport_notice(state):
    """Mensagem para o cliente, ou None (segue o fluxo normal)."""
    try:
        airports, nicknames = _base()
        if not airports:
            return None
        name = state.get('opposing_party_name') or ''
        location = state.get('company_location_input') or ''
        problem = state.get('problem_description') or ''
        if not mentions_airport(name, location, problem):
            return None
        blob = ' ' + _norm(' '.join([name, location, problem])) + ' '
        if any(f' {nick} ' in blob for nick in nicknames):
            return None                                   # "Congonhas", "Galeao": aeroporto nomeado
        loc = resolve_locality(location, exclude=name)
        if not loc.city or (loc.online and not loc.explicit):
            return None
        ufs = [loc.uf] if loc.uf else list(city_ufs(loc.city))
        if not ufs:
            return None                                   # nao e municipio conhecido: nao afirmar nada
        city_key = _norm(loc.city)
        if any(city_key in a['municipios'] and (not a['uf'] or a['uf'] in ufs) for a in airports):
            return None
        key = f"{city_key}/{','.join(sorted(ufs))}"
        if state.get('airport_check_key') == key:
            return None                                   # ja avisamos desta cidade: seguir
        state['airport_check_key'] = key
        city = _pretty(loc.city)
        where = f"{city}/{ufs[0]}" if len(ufs) == 1 else city
        near = sorted({(a['municipios'][0], a['iata']) for a in airports if a['uf'] == ufs[0] and a['municipios']})
        text = f"Não encontrei aeroporto com voos comerciais em {where}."
        if len(ufs) == 1 and near:
            shown = ', '.join(f"{_pretty(c)} ({iata})" for c, iata in near[:_MAX_SUGGESTIONS])
            more = ' e outros' if len(near) > _MAX_SUGGESTIONS else ''
            text += f" Em {ufs[0]} os aeroportos com voos comerciais ficam em {shown}{more}."
        text += " Foi em algum deles, ou em outro lugar? Me diga o nome do aeroporto e a cidade."
        return text
    except Exception:
        return None
