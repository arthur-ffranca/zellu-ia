"""Company form contract in the new principal."""
import asyncio
from unittest.mock import AsyncMock
from src.services.company_intake import CompanyIntakeService
from src.open_dots.intake import handle_company_form


def test_candidate_is_persisted_only_after_explicit_selection():
    service = CompanyIntakeService()
    service._persist_confirmed_company = AsyncMock()
    service._resolve_favicons = AsyncMock()
    from types import SimpleNamespace
    state = {'messages': [], 'step': 0}
    result = SimpleNamespace(source='test', empresas=[
        {'cnpj': '33200056000149', 'nome_fantasia': 'Loja A'},
        {'cnpj': '11222333000181', 'nome_fantasia': 'Loja B'}])
    asyncio.run(service._process_firecrawl_results(state, result))
    service._persist_confirmed_company.assert_not_called()
    selected = state['pending_form_event_type']['fields'][0]['options'][0]['value']
    asyncio.run(handle_company_form(state, service, 'company_select', {'selected_company_id': selected}))
    service._persist_confirmed_company.assert_awaited_once()
    assert state['selected_company_record']['cnpj'] == '33200056000149'


def test_company_form_keeps_branch_address_without_tax_id():
    service = CompanyIntakeService()
    form = service._build_company_select_form([{'cnpj': '33200056000149',
        'nome_fantasia': 'Loja', 'bairro': 'Shopping Dom Pedro', 'municipio': 'Campinas', 'uf': 'SP',
        'favicon_url': 'https://example.com/icon.png'}])
    option = form['fields'][0]['options'][0]
    assert option['label'] == 'Loja - Shopping Dom Pedro - Campinas/SP'
    assert option['faviconUrl'] == 'https://example.com/icon.png'
    assert option['value'].startswith('cmp_')
    assert '33200056000149' not in str(form)
    assert '33.200.056/0001-49' not in str(form)
