from src.open_dots.intake import run_intake
import asyncio
from unittest.mock import AsyncMock, Mock
import pytest
from src.services import staged_intake_adapter as adapter
from src.services import staged_intake_flow as flow
from src.services.company_intake import CompanyIntakeService


def evidence(status='read', audit='CONSISTENT', stored=True):
    return {'id': 'file', 'status': status, 'original_stored': stored, 'audit': {'status': audit}}


@pytest.mark.parametrize('records,expected', [([], False), ([evidence()], True),
    ([evidence('partial')], True), ([evidence('unread')], True),  # recebido e nao lido segue, com aviso
    ([evidence(audit='BLOCKED')], False), ([evidence(stored=False)], True),
    ([evidence(), evidence('processing')], False)])
def test_attachment_gate(records, expected):
    assert adapter.attachment_gate({'case_evidence': records}) is expected


def test_malware_never_satisfies_gate():
    record = evidence()
    record['audit']['securityFlags'] = ['MALWARE_DETECTED']
    assert not adapter.attachment_gate({'case_evidence': [record]})


def test_mongo_matriz_hit_skips_paid_search(monkeypatch):
    import src.services.company_intake as module
    agent = CompanyIntakeService(llm=None, company_search_client=Mock())
    agent.company_search_client.advanced_search = AsyncMock()
    agent._set_matriz_company = AsyncMock(return_value='matriz_found')
    monkeypatch.setattr(module, 'find_company_by_cnpj', lambda cnpj: {'cnpj': cnpj, 'razao_social': 'Riachuelo'})
    state = {'opposing_party_filial_cnpj': '33200056035872'}
    assert asyncio.run(agent._search_matriz_for_filial(state)) == 'matriz_found'
    agent.company_search_client.advanced_search.assert_not_called()
    assert agent._set_matriz_company.call_args.args[1]['cnpj'] == '33200056000149'


def test_full_turn_goes_directly_to_company_search(monkeypatch):
    agent = CompanyIntakeService(llm=None)
    agent._search_via_firecrawl = AsyncMock(side_effect=lambda state: state)
    updated = flow.IntakeState(problem_description='Defeito no notebook', loss_amount_raw='8 mil',
        incident_date_raw='ontem', company_name_hint='Loja X', location_hint='Campinas', case_summary='Notebook com defeito.')
    monkeypatch.setattr(flow, 'understand_turn', AsyncMock(return_value=(updated, 'Uma pergunta')))
    report = 'Comprei ontem na Loja X em Campinas um notebook por 8 mil reais e veio quebrado.'
    state = {'messages': [{'role': 'user', 'content': report}]}
    asyncio.run(run_intake(state, agent))
    agent._search_via_firecrawl.assert_called_once()
    assert state['problem_description_original'] == report


@pytest.mark.parametrize('confidence,margin,source', [(0.9, 0.8, 'JEV'), (0.69, 0.8, 'GPT5_FALLBACK'),
    (0.9, 0.49, 'GPT5_FALLBACK'), (0.7, 0.5, 'JEV')])
def test_legal_fallback_thresholds(monkeypatch, confidence, margin, source):
    monkeypatch.setattr(flow, 'choose_with_jev', AsyncMock(return_value={
        'chosen_article_id': 'article', 'confidence': confidence, 'margin': margin}))
    fallback = AsyncMock(return_value='article')
    monkeypatch.setattr(flow, 'choose_with_gpt5', fallback)
    state = {'case_confirmed': True, 'case_evidence': [evidence()], 'staged_intake': {}}
    docs = [{'id': 'article', 'content': 'Lei', 'metadata': {}}]
    assert asyncio.run(adapter.route_legal_candidates(state, docs)) == docs
    assert state['legal_decision']['source'] == source
    assert fallback.call_count == int(source == 'GPT5_FALLBACK')


def test_legal_rag_cannot_run_without_confirmation():
    with pytest.raises(ValueError):
        asyncio.run(adapter.route_legal_candidates({'case_evidence': [evidence()]}, []))


def test_prototype_does_not_advance_with_processing_attachment():
    state = flow.IntakeState(attachments=[flow.AttachmentAudit(document_id='1', filename='a', audit_status='CONSISTENT'),
        flow.AttachmentAudit(document_id='2', filename='b')])
    assert not flow.attachment_gate_passed(state)


def ready_state():
    return {'messages': [{'role': 'user', 'content': 'Confirmado'}],
        'problem_description': 'Problema descrito.', 'company_confirmed': True,
        'client_details_confirmed': True, 'client_name': 'Joao Teste',
        'client_phone': '5511999999999', 'client_email': 'joao@example.com', 'client_cpf': '52998224725',
        'staged_intake': flow.IntakeState(problem_description='Problema descrito.', loss_amount_raw='nao sei ainda',
            incident_date_raw='09/09/2026').model_dump(mode='json'),
        'case_evidence': [evidence()], 'case_summary': 'Resumo do problema.',
        'pending_form': 'case_confirmation', 'incoming_messagedata': {'dynamic_form_response': {
            'form_name': 'case_confirmation', 'values': {'case_confirmed': 'yes'}}}}


def test_confirmation_unlocks_existing_orchestrator_route(monkeypatch):
    from src.open_dots.agent import DotAgent
    monkeypatch.setattr('src.open_dots.intake.save_record', lambda state: {})
    state = ready_state()
    asyncio.run(run_intake(state, CompanyIntakeService(llm=None)))
    assert state['case_confirmed'] and state['ready_for_classification'] and state['validated']
    orchestrator = object.__new__(DotAgent)
    assert orchestrator.ready(state)


def test_confirmation_does_not_bypass_processing_file(monkeypatch):
    monkeypatch.setattr('src.open_dots.intake.save_record', lambda state: {})
    state = ready_state()
    state['case_evidence'].append(evidence('processing'))
    asyncio.run(run_intake(state, CompanyIntakeService(llm=None)))
    assert not state['ready_for_classification']


def test_manual_cnpj_uses_existing_backend_method_after_mongo_miss(monkeypatch):
    from src.services import company_lookup_cache
    monkeypatch.setattr(company_lookup_cache, 'find_company_by_cnpj', lambda cnpj: None)
    agent = CompanyIntakeService(llm=None)
    agent._create_company_from_cnpj = AsyncMock(return_value='exists')
    state = ready_state()
    state['pending_form'] = 'cnpj_input'
    state['incoming_messagedata'] = {'dynamic_form_response': {'form_name': 'cnpj_input',
        'values': {'opposing_party_cnpj': '33200056000149'}}}
    asyncio.run(run_intake(state, agent))
    agent._create_company_from_cnpj.assert_called_once()
    assert state['pending_form'] == 'company_confirm'
