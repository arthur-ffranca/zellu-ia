"""Don's staged gates under the Open Dots principal."""
import asyncio
from unittest.mock import AsyncMock
from src.open_dots.intake import run_intake
from src.open_dots.memory import next_missing_question
from src.services.company_intake import CompanyIntakeService
from src.services import staged_intake_flow as flow


def test_complete_report_searches_company_without_asking_report_again(monkeypatch):
    service = CompanyIntakeService()
    service._search_via_firecrawl = AsyncMock(side_effect=lambda state: state)
    update = flow.IntakeState(problem_description='Notebook com defeito', loss_amount_raw='2500 reais',
        incident_date_raw='02/10/2026', company_name_hint='Loja X', location_hint='Shopping Dom Pedro')
    understand = AsyncMock(return_value=(update, 'O que aconteceu?'))
    monkeypatch.setattr(flow, 'understand_turn', understand)
    state = {'messages': [{'role': 'user', 'content': 'Comprei ontem um notebook na Loja X do Shopping Dom Pedro por 2500 reais e veio com defeito.'}]}
    asyncio.run(run_intake(state, service))
    service._search_via_firecrawl.assert_awaited_once()
    assert len(state['messages']) == 1


def test_known_company_is_not_asked_again_when_date_is_missing():
    update = flow.IntakeState(problem_description='Notebook com defeito', loss_amount_raw='2500', company_name_hint='Loja X')
    reply = next_missing_question({}, update, 'Qual o nome da empresa?', 'Quando aconteceu?')
    assert reply == 'Quando aconteceu?'


def test_known_unknown_amount_counts_as_answer():
    update = flow.IntakeState(problem_description='Notebook com defeito', loss_amount_raw='Ainda não sei', incident_date_raw='ontem')
    assert flow.narrative_missing(update) == []


def test_attachment_is_requested_after_company_before_identity():
    update = flow.IntakeState(problem_description='Notebook com defeito', loss_amount_raw='2500', incident_date_raw='02/10/2026')
    state = {'chat_id': 'test', 'company_confirmed': True, 'staged_intake': update.model_dump(mode='json'), 'messages': []}
    asyncio.run(run_intake(state, CompanyIntakeService()))
    assert state['documents_requested']
    assert state.get('pending_form') != 'client_details'
