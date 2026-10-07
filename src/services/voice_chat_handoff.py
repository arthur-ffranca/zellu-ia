"""Entrega o relato de voz ao chat identificado pelo backend, sem abrir chamado."""
import asyncio
import re
from src.utils.text_normalizer import normalize_user_text


def review_reply(state, text):
    """None libera o intake; texto devolvido mantem a revisao pendente."""
    normalized = re.sub(r'[^\w\s]', ' ', normalize_user_text(text))
    normalized = ' '.join(normalized.split())
    if normalized in {'sim', 's', 'ok', 'correto', 'esta correto', 'isso', 'isso mesmo', 'confirmo', 'pode seguir'}:
        state['voice_review_pending'] = False
        state.setdefault('messages', []).append({
            'role': 'assistant', 'content': 'Relato confirmado. Vamos identificar a empresa.',
            'agent': 'intake',
        })
        return None
    if normalized in {'nao', 'n', 'nao esta correto', 'errado'} or not normalized:
        return 'Qual parte do relato precisa ser corrigida? Escreva a correcao para conferirmos antes de selecionar a empresa.'
    # A correcao e atribuida ao cliente; nao substitui silenciosamente os fatos.
    state['problem_description'] += '\nCorrecao informada pelo cliente: ' + text.strip()
    return 'Relato atualizado:\n' + state['problem_description'] + '\n\nAgora esta correto?'


def review_message(data):
    # A transcricao integral permanece no registro da chamada. Exibir tambem
    # o resumo extraido para a pessoa corrigir antes da busca da empresa.
    turns = data.get('transcricao') or []
    text = '\n'.join(
        str(turn.get('text') or '').strip()
        for turn in turns if turn.get('role') == 'user' and turn.get('text')
    )
    summary = data.get('summary') or ''
    return (
        'Transcricao da sua fala:\n' + (text or summary)
        + '\n\nResumo do relato:\n' + summary
        + '\n\nConfira o relato. Esta correto? Se precisar, escreva a correcao. '
        'Depois vamos identificar e selecionar a empresa.'
    )


async def deliver(data, response, state_manager, client):
    chat_id = response.get('chatId') or response.get('chat_id')
    user_id = response.get('userId')
    # Nunca deduzir uma conta pelo telefone nem confundir sessionId de voz
    # com chatId. O backend precisa fornecer o vinculo autenticado/verificado.
    if not chat_id or not user_id:
        return {'status': 'pending_chat_link'}
    state = state_manager.get_state(chat_id)
    if state and state.get('user_id') != user_id:
        return {'status': 'identity_mismatch'}
    if state and state.get('voice_conversation_id') == data['conversationId'] and state.get('voice_review_delivered'):
        return {'status': 'already_delivered'}
    if state and state.get('problem_description') and state.get('voice_conversation_id') != data['conversationId']:
        return {'status': 'existing_case'}
    if not state:
        state_manager.create_state(chat_id, user_id)
    state_manager.update_state(chat_id, {
        'voice_conversation_id': data['conversationId'],
        'voice_review_pending': True,
        'problem_description': data['summary'],
        'completed': False, 'ready_for_classification': False,
        'validated': False,
    })
    state = state_manager.get_state(chat_id)
    if not state.get('client_phone') and data.get('phone'):
        state_manager.update_state(chat_id, {'client_phone': data['phone']})
    if not state.get('client_name'):
        name = ' '.join(filter(None, [data.get('firstName'), data.get('lastName')]))
        if name:
            state_manager.update_state(chat_id, {'client_name': name})
    try:
        await client.send_message(
            chat_id=chat_id, message=review_message(data), is_finished=False,
            messagedata={'voiceReview': {
                'conversationId': data['conversationId'],
                'summary': data['summary'], 'transcription': data['transcricao'],
                'requiresConfirmation': True, 'nextStep': 'company_selection',
            }},
        )
    except Exception:
        return {'status': 'delivery_failed'}
    state_manager.update_state(chat_id, {'voice_review_delivered': True})
    return {'status': 'delivered', 'chatId': chat_id}


def deliver_registered_call(data, response):
    from src.main import state_manager, zellu_client
    return asyncio.run(deliver(data, response, state_manager, zellu_client))
