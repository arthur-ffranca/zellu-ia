import asyncio
from unittest.mock import AsyncMock, Mock
from src.services import company_search_pipeline as pipeline
from src.services.scrapling_company_search import CompanySearchResult


def test_api_candidates_are_read_by_scrapling_and_missing_address_filled(monkeypatch):
    monkeypatch.setattr(pipeline, 'search_companies', AsyncMock(return_value=CompanySearchResult(
        empresas=[{'cnpj': '58997354000132', 'razao_social': 'Iguatemi', 'endereco': {'municipio': 'Campinas'}}], source='casadosdados')))
    service = Mock()
    service.read_company.return_value = {'cnpj': '58997354000132', 'source_url': 'https://cnpj.biz/58997354000132',
                                         'endereco': {'municipio': 'Outra cidade', 'bairro': 'Vl Brandina'}}
    monkeypatch.setattr(pipeline, 'get_scrapling_company_search', lambda: service)
    result = asyncio.run(pipeline.search_external_companies('Iguatemi', city='Campinas'))
    assert result.source == 'casadosdados+scrapling'
    assert result.empresas[0]['endereco'] == {'municipio': 'Campinas', 'bairro': 'Vl Brandina'}
    service.read_company.assert_called_once_with('58997354000132')
    service.search_cnpj.assert_not_called()


def test_blocked_source_retains_api_result_without_claiming_verified(monkeypatch):
    monkeypatch.setattr(pipeline, 'search_companies', AsyncMock(return_value=CompanySearchResult(
        empresas=[{'cnpj': '58997354000132', 'razao_social': 'Iguatemi'}])))
    service = Mock()
    service.read_company.return_value = None
    monkeypatch.setattr(pipeline, 'get_scrapling_company_search', lambda: service)
    result = asyncio.run(pipeline.search_external_companies('Iguatemi'))
    assert result.source == 'casadosdados'
    assert result.empresas[0]['public_page_verified'] is False


def test_unavailable_api_uses_public_search(monkeypatch):
    monkeypatch.setattr(pipeline, 'search_companies', AsyncMock(return_value=CompanySearchResult(success=False,error='http_503')))
    service = Mock()
    service.search_cnpj.return_value = CompanySearchResult(empresas=[])
    monkeypatch.setattr(pipeline, 'get_scrapling_company_search', lambda: service)
    result = asyncio.run(pipeline.search_external_companies('Iguatemi', 'Campinas', 'shopping'))
    service.search_cnpj.assert_called_once_with('Iguatemi', 'Campinas', 'shopping')
    assert result.empresas == []
