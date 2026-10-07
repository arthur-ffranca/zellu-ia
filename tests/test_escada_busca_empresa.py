"""Escada de busca da empresa: relato -> web profunda -> referencia do cliente."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.open_dots.intake import run_intake
from src.services import company_search_ladder as ladder
from src.services import company_search_pipeline as pipeline
from src.services.company_intake import CompanyIntakeService
from src.services.scrapling_company_search import CompanySearchResult

CAMPINAS = {'cnpj': '33200056035872', 'nome_fantasia': 'Lojas Riachuelo',
            'endereco': {'logradouro': 'AVENIDA IGUATEMI', 'bairro': 'VILA BRANDINA',
                         'municipio': 'CAMPINAS', 'uf': 'SP'}}
CENTRO = {'cnpj': '33200056045673', 'nome_fantasia': 'Riachuelo',
          'endereco': {'logradouro': 'RUA TREZE DE MAIO', 'bairro': 'CENTRO', 'municipio': 'CAMPINAS', 'uf': 'SP'}}
DOM_PEDRO = {'cnpj': '11222333000181', 'nome_fantasia': 'Riachuelo Dom Pedro',
             'endereco': {'bairro': 'JARDIM SANTA GENEBRA', 'municipio': 'CAMPINAS', 'uf': 'SP'}}


@pytest.fixture(autouse=True)
def bubbles(monkeypatch):
    sent = []

    async def send(state, message):
        sent.append(message)
    monkeypatch.setattr(ladder, 'send_progress', send)
    return sent


def offered():
    agent = CompanyIntakeService(llm=None)
    agent._resolve_favicons = AsyncMock()
    agent._persist_confirmed_company = AsyncMock()
    state = {'messages': [], 'step': 0, 'opposing_party_name': 'Riachuelo', 'company_location_input': 'Campinas',
             'problem_description': 'Comprei uma calça com defeito.', 'company_search_attempt': 1,
             'staged_intake': {'problem_description': 'Comprei uma calça com defeito.', 'loss_amount_raw': 'R$ 200',
                               'incident_date_raw': '01/10/2026', 'company_name_hint': 'Riachuelo',
                               'location_hint': 'Campinas'}}
    asyncio.run(agent._process_firecrawl_results(state, SimpleNamespace(
        source='casadosdados', empresas=[dict(CAMPINAS), dict(CENTRO)])))
    state['pending_form_event_type'] = None
    return agent, state


def say(state, agent, text, messagedata=None):
    state['incoming_messagedata'] = messagedata
    state['messages'].append({'role': 'user', 'content': text})
    asyncio.run(run_intake(state, agent))
    return state['messages'][-1]['content']


def none_form():
    return {'dynamic_form_response': {'form_name': 'company_select', 'values': {'selected_company_id': 'none'}}}


# --------------------------------------------------------------------------- deteccao da recusa

@pytest.mark.parametrize('text,expected', [
    ('Nenhuma dessas', ''), ('nenhuma delas é a loja', ''), ('não', ''), ('Não é nenhuma dessas', ''),
    ('não é essa', ''), ('é outra loja', ''), ('estão todas erradas', ''),
    ('Não, é a do shopping Dom Pedro', 'a do shopping Dom Pedro'),
    ('não é essa, é a da Rua Treze de Maio', 'a da Rua Treze de Maio'),
    ('não tenho nenhuma nota fiscal', None), ('é a do shopping', None), ('nao lembro o nome', None),
    ('[Mensagem com arquivo(s)]', None),
])
def test_recusa_em_texto(text, expected):
    assert ladder.rejection_remainder(text) == expected


def test_referencia_extraida_sem_confundir_email():
    ref = ladder.extract_reference({}, 'o insta é @riachuelo.campinas e o email contato@gmail.com')
    assert ref['instagram'] == 'riachuelo.campinas' and 'site' not in ref
    ref = ladder.extract_reference({}, 'www.megacalcados.com.br')
    assert ref['site'] == 'megacalcados.com.br' and ref['site_name'] == 'megacalcados'
    assert ladder.extract_reference({}, 'Mega Calçados Ltda') == {'printed_name': 'Mega Calçados Ltda'}
    assert ladder.extract_reference({}, 'fica do lado do Extra na Av. Brasil')['place']
    assert ladder.extract_reference({}, 'CNPJ 11.222.333/0001-81')['cnpj'] == '11222333000181'
    assert ladder.extract_reference({}, 'não sei') == {}


def test_cnpj_da_foto_da_nota_e_usado_e_chave_nfe_nao_vira_cnpj():
    key = '3526 1011 2223 3300 0181 5500 1000 0000 0110 0000 0001'
    state = {'files': ['https://storage.test/nota.jpg'], 'case_evidence': [
        {'text': f'NFC-e chave {key.replace(" ", "")} EMITENTE CNPJ: 11.222.333/0001-81'}]}
    assert ladder.extract_reference(state, '[Mensagem com arquivo(s)]')['cnpj'] == '11222333000181'


# --------------------------------------------------------------------------- escada

def test_recusa_vai_para_busca_profunda_sem_reoferecer_recusados(bubbles):
    agent, state = offered()

    async def deep(s, reference, select_message, confirm_prefix):
        assert set(s['company_rejected_cnpjs']) == {CAMPINAS['cnpj'], CENTRO['cnpj']}
        result = SimpleNamespace(source='deep:scrapling_deep', empresas=[dict(CAMPINAS), dict(DOM_PEDRO)])
        return await agent._process_firecrawl_results(s, result, select_message=select_message,
                                                      confirm_prefix=confirm_prefix) is not None
    agent._deep_search_company = AsyncMock(side_effect=deep)
    reply = say(state, agent, '[Resposta]', none_form())
    assert bubbles == [ladder.MSG_DEEP_SEARCH]
    assert state['company_search_attempt'] == 2
    # So sobrou um candidato novo: confirmacao com o prefixo da busca profunda.
    assert state['pending_form'] == 'company_confirm'
    assert reply.startswith('Procurei mais a fundo.')
    assert state['pending_company_confirm']['cnpj'] == DOM_PEDRO['cnpj']


def test_segunda_recusa_pede_referencia_e_busca_com_ela_sem_repetir_a_pergunta(bubbles):
    agent, state = offered()
    agent._deep_search_company = AsyncMock(return_value=False)
    first = say(state, agent, '[Resposta]', none_form())
    assert first == ladder.REFERENCE_ASKS[0] and state['company_awaiting_reference']
    second = say(state, agent, 'fica do lado do Extra na Av. Brasil')
    reference = agent._deep_search_company.await_args_list[-1].args[1]
    assert reference['place'] == 'fica do lado do Extra na Av. Brasil'
    assert second == ladder.REFERENCE_ASKS[1]
    third = say(state, agent, 'não sei')
    assert third == ladder.REFERENCE_ASKS[2]
    assert len({first, second, third}) == 3
    final = say(state, agent, 'Mega Loja')
    assert state['pending_form'] == 'cnpj_or_retry' and not state['company_awaiting_reference']
    assert final == ladder.EXHAUSTED[0]
    assert ladder.MSG_REFERENCE_SEARCH in bubbles


def test_recusa_em_texto_com_referencia_e_cnpj_informado(monkeypatch):
    agent, state = offered()
    agent._deep_search_company = AsyncMock(return_value=False)
    say(state, agent, 'Não é nenhuma dessas')
    assert state['company_awaiting_reference']
    monkeypatch.setattr('src.services.company_lookup_cache.find_company_by_cnpj', lambda c: dict(DOM_PEDRO))
    reply = say(state, agent, 'o CNPJ é 11.222.333/0001-81')
    assert state['pending_form'] == 'company_confirm'
    assert 'Jardim Santa Genebra' in reply or 'JARDIM SANTA GENEBRA' in reply


def test_nao_na_confirmacao_unica_tambem_sobe_a_escada():
    agent, state = offered()
    agent._emit_company_confirmation(state, dict(CENTRO, municipio='Campinas', uf='SP'), 'ok')
    agent._deep_search_company = AsyncMock(return_value=False)
    say(state, agent, 'não')
    assert CENTRO['cnpj'] in state['company_rejected_cnpjs']
    agent._deep_search_company.assert_awaited_once()


def test_primeira_tentativa_vazia_aprofunda_direto(monkeypatch, bubbles):
    monkeypatch.setattr('src.services.company_intake.find_company_lookup_results', lambda *a: [])
    monkeypatch.setattr(pipeline, 'search_companies', AsyncMock(return_value=CompanySearchResult(source='casadosdados')))
    service = Mock()
    service.search_cnpj.return_value = CompanySearchResult(empresas=[])
    monkeypatch.setattr(pipeline, 'get_scrapling_company_search', lambda: service)
    agent = CompanyIntakeService(llm=None)
    agent._deep_search_company = AsyncMock(return_value=False)
    state = {'opposing_party_name': 'Riachuelo', 'company_location_input': 'Campinas/SP', 'messages': []}
    asyncio.run(agent._search_via_firecrawl(state))
    agent._deep_search_company.assert_awaited_once()
    assert state['messages'][-1]['content'] == ladder.REFERENCE_ASKS[0]


# --------------------------------------------------------------------------- busca profunda

def test_busca_profunda_amplia_sem_repetir_e_exclui_recusados(monkeypatch):
    search = AsyncMock(return_value=CompanySearchResult(empresas=[dict(CAMPINAS), dict(DOM_PEDRO)], source='casadosdados'))
    monkeypatch.setattr(pipeline, 'search_companies', search)
    service = Mock()
    service.deep_search_cnpj.return_value = CompanySearchResult(empresas=[], source='scrapling_deep')
    service.read_company.return_value = None
    monkeypatch.setattr(pipeline, 'get_scrapling_company_search', lambda: service)
    result, done = asyncio.run(pipeline.deep_search_companies(
        'Lojas Riachuelo', 'Campinas/SP', narrative='Comprei um Galaxy na loja.',
        exclude_cnpjs=[CAMPINAS['cnpj']]))
    assert [c['cnpj'] for c in result.empresas] == [DOM_PEDRO['cnpj']]
    assert all(call.kwargs['fuzzy'] and call.kwargs['limit'] > 5 for call in search.await_args_list)
    queries = service.deep_search_cnpj.call_args.args[1]
    assert any('Galaxy' in q for q in queries) and len(queries) <= 6
    # Mesma situacao de novo: nada a repetir.
    search.reset_mock()
    again, _ = asyncio.run(pipeline.deep_search_companies('Lojas Riachuelo', 'Campinas/SP',
                                                          narrative='Comprei um Galaxy na loja.', skip_queries=done))
    search.assert_not_awaited()
    assert again.error == 'no_new_queries'


def test_resultado_da_busca_profunda_e_salvo_no_mongo(monkeypatch):
    agent = CompanyIntakeService(llm=None)
    agent._resolve_favicons = AsyncMock()
    found = CompanySearchResult(empresas=[dict(DOM_PEDRO), dict(CENTRO)], source='deep:casadosdados')
    monkeypatch.setattr(pipeline, 'deep_search_companies', AsyncMock(return_value=(found, ['web|x'])))
    save = Mock(return_value=2)
    monkeypatch.setattr('src.services.company_intake.save_company_lookup_results', save)
    state = {'opposing_party_name': 'Riachuelo', 'company_location_input': 'Campinas', 'messages': [], 'step': 0,
             'company_rejected_cnpjs': [CENTRO['cnpj']]}
    assert asyncio.run(agent._deep_search_company(state, {}, ladder.DEEP_SELECT, ladder.DEEP_CONFIRM_PREFIX))
    assert save.call_args.kwargs['source'] == 'deep:casadosdados'
    assert state['company_search_queries'] == ['web|x']
    assert state['pending_company_confirm']['cnpj'] == DOM_PEDRO['cnpj']  # recusado saiu da lista


def test_link_do_bing_e_desembrulhado():
    import base64
    from src.services.scrapling_company_search import _unwrap_result_link
    target = 'https://www.riachuelo.com.br/lojas'
    encoded = 'a1' + base64.urlsafe_b64encode(target.encode()).decode().rstrip('=')
    assert _unwrap_result_link('https://www.bing.com/ck/a?!&&p=abc&u=' + encoded + '&ntb=1') == target
    assert _unwrap_result_link('/url?q=https://cnpj.biz/1&sa=U') == 'https://cnpj.biz/1'


def test_cnpj_do_rodape_do_site_entra_sem_virar_verificado(monkeypatch):
    from scrapling.parser import Selector
    from src.services.scrapling_company_search import ScraplingCompanySearch

    class Page:
        status = 200
        parser = Selector('<title>Mega Calçados | Loja oficial</title><footer>CNPJ 11.222.333/0001-81</footer>')
        def css(self, selector): return self.parser.css(selector)
        def get_all_text(self, **kwargs): return self.parser.get_all_text(**kwargs)
    service = ScraplingCompanySearch()
    monkeypatch.setattr(service, '_get', Mock(return_value=Page()))
    monkeypatch.setattr(service, 'read_company', Mock(return_value=None))  # diretorio bloqueado (403)
    result = service.deep_search_cnpj('Mega Calçados', [], 10, sites=('https://megacalcados.com.br/',))
    assert result.empresas[0]['cnpj'] == '11222333000181'
    assert result.empresas[0]['nome_fantasia'] == 'Mega Calçados'
    assert result.empresas[0]['public_page_verified'] is False
