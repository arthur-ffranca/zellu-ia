"""Datas em PT-BR: preserva a expressao e a precisao, sem inventar dias."""
import calendar
import re
import json
import unicodedata
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/Sao_Paulo')
MONTHS = dict(zip('janeiro fevereiro marco abril maio junho julho agosto setembro outubro novembro dezembro'.split(), range(1, 13)))
NUMBERS = {'um': 1, 'uma': 1, 'dois': 2, 'duas': 2, 'tres': 3, 'quatro': 4,
           'cinco': 5, 'seis': 6, 'sete': 7, 'oito': 8, 'nove': 9, 'dez': 10,
           'onze': 11, 'doze': 12, 'quinze': 15, 'vinte': 20, 'trinta': 30}


def resolve_date_mentions(text, reference=None):
    reference = reference or datetime.now(TZ)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=TZ)
    today = reference.astimezone(TZ).date()
    # A normalizacao mantem as posicoes dos caracteres para recuperar o original.
    normalized = ''.join(''.join(c for c in unicodedata.normalize('NFKD', char)
                                 if not unicodedata.combining(c)) for char in text).lower()
    results, occupied = [], []

    def add(match, start=None, end=None, precision='day', status='resolved'):
        if any(match.start() < b and match.end() > a for a, b in occupied):
            return
        occupied.append(match.span())
        results.append({'expression': text[match.start():match.end()],
                        'start': start.isoformat() if start else None,
                        'end': (end or start).isoformat() if start else None,
                        'precision': precision, 'status': status,
                        'reference_timestamp': reference.isoformat()})

    for pattern, iso in [(r'\b(\d{4})-(\d{2})-(\d{2})\b', True),
                         (r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b', False)]:
        for match in re.finditer(pattern, normalized):
            values = list(map(int, match.groups()))
            year, month, day = values if iso else values[::-1]
            try:
                add(match, date(year, month, day))
            except ValueError:
                add(match, status='invalid')
    for match in re.finditer(r'\b(\d{1,2}) de (' + '|'.join(MONTHS) + r') de (\d{4})\b', normalized):
        try:
            add(match, date(int(match[3]), MONTHS[match[2]], int(match[1])))
        except ValueError:
            add(match, status='invalid')
    for word, delta in [('anteontem', 2), ('ontem', 1), ('hoje', 0), ('amanha', -1)]:
        for match in re.finditer(r'\b' + word + r'\b', normalized):
            add(match, today - timedelta(days=delta))
    weekdays = {'segunda': 0, 'terca': 1, 'quarta': 2, 'quinta': 3, 'sexta': 4, 'sabado': 5, 'domingo': 6}
    pattern = r'\b(?:ultima?\s+)?(' + '|'.join(weekdays) + r')(?:[- ]feira)?(?: passada?| anterior| ultima)?\b'
    for match in re.finditer(pattern, normalized):
        if not any(word in match[0] for word in ('passad', 'anterior', 'ultim')):
            add(match, status='needs_confirmation')
            continue
        delta = (today.weekday() - weekdays[match[1]]) % 7 or 7
        add(match, today - timedelta(days=delta))
    for match in re.finditer(r'\b(?:semana passada|ultima semana)\b', normalized):
        start = today - timedelta(days=today.weekday() + 7)
        add(match, start, start + timedelta(days=6), 'week')
    for match in re.finditer(r'\b(?:mes passado|ultimo mes)\b', normalized):
        end = today.replace(day=1) - timedelta(days=1)
        add(match, end.replace(day=1), end, 'month')
    for match in re.finditer(r'\b(?:ha|faz|fazem)\s+(\d+|' + '|'.join(NUMBERS) + r')\s+(dias?|semanas?|mes|meses|anos?)\b', normalized):
        amount = int(match[1]) if match[1].isdigit() else NUMBERS[match[1]]
        unit = match[2]
        try:
            if unit.startswith('dia') or unit.startswith('semana'):
                resolved = today - timedelta(days=amount * (7 if unit.startswith('semana') else 1))
            else:
                offset = amount * (12 if unit.startswith('ano') else 1)
                year, month = divmod(today.year * 12 + today.month - 1 - offset, 12)
                month += 1
                resolved = date(year, month, min(today.day, calendar.monthrange(year, month)[1]))
            add(match, resolved, precision='approximate_day')
        except (ValueError, OverflowError):
            add(match, status='invalid')
    return sorted(results, key=lambda item: next((a for a, b in occupied if text[a:b] == item['expression']), 0))


def temporal_prompt(state):
    now = state.get('intake_reference_timestamp') or datetime.now(TZ).isoformat()
    return (f'Referencia temporal do servidor: {now}; fuso America/Sao_Paulo. '
            'Datas numericas usam dia/mes/ano. Entenda expressoes relativas usando esta referencia. '
            'Se nao houver dia exato, preserve o periodo e nao invente uma data. '
            'Uma data invalida ou ambigua requer confirmar somente a data, sem reiniciar o relato. '
            'Leia integralmente o relato, inclusive os detalhes finais; preserve valores, fatos e correcoes. '
            'Interpretacoes registradas (nao transformar periodos em dias exatos): '
            + json.dumps(state.get('case_date_mentions') or [], ensure_ascii=False))
