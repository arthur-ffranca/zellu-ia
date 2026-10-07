import pytest
from src.state_manager import StateManager
from src.services.voice_chat_handoff import deliver, review_reply
from src.services.chat_completion import prepare_completion, can_finish


class Client:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)


DATA = {
    'conversationId': 'call_test', 'summary': 'Notebook chegou com a tela rachada.',
    'transcricao': [{'role': 'user', 'text': 'Comprei um notebook e veio rachado.'}],
}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_transcricao_revisao_sem_finalizar_e_reentrega_sem_duplicar():
    manager, client = StateManager(), Client()
    link = {'chatId': 'chat_test', 'userId': 'user_test'}
    assert (await deliver(DATA, link, manager, client))['status'] == 'delivered'
    state = manager.get_state('chat_test')
    assert state['problem_description'] == DATA['summary']
    assert state['voice_review_pending']
    assert 'Comprei um notebook' in client.calls[0]['message']
    assert client.calls[0]['is_finished'] is False
    assert (await deliver(DATA, link, manager, client))['status'] == 'already_delivered'
    assert len(client.calls) == 1


@pytest.mark.anyio
async def test_nao_associa_por_telefone_nem_session_id_de_voz():
    manager, client = StateManager(), Client()
    assert (await deliver(DATA, {'sessionId': 'voice', 'userId': 'user'}, manager, client))['status'] == 'pending_chat_link'
    manager.create_state('chat', 'outra_conta')
    assert (await deliver(DATA, {'chatId': 'chat', 'userId': 'user'}, manager, client))['status'] == 'identity_mismatch'
    assert not client.calls


@pytest.mark.parametrize('answer', ['SIM!', 's.', 'Está correto.', 'isso mesmo'])
def test_confirmacao_libera_fluxo(answer):
    state = {'voice_review_pending': True, 'problem_description': DATA['summary']}
    assert review_reply(state, answer) is None
    assert state['voice_review_pending'] is False
    assert state['problem_description'] == DATA['summary']


def test_negacao_e_correcao_nao_confirmam_sozinhas():
    state = {'voice_review_pending': True, 'problem_description': DATA['summary']}
    assert review_reply(state, 'Não.')
    assert 'Correcao' not in state['problem_description']
    assert review_reply(state, 'A tela ficou preta, nao rachada.')
    assert state['voice_review_pending']
    assert 'tela ficou preta' in state['problem_description']


@pytest.mark.parametrize('value', [None, 0, -10, True, float('nan'), float('inf')])
def test_sem_valor_valido_nao_promete_chamado_criado(value):
    message, finished, groups = prepare_completion(
        'Chamado criado!', True, {'estimatedValue': value}, [{'is_finished': True}]
    )
    assert not finished
    assert 'Chamado criado' not in message
    assert not groups[0]['is_finished']


def test_valor_declarado_positivo_finaliza():
    assert can_finish({'estimatedValue': 6400.0})
    assert prepare_completion('Pronto', True, {'estimatedValue': 6400}, None) == ('Pronto', True, None)


@pytest.mark.anyio
async def test_first_voice_report_is_retained_before_new_intake(monkeypatch):
    from src.services.company_intake import CompanyIntakeService
    from src.open_dots.intake import run_intake
    from src.services import staged_intake_flow as flow

    seen = {}

    async def understand_turn(intake, message, recent):
        seen['problem_description'] = intake.problem_description
        seen['case_summary'] = intake.case_summary
        return flow.IntakeState(
            problem_description=intake.problem_description,
            case_summary=intake.case_summary,
            loss_amount_raw='nao sei ainda',
            incident_date_raw='hoje',
        ), ''

    monkeypatch.setattr('src.open_dots.intake.flow.understand_turn', understand_turn)
    state = {
        'problem_description': DATA['summary'],
        'messages': [{'role': 'user', 'content': 'A empresa foi a Loja X'}],
    }
    await run_intake(state, CompanyIntakeService())
    assert state['problem_description'] == DATA['summary']
    assert state['problem_description_original'] == DATA['summary']
    assert seen == {
        'problem_description': DATA['summary'],
        'case_summary': DATA['summary'],
    }
