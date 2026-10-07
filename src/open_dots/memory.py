"""Deterministic question memory; does not modify Don's conversation prompt."""
import re
import unicodedata


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(text or '').lower())
                   if not unicodedata.combining(c))


def answered_topics(state):
    messages = state.get('messages') or []
    text = normalize(' '.join(str(m.get('content') or '') for m in messages if m.get('role') == 'user')
                     + ' ' + str(state.get('problem_description') or ''))
    topics = set(state.get('answered_topics') or [])
    patterns = {'valor': r'\b(?:reais?|valor|gastei|paguei|custo|custou)\b|r\$',
                'boletim': r'boletim|\bb\.?o\.?\b', 'prova': r'foto|video|prova|comprovante',
                'testemunha': r'testemunha', 'lesao': r'lesao|machuquei|feri|fratura|dor'}
    for topic, pattern in patterns.items():
        if re.search(pattern, text):
            topics.add(topic)
    # Bind binary answers to their actual preceding question, not any old question.
    for index, message in enumerate(messages):
        answer = re.sub(r'[^a-z ]', '', normalize(message.get('content'))).strip()
        if message.get('role') != 'user' or answer not in {'sim', 's', 'yes', 'nao', 'n', 'no'}:
            continue
        previous = next((m for m in reversed(messages[:index]) if m.get('role') == 'assistant'), {})
        for topic, pattern in patterns.items():
            if re.search(pattern, normalize(previous.get('content'))):
                topics.add(topic)
    if state.get('case_evidence'):
        topics.add('prova')
    state['answered_topics'] = sorted(topics)
    return topics


def remove_answered_questions(state, reply):
    topics = answered_topics(state)
    patterns = {'valor': r'valor|quanto|gasto|custou|pagou', 'boletim': r'boletim|\bb\.?o\.?\b',
                'prova': r'foto|video|prova|comprovante', 'testemunha': r'testemunha',
                'lesao': r'lesao|machuc|feriment|fratura'}
    kept = []
    for sentence in re.split(r'(?<=[.!?])\s+', str(reply or '')):
        if '?' in sentence and any(re.search(patterns[topic], normalize(sentence)) for topic in topics if topic in patterns):
            continue
        kept.append(sentence)
    return ' '.join(kept).strip()


def next_missing_question(state, updated, reply, fallback):
    """Keep the model's tone while refusing questions about already-filled gates.

    Prefere a resposta nao-vazia da LLM (tom humano) em vez do template frio,
    desde que nao repergunte um gate ja preenchido.
    """
    cleaned = remove_answered_questions(state, reply)
    question = normalize(cleaned)
    known = [(updated.problem_description, r'o que aconteceu|conte.*aconteceu|descreva.*problema'),
             (updated.loss_amount_raw or updated.loss_amount_brl is not None, r'quanto|qual.*valor|algum.*prejuizo|algum.*gasto'),
             (updated.incident_date_raw, r'quando|qual.*data|qual.*dia|qual.*periodo'),
             (updated.company_name_hint, r'qual.*(?:empresa|loja)|nome.*(?:empresa|loja)'),
             (updated.location_hint, r'onde|qual.*(?:unidade|shopping|bairro|cidade)')]
    if cleaned and not any(value and re.search(pattern, question) for value, pattern in known):
        return cleaned
    return fallback
