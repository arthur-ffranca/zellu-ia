"""Case intake owned by Dot: explicit gates, no inherited agent state machine."""
import asyncio
from datetime import datetime

from src.services import staged_intake_flow as flow
from src.services.client_intake_record import client_details_required, intake_gate, valid_personal, save_record
from src.services.staged_intake_adapter import attachment_gate, attachments_blocked_message, emit
from src.utils.company_normalizer import normalize_cnpj, is_valid_cnpj
from src.utils.intake_dates import temporal_prompt, resolve_date_mentions


def normalize_loss_amount(updated, message):
    """'8k', '8 mil', 'oito mil', 'oitocentos reais' -> loss_amount_brl, sem depender so da LLM.

    O texto literal do cliente (loss_amount_raw) manda: se ele le como um valor so, corrige o numero
    que a LLM devolveu. Varios valores no campo ou no relato: nao adivinhar qual e o prejuizo.
    """
    from src.utils.brl_amount import parse_brl_amount, find_brl_amounts
    try:
        parsed = parse_brl_amount(updated.loss_amount_raw)
        if parsed is not None:
            updated.loss_amount_brl = parsed
            return
        if updated.loss_amount_brl is None and not updated.loss_amount_raw:
            amounts = find_brl_amounts(message)
            if len(amounts) == 1:
                updated.loss_amount_brl = amounts[0]
                updated.loss_amount_raw = ('R$ {:,.2f}'.format(amounts[0])
                                           .replace(',', '#').replace('.', ',').replace('#', '.'))
    except Exception:
        pass


COMPANY_FORMS = {'company_select', 'company_confirm', 'company_refine', 'cnpj_input', 'cnpj_or_retry', 'cnpj_knows'}


def clear_form(state):
    for key in ('pending_form', 'pending_form_fields', 'pending_form_event_type', 'pending_form_snapshot', 'incoming_messagedata'):
        state[key] = None
    state['pending_form_misses'] = 0


def reoffer_form(state, companies, message, count=True):
    """Reenvia o formulario pendente junto com o aviso.

    main.py consome pending_form_event_type ao enviar; sem recolocar o form, o
    cliente recebia so o texto "selecione uma opcao" e nao tinha onde selecionar.
    """
    form = state.get('pending_form_snapshot')
    if not form and state.get('pending_form') == 'company_select' and state.get('company_search_results'):
        form = companies._build_company_select_form(state['company_search_results'])  # sessao anterior ao snapshot
    if form:
        state['pending_form_event_type'] = state['pending_form_snapshot'] = form
    state['pending_form_misses'] = int(state.get('pending_form_misses') or 0) + (1 if count else 0)
    return emit(state, message)


def release_company_form(state):
    """Cliente respondeu em texto livre duas vezes: volta a conversa em vez de travar no form."""
    clear_form(state)
    state.update(company_confirmed=False, pending_company_confirm=None, company_search_results=[])
    staged = state.setdefault('staged_intake', {})
    staged['selected_company'] = None
    staged['company_candidates'] = []


def resolve_company(state, selected):
    """Only resolve an ID/CNPJ against this session's offered records."""
    selected = str(selected or '').strip()
    records = state.get('company_search_results') or []
    # Typed staged sessions also persist the candidate ID alongside the CNPJ.
    candidates = (state.get('staged_intake') or {}).get('company_candidates') or []
    if not records:
        records = candidates
    matches = []
    for company in records:
        cnpj = normalize_cnpj(company.get('cnpj'))
        identifiers = {str(company.get(key)) for key in ('id', 'company_id', 'companyId', 'candidate_id') if company.get(key)}
        identifiers |= {str(c.get('id')) for c in candidates
                        if c.get('id') and cnpj and normalize_cnpj(c.get('cnpj')) == cnpj}
        if selected in identifiers or (is_valid_cnpj(selected) and normalize_cnpj(selected) == cnpj):
            matches.append(company)
    if len(matches) != 1 or not is_valid_cnpj(matches[0].get('cnpj')):
        return None
    return matches[0]


async def confirm_company(state, company, companies):
    cnpj = normalize_cnpj(company.get('cnpj'))
    name = next((company.get(k) for k in ('nome_fantasia', 'nomeFantasia', 'tradeName', 'razao_social', 'razaoSocial', 'companyName', 'display_name', 'name') if company.get(k)), state.get('opposing_party_name'))
    company_id = company.get('id') or company.get('companyId') or company.get('company_id')
    selected = {**company, 'cnpj': cnpj, 'name': name,
                'location': state.get('company_location_input'),
                'faviconUrl': company.get('favicon_url') or company.get('faviconUrl')}
    state.update(company_confirmed=True, selected_company_record=selected,
                 opposing_party_name=name, opposing_party_cnpj=cnpj,
                 opposing_party_found_in_db=True, opposing_party_company_id=company_id,
                 _company_to_persist=dict(company))
    staged = state.setdefault('staged_intake', {})
    staged['selected_company'] = {'id': str(company_id or cnpj), 'cnpj': cnpj,
                                  'display_name': name or cnpj, 'address': state.get('company_location_input')}
    staged['stage'] = flow.Stage.ATTACHMENTS.value
    clear_form(state)
    # Persistence outages do not make the user reselect a company.
    try:
        await companies._persist_confirmed_company(state)
    except Exception:
        state['company_selection_saved_to_mongo'] = False
    # Preserve the selected branch; any parent lookup is separate from selection.
    state.update(opposing_party_filial_cnpj=None, opposing_party_matriz_cnpj=None,
                 company_parent_lookup_pending=False, company_parent_lookup_tries=0)
    if companies._parse_cnpj_type(cnpj).get('is_filial'):
        state['opposing_party_filial_cnpj'] = cnpj
        await link_parent_company(state, companies)


async def link_parent_company(state, companies):
    """Cliente escolhe a filial; o chamado segue para a matriz, sem nova pergunta.

    Se a matriz nao for localizada agora (base ou plataforma fora do ar), o chamado
    continua na filial e a busca e refeita nos proximos turnos, ate tres vezes.
    """
    state['company_parent_lookup_tries'] = int(state.get('company_parent_lookup_tries') or 0) + 1
    try:
        status = await companies._search_matriz_for_filial(state)
    except Exception:
        status = 'error'
    state['company_parent_lookup_pending'] = status != 'matriz_found'


async def handle_company_form(state, companies, pending, values):
    """Return True when the turn must wait; accepted selections continue in-place."""
    if pending == 'company_select':
        selected = values.get('selected_company_id') or values.get('selected_company_cnpj')
        if selected is None and len(values) == 1:
            selected = next(iter(values.values()))  # campo com outro nome, um unico valor
        if selected != 'none' and resolve_company(state, selected) is None:
            # Renderer que envia o rotulo visivel no lugar do id da opcao.
            from src.services.dynamic_form_bridge import option_value_from_label
            selected = option_value_from_label(state, 'selected_company_id', selected) or selected
        if selected == 'none':
            # Escada: recusa -> busca profunda na web -> pedido de referencia.
            from src.services.company_search_ladder import escalate_company_search
            await escalate_company_search(state, companies)
            return True
        company = resolve_company(state, selected)
        if company is None:
            reoffer_form(state, companies, 'Não consegui registrar a escolha. Selecione a unidade na lista abaixo.', count=False)
            return True
        await confirm_company(state, company, companies)
        return False
    if pending == 'company_confirm':
        choice = values.get('company_confirmed')
        company = state.get('pending_company_confirm') or {}
        if choice == 'yes' and is_valid_cnpj(company.get('cnpj')):
            await confirm_company(state, company, companies)
            state['pending_company_confirm'] = None
            return False
        if choice == 'no':
            from src.services.company_search_ladder import escalate_company_search
            await escalate_company_search(state, companies)
            return True
        emit(state, 'Confirme a empresa no formulário para continuar.')
        return True
    if pending == 'cnpj_input':
        cnpj = normalize_cnpj(values.get('opposing_party_cnpj'))
        if not is_valid_cnpj(cnpj):
            emit(state, 'Confira o CNPJ informado; o número não é válido.')
            return True
        state['opposing_party_cnpj'] = cnpj
        from src.services.company_lookup_cache import find_company_by_cnpj
        company = await asyncio.to_thread(find_company_by_cnpj, cnpj)
        if not company:
            status = await companies._create_company_from_cnpj(state)
            if status not in {'created', 'exists', 'inactive'}:
                emit(state, 'Não consegui consultar esse CNPJ agora. Confira o número ou tente novamente.')
                return True
            company = {'cnpj': cnpj, 'nome_fantasia': state.get('opposing_party_name')}
        clear_form(state)
        companies._emit_company_confirmation(state, company, companies._build_company_confirm_message(company))
        return True
    if pending in {'cnpj_or_retry', 'cnpj_knows'}:
        choice = values.get('cnpj_or_retry_choice') or values.get('cnpj_knows')
        if choice in {'provide_cnpj', 'yes'}:
            clear_form(state)
            companies._set_pending_form(state, 'cnpj_input', companies._build_cnpj_input_form())
            emit(state, 'Informe o CNPJ da empresa.')
        elif choice in {'retry_search', 'no', 'skip_cnpj'}:
            clear_form(state)
            companies._set_pending_form(state, 'company_refine', companies._build_company_refine_form())
            emit(state, 'Informe outra referência de localização para identificar a empresa.')
        else:
            emit(state, 'Selecione uma opção no formulário.')
        return True
    if pending == 'company_refine':
        location = str(values.get('company_location') or '').strip()
        if not location:
            emit(state, 'Informe a cidade, shopping, bairro ou endereço da empresa.')
            return True
        previous = str(state.get('company_location_input') or '').strip()
        if state.pop('company_city_needed', None) and previous and previous.lower() not in location.lower():
            from src.services.company_search_pipeline import resolve_locality
            answer = resolve_locality(location, exclude=state.get('opposing_party_name') or '')
            if answer.city or answer.uf:
                # A pergunta foi so a cidade: manter o shopping/bairro ja informado.
                location = f'{previous}, {location}'
        state['company_location_input'] = location
        state.setdefault('staged_intake', {})['location_hint'] = location
        state['company_segment'] = str(values.get('company_segment') or '').strip()
        clear_form(state)
        await companies._search_via_firecrawl(state)
        # Sede registrada direto (compra online): o turno continua para os anexos.
        return not (state.get('company_confirmed') and not state.get('pending_form'))
    # Old sessions cannot accidentally accept an unrelated/stale form.
    clear_form(state)
    emit(state, 'Vamos retomar a identificação da empresa. Informe o nome e a localização.')
    return True


async def run_intake(state, companies):
    state.setdefault('messages', [])
    state.setdefault('step', 0)
    state['current_agent'] = 'intake'
    state['ready_for_classification'] = False
    state['validated'] = False
    companies._remember_intake_input(state)
    if (state.get('company_confirmed') and state.get('company_parent_lookup_pending')
            and state.get('opposing_party_filial_cnpj')
            and int(state.get('company_parent_lookup_tries') or 0) < 3):
        await link_parent_company(state, companies)
    pending = state.get('pending_form')
    # Aceita o envio estruturado e o envio em texto do renderer legado
    # ("Qual dessas unidades é a correta?: cmp_..."), sempre restrito ao form pendente.
    from src.services.dynamic_form_bridge import extract_dynamic_form_response
    typed = companies._latest_user_message_text(state)
    response = extract_dynamic_form_response(state, state.get('incoming_messagedata'), typed) or {}
    if not isinstance(response.get('values'), dict):
        response = {**response, 'values': {}} if response else response
    if pending and response.get('form_name') == pending:
        # Etapas que leem o envio direto do state (dados pessoais) recebem a
        # resposta ja normalizada, venha ela estruturada ou em texto.
        state['incoming_messagedata'] = {'dynamic_form_response': response}
    if pending and response.get('form_name') != pending:
        free_text = bool(typed) and not typed.startswith('[') and not response
        # Sem conteudo do cliente: so o formato do envio, para diagnosticar o transporte.
        print(f"[INTAKE] resposta nao casou com o form pendente={pending} "
              f"form_recebido={response.get('form_name')!r} campos={sorted(response.get('values') or {})} "
              f"texto_livre={free_text} tentativas={int(state.get('pending_form_misses') or 0) + 1}")
        if pending in {'company_select', 'company_confirm'} and free_text:
            # "não é nenhuma dessas" / "não, é a do shopping X" em texto: recusa (+ referencia).
            from src.services.company_search_ladder import escalate_company_search, rejection_remainder
            remainder = rejection_remainder(typed)
            if remainder is not None:
                await escalate_company_search(state, companies, remainder)
                return state
        if pending in COMPANY_FORMS and free_text and int(state.get('pending_form_misses') or 0) >= 1:
            # Segunda resposta em texto: o cliente esta explicando, nao escolhendo.
            release_company_form(state)
            pending = None
        elif pending == 'client_details':
            intake_gate(state)  # reemite o formulario de dados pessoais
            return state
        else:
            return reoffer_form(state, companies, 'Para continuar, responda pelo formulário abaixo.'
                                if pending not in {'company_select', 'company_confirm'} else
                                'Para continuar, selecione a unidade na lista abaixo. Se nenhuma for a correta, '
                                'escolha "Nenhuma dessas" ou me diga em qual cidade e local fica a unidade.',
                                count=free_text)  # anexo ou envio ilegivel nao conta como explicacao
    if pending:
        values = response.get('values') or {}
        if pending == 'case_confirmation':
            choice = values.get('case_confirmed')
            if choice not in {'yes', 'no'}:
                return emit(state, 'Confirme se o relato está correto no formulário.')
            clear_form(state)
            state['case_confirmed'] = choice == 'yes'
            if choice == 'no':
                state['case_correction_pending'] = True
                return emit(state, 'Qual ponto do relato precisa corrigir?')
        elif pending == 'client_details':
            if intake_gate(state):
                return state
        elif await handle_company_form(state, companies, pending, values):
            return state
    elif response:
        # Duplicate delivery of an already-consumed form must not become narrative.
        state['incoming_messagedata'] = None
    elif state.get('evidence_conflict_asked') and not (state.get('files') or state.get('audio')):
        from src.services.document_case_understanding import record_conflict_explanation
        from src.services.case_evidence import persist_case_evidence
        record_conflict_explanation(state, companies._latest_user_message_text(state))
        await persist_case_evidence(state)
        state['evidence_conflict_asked'] = False
    elif state.get('company_awaiting_reference') and not state.get('company_confirmed'):
        # Resposta ao pedido de referencia (CNPJ, site, endereco, foto da nota...).
        from src.services.company_search_ladder import handle_company_reference
        await handle_company_reference(state, companies)
        return state
    elif not state.get('company_confirmed') or not state.get('staged_intake') or state.get('case_correction_pending'):
        state['case_confirmed'] = False
        intake = flow.IntakeState.model_validate(state.get('staged_intake') or {})
        existing_problem = state.get('problem_description') or intake.problem_description
        intake.problem_description = existing_problem
        if existing_problem:
            state.setdefault('problem_description_original', existing_problem)
            if not intake.case_summary:
                intake.case_summary = state.get('case_summary') or existing_problem
        intake.company_name_hint = state.get('opposing_party_name') or intake.company_name_hint
        intake.location_hint = state.get('company_location_input') or intake.location_hint
        message = companies._latest_user_message_text(state)
        recent = [{'role': m['role'], 'content': str(m.get('content', ''))[-2000:]}
                  for m in state['messages'][-7:-1] if m.get('role') in {'user', 'assistant'}]
        try:
            from src.services.intake_progress import MSG_BEFORE_LLM, send_progress
            await send_progress(state, MSG_BEFORE_LLM)
            updated, reply = await flow.understand_turn(intake, message, recent)
        except Exception:
            return emit(state, 'Seu relato foi mantido, mas não consegui concluir a leitura agora. Tente novamente em instantes.')
        normalize_loss_amount(updated, message)
        state['staged_intake'] = updated.model_dump(mode='json')
        for source, target in [('company_name_hint', 'opposing_party_name'), ('location_hint', 'company_location_input')]:
            if getattr(updated, source):
                state[target] = getattr(updated, source)
        if updated.problem_description:
            current_problem = state.get('problem_description')
            if not current_problem:
                state['problem_description'] = updated.problem_description
                state.setdefault('problem_description_original', updated.problem_description)
            elif not state.get('problem_description_original'):
                state['problem_description_original'] = message
                state['problem_description'] = message
            elif updated.problem_description != current_problem:
                await companies._merge_descriptions(state)
        state['case_summary'] = updated.case_summary or state.get('case_summary') or state.get('problem_description')
        state['intake_temporal_context'] = temporal_prompt(state)
        state['case_correction_pending'] = False
        if flow.narrative_missing(updated):
            from src.open_dots.memory import next_missing_question
            return emit(state, next_missing_question(state, updated, reply,
                missing_question(flow.narrative_missing(updated)[0])))

    essential = flow.IntakeState.model_validate(state.get('staged_intake') or {})
    missing = flow.narrative_missing(essential)
    if missing:
        state['case_correction_pending'] = True
        return emit(state, missing_question(missing[0]))
    reference = state.get('intake_reference_timestamp')
    dates = resolve_date_mentions(essential.incident_date_raw or '', datetime.fromisoformat(reference) if reference else None)
    if any(d['status'] in {'invalid', 'needs_confirmation'} for d in dates):
        state['case_correction_pending'] = True
        return emit(state, 'Qual dia ou período você quis dizer? Preciso confirmar a data.')
    if not state.get('company_confirmed'):
        if not state.get('opposing_party_name') or not state.get('company_location_input'):
            return emit(state, 'Qual é o nome da empresa?' if not state.get('opposing_party_name') else 'Onde aconteceu? Informe cidade, shopping, bairro ou site.')
        from src.services.airport_check import airport_notice
        notice = airport_notice(state)
        if notice:
            return emit(state, notice)
        await companies._search_via_firecrawl(state)
        if not state.get('company_confirmed') or state.get('pending_form'):
            return state
        # Compra online com uma unica sede encontrada: empresa ja registrada, segue o fluxo.
    if not attachment_gate(state):
        blocked = attachments_blocked_message(state)
        if blocked:
            # Anexo bloqueado por seguranca, em processamento ou sem registro:
            # explicar o motivo em vez de repetir o pedido inicial.
            state['documents_requested'] = True
            state['evidence_receipt_consumed'] = True
            return emit(state, blocked)
        if state.get('documents_requested'):
            nags = (
                'Ainda preciso de pelo menos um documento do caso para seguir: nota fiscal, laudo, comprovante, contrato, fotos ou áudio. Pode anexar pelo botão de anexos?',
                'Sem anexo fica difícil continuar. Quando puder, manda uma nota, comprovante, foto ou áudio do caso.',
                'Pode enviar pelo menos um documento (nota, laudo, contrato, foto ou áudio)? É só usar o botão de anexos.',
            )
            count = int(state.get('documents_nag_count') or 0)
            state['documents_nag_count'] = count + 1
            return emit(state, nags[count % len(nags)])
        state['documents_requested'] = True
        state['documents_nag_count'] = 0
        opening = 'Pronto, identifiquei a empresa.'
        if state.pop('company_auto_selected', None):
            # Nao houve escolha do cliente: dizer qual empresa foi registrada.
            opening = f"Como a compra foi online, registrei a sede da empresa: {state.get('opposing_party_name')}."
        return emit(state, opening + ' Agora me envie os documentos do caso: nota fiscal, laudo, comprovante, contrato, fotos ou áudio. Vou registrar os anexos, extrair o conteúdo e organizar um resumo de cada um junto ao seu relato.')
    if state.get('evidence_conflict_pending'):
        from src.services.document_case_understanding import client_inventory_text
        state['evidence_conflict_asked'] = True
        state['evidence_receipt_consumed'] = True
        return emit(state, client_inventory_text(state))
    if client_details_required() and (not state.get('client_details_confirmed') or not valid_personal(state)):
        intake_gate(state)
        return state
    save_record(state)
    if not state.get('case_confirmed'):
        companies._set_pending_form(state, 'case_confirmation', {
            'type': 'form', 'mode': 'editable', 'form_name': 'case_confirmation',
            'submit_label': 'Confirmar relato', 'fields': [{'name': 'case_confirmed', 'type': 'select',
            'label': 'O relato está correto?', 'required': True, 'options': [
                {'value': 'yes', 'label': 'Sim'}, {'value': 'no', 'label': 'Quero corrigir'}]}]})
        from src.services.document_case_understanding import client_inventory_text
        summary = state.get('case_summary') or state.get('problem_description') or ''
        return emit(state, summary + '\n\n' + client_inventory_text(state) + '\n\nEstá correto?')
    state['ready_for_classification'] = state['validated'] = True
    save_record(state)
    return state


def missing_question(field):
    return {'problem_description': 'O que aconteceu?',
            'loss_amount': 'Você teve algum gasto ou prejuízo? Pode informar uma estimativa ou dizer que ainda não sabe.',
            'incident_date': 'Quando aconteceu? Pode ser uma data ou período aproximado.'}[field]
