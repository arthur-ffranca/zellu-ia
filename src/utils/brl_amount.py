"""Valores em reais escritos de qualquer jeito -> numero.

Entende: "R$ 8.000,00", "8000", "8k", "8 K", "8 mil", "8,5 mil", "8 mil e 500",
"1,5 milhao", "oito mil", "oitocentos reais", "dois mil e quinhentos",
"mil e quinhentos", "um milhao e meio", "cento e vinte reais".

Duas entradas:
- parse_brl_amount(valor): o campo inteiro e um valor (ex.: loss_amount_raw).
- iter_brl_amounts(texto): varre um relato; so aceita numero com marca de
  dinheiro (R$, "reais", k, mil, milhao) para nao confundir "dia 12" com R$ 12.
Nunca levanta excecao: sem valor -> None / lista vazia.
"""
import re
import unicodedata

_UNITS = {
    'zero': 0, 'um': 1, 'uma': 1, 'dois': 2, 'duas': 2, 'tres': 3, 'quatro': 4, 'cinco': 5,
    'seis': 6, 'sete': 7, 'oito': 8, 'nove': 9, 'dez': 10, 'onze': 11, 'doze': 12,
    'treze': 13, 'catorze': 14, 'quatorze': 14, 'quinze': 15, 'dezesseis': 16,
    'dezasseis': 16, 'dezessete': 17, 'dezassete': 17, 'dezoito': 18, 'dezenove': 19,
    'dezanove': 19,
}
_TENS = {'vinte': 20, 'trinta': 30, 'quarenta': 40, 'cinquenta': 50, 'sessenta': 60,
         'setenta': 70, 'oitenta': 80, 'noventa': 90}
_HUNDREDS = {'cem': 100, 'cento': 100, 'duzentos': 200, 'trezentos': 300, 'quatrocentos': 400,
             'quinhentos': 500, 'seiscentos': 600, 'setecentos': 700, 'oitocentos': 800,
             'novecentos': 900}
_SCALES = {'mil': 1_000, 'milhao': 1_000_000, 'milhoes': 1_000_000,
           'bilhao': 1_000_000_000, 'bilhoes': 1_000_000_000}
_HALF = {'meio', 'meia'}
_WORDS = set(_UNITS) | set(_TENS) | set(_HUNDREDS) | set(_SCALES) | _HALF

_SUFFIX = {'k': 1_000, 'mil': 1_000, 'milhao': 1_000_000, 'milhoes': 1_000_000,
           'bilhao': 1_000_000_000, 'bilhoes': 1_000_000_000}
_CURRENCY_AFTER = {'real', 'reais'}


def normalize(text):
    text = unicodedata.normalize('NFKD', str(text or ''))
    return ''.join(c for c in text if not unicodedata.combining(c)).lower()


def words_to_number(tokens):
    """['dois','mil','e','quinhentos'] -> 2500. None se algum token nao for numeral."""
    total, current, last_scale, seen = 0.0, 0.0, 0, False
    for token in tokens:
        if token == 'e':
            continue
        if token in _UNITS:
            current += _UNITS[token]
        elif token in _TENS:
            current += _TENS[token]
        elif token in _HUNDREDS:
            current += _HUNDREDS[token]
        elif token in _HALF:
            if last_scale and current == 0:
                total += last_scale / 2          # "um milhao e meio"
            else:
                current += 0.5                   # "meio milhao"
        elif token in _SCALES:
            scale = _SCALES[token]
            total += (current or 1) * scale
            current, last_scale = 0.0, scale
        else:
            return None
        seen = True
    return total + current if seen else None


def _digits_to_number(raw):
    """'8.000' -> 8000 | '8,5' -> 8.5 | '1.500,50' -> 1500.5 | '8.5' -> 8.5."""
    raw = raw.strip().strip('.,')
    if not raw:
        return None
    try:
        if ',' in raw and '.' in raw:
            return float(raw.replace('.', '').replace(',', '.'))
        if ',' in raw:
            return float(raw.replace(',', '.'))
        if re.fullmatch(r'\d{1,3}(?:\.\d{3})+', raw):
            return float(raw.replace('.', ''))
        return float(raw)
    except ValueError:
        return None


_DIGIT_RE = re.compile(
    r'(?P<cur>r\s*\$\s*)?'
    r'(?<![\w.,])(?P<num>\d+(?:[.,]\d+)*)'
    r'(?:\s*(?P<suf>k|milhoes|milhao|bilhoes|bilhao|mil)(?![a-z]))?'
    r'(?P<tail>\s*(?:e\s*(?P<extra>\d+(?:[.,]\d+)*)(?![\d.,]*\s*(?:k|mil)\b))?)'
    r'(?:\s*(?P<real>reais|real)(?![a-z]))?')


def _scan_digits(text):
    for m in _DIGIT_RE.finditer(text):
        base = _digits_to_number(m.group('num'))
        if base is None:
            continue
        suffix = m.group('suf')
        has_money = bool(m.group('cur') or m.group('real') or suffix)
        if not has_money:
            continue
        value = base * _SUFFIX[suffix] if suffix else base
        end = m.end('suf') if suffix else m.end('num')
        extra = m.group('extra')
        # "8 mil e 500" -> 8500 (so quando o complemento e menor que a escala)
        if suffix and extra:
            add = _digits_to_number(extra)
            if add is not None and add < _SUFFIX[suffix]:
                value += add
                end = m.end('extra')
        if m.group('real'):
            end = max(end, m.end('real'))
        yield value, m.start('cur') if m.group('cur') else m.start('num'), end


_TOKEN_RE = re.compile(r'[a-z]+|\$')


def _scan_words(text):
    tokens = [(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]
    index = 0
    while index < len(tokens):
        if tokens[index][0] not in _WORDS:
            index += 1
            continue
        start = index
        while index < len(tokens) and (tokens[index][0] in _WORDS or (
                tokens[index][0] == 'e' and index + 1 < len(tokens) and tokens[index + 1][0] in _WORDS
                and index > start)):
            index += 1
        run = tokens[start:index]
        value = words_to_number([t[0] for t in run])
        if value is None or value <= 0:
            continue
        nxt = tokens[index][0] if index < len(tokens) else ''
        prev = tokens[start - 1][0] if start else ''
        names = {t[0] for t in run}
        marked = (nxt in _CURRENCY_AFTER or prev in {'r', '$'}
                  or bool(names & set(_SCALES)) or bool(names & set(_HUNDREDS) - {'cem'}))
        # "um"/"dois"... sozinhos sem 'reais' nao sao dinheiro ("um dia", "duas vezes")
        if not marked:
            continue
        end = run[-1][2]
        if nxt in _CURRENCY_AFTER:
            end = tokens[index][2]
        yield value, run[0][1], end


def iter_brl_amounts(text):
    """(valor, inicio, fim) de cada valor em reais do texto, em ordem. Offsets sobre normalize(texto)."""
    try:
        norm = normalize(text)
        found = sorted([*_scan_digits(norm), *_scan_words(norm)], key=lambda t: (t[1], -t[2]))
        out, last_end = [], -1
        for value, start, end in found:
            if start < last_end:
                continue                         # sobreposto a um valor ja lido
            out.append((float(value), start, end))
            last_end = end
        return out
    except Exception:
        return []


def find_brl_amounts(text):
    return [v for v, _s, _e in iter_brl_amounts(text)]


def parse_brl_amount(value):
    """Campo inteiro -> float. Aceita numero puro ('8'), 'R$ 8k', 'oito mil', 'oitocentos'."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        norm = normalize(value).strip()
        if not norm:
            return None
        found = iter_brl_amounts(norm)
        if len(found) == 1:
            return found[0][0]
        if len(found) > 1:
            return None                          # varios valores: nao adivinhar
        bare = re.sub(r'(?:r\s*\$|reais|real)', ' ', norm).strip()
        if re.fullmatch(r'\d+(?:[.,]\d+)*', bare):
            return _digits_to_number(bare)
        tokens = re.findall(r'[a-z]+', bare)
        if tokens and all(t in _WORDS or t == 'e' for t in tokens):
            return words_to_number(tokens)
        return None
    except Exception:
        return None
