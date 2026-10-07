import asyncio
from unittest.mock import AsyncMock, Mock
import pytest
from src.services import casa_dos_dados_search as provider


def configure(monkeypatch, data, status=200):
    monkeypatch.setenv('API_CNPJ_CONNECTION_URL', 'https://api.casadosdados.com.br')
    monkeypatch.setenv('API_CNPJ_CONNECTION_KEY', 'test-key')
    post = AsyncMock(return_value=Mock(status_code=status, json=lambda: data))
    client = AsyncMock()
    client.__aenter__.return_value.post = post
    monkeypatch.setattr(provider.httpx, 'AsyncClient', lambda **kwargs: client)
    return post


def test_provider_prioritizes_shopping_not_store_inside_it(monkeypatch):
    configure(monkeypatch, {'cnpjs': [
        {'cnpj': '45771473000120', 'nome_fantasia': 'Cololido Shopping Iguatemi'},
        {'cnpj': '58997354000132', 'nome_fantasia': 'Shopping Center Iguatemi Campinas'},
        {'cnpj': '90400888299180', 'nome_fantasia': 'Banco Santander'}]})
    result = asyncio.run(provider.search_companies('Shopping Iguatemi', 'Campinas'))
    assert result.empresas[0]['cnpj'] == '58997354000132'
    assert len(result.empresas) == 2


def test_empty_name_never_runs_paid_search(monkeypatch):
    post = configure(monkeypatch, {})
    result = asyncio.run(provider.search_companies(''))
    assert result.error == 'empty_company_name'
    post.assert_not_called()


def test_provider_prioritizes_local_address_and_sends_city(monkeypatch):
    post = configure(monkeypatch, {'cnpjs': [
        {'cnpj': '33200056045673', 'nome_fantasia': 'Riachuelo', 'endereco': {'logradouro': 'Selma Parada'}},
        {'cnpj': '33200056035872', 'nome_fantasia': 'Lojas Riachuelo', 'endereco': {'logradouro': 'Avenida Iguatemi'}}]})
    result = asyncio.run(provider.search_companies('Riachuelo', 'Campinas', 'SP', 'Shopping Iguatemi Campinas'))
    assert result.empresas[0]['cnpj'] == '33200056035872'
    payload = post.call_args.kwargs['json']
    assert payload['municipio'] == ['campinas']
    assert payload['situacao_cadastral'] == ['ATIVA']


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_provider_failure_is_explicit_and_never_returns_companies(monkeypatch, status):
    configure(monkeypatch, {}, status)
    result = asyncio.run(provider.search_companies('Iguatemi'))
    assert not result.success
    assert result.error == f'http_{status}'
    assert not result.empresas
