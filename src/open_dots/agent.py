"""The principal chat agent. Open Dots governs all case operations.

No old orchestrator, inherited intake agent or framework fallback is reachable.
The Zellu state manager remains the owner of per-chat persistence and locking.
"""
from src.open_dots.vendor.action_gateway import ActionDefinition, ActionGateway, ActionInvocation
from src.open_dots.intake import run_intake
from src.services.company_intake import CompanyIntakeService
from src.services.case_classification import CaseClassification
from src.services.legal_analysis import LegalAnalysis
from src.services.case_documents import CaseDocuments
from src.services.service_context import case_evidence_scope, set_agent_usage_context
from src.services.ai_usage_tracker import new_event_id
from src.utils.step_timing import timed


class CaseAudit:
    def __init__(self, state):
        self.state = state

    def add_audit_event(self, event):
        # Case history stores lifecycle only, never the document text/credentials.
        events = self.state.setdefault('dot_actions', [])
        events.append({key: event.get(key) for key in
                       ('event', 'request_id', 'tool', 'action', 'state', 'created_at')})
        del events[:-100]


class DotAgent:
    def __init__(self, llm, rag_system, llm_fast=None, minio_service=None,
                 company_search_client=None, email_validation_client=None,
                 pre_registration_client=None):
        self.llm = llm
        self.llm_fast = llm_fast or llm
        self.company_search_client = company_search_client
        self.classification = CaseClassification(self.llm_fast)
        self.analysis = LegalAnalysis(llm, rag_system)
        self.documents = CaseDocuments(llm, minio_service)

    async def process(self, state):
        from src.services.case_evidence import ingest_case_evidence, apply_audio_message
        state.setdefault('messages', [])
        state.setdefault('step', 0)
        state['chat_runtime'] = 'open_dots'
        state['event_id'] = state.get('event_id') or new_event_id()
        set_agent_usage_context(event_id=state['event_id'], event_type='ticket_open',
                                session_id=state.get('chat_id'), user_id=state.get('user_id'))
        gateway = ActionGateway(audit=CaseAudit(state))
        companies = CompanyIntakeService(self.llm_fast, self.company_search_client)

        async def evidence(_):
            # Intake leve: so registra/restaura; leitura pesada na etapa de anexos.
            # Docstring de ingest_case_evidence: include_files=False no intake.
            staged = (state.get('staged_intake') or {}).get('stage')
            at_attachments = bool(
                state.get('files') or state.get('received_case_files')
                or state.get('documents_requested')
                or staged == 'ATTACHMENTS'
            )
            await ingest_case_evidence(state, include_files=at_attachments)
            apply_audio_message(state)
            return {'files': len(state.get('case_evidence') or [])}

        async def intake(_):
            await run_intake(state, companies)
            return {'ready': bool(state.get('ready_for_classification'))}

        async def classify(_):
            await self.classification.process(state)
            return {'category': state.get('legal_category')}

        async def analyze(_):
            await self.analysis.process(state)
            return {'analyzed': bool(state.get('suggested_next_steps'))}

        async def documents(_):
            result = await self.documents.process(state)
            if result.get('completed') is False:
                raise RuntimeError('Case document generation did not complete')
            state.update(result.get('update_state', {}))
            return {'generated': sorted((state.get('generated_documents') or {}).keys())}

        for action, executor in [('evidence', evidence), ('intake', intake),
                                 ('classify', classify), ('analyze', analyze), ('documents', documents)]:
            gateway.register_action(ActionDefinition(
                name='case.' + action, tool='case', action=action,
                intent='Process the current authorized case: ' + action,
                risk='write', requires_approval=False), executor)

        async def dispatch(action):
            request, _ = gateway.open(str(state.get('chat_id')), 'zellu-intake', ActionInvocation(
                name='case.' + action, arguments={}, target={'case': str(state.get('chat_id'))}, preview=action))
            with timed('case.' + action, chat_id=state.get('chat_id')):
                result = await gateway.execute(request)
            if result.status != 'completed':
                state['completed'] = False
                state['dot_failed_action'] = action
                detail = getattr(result, 'error', None) or ''
                print(f'[CASE] acao {action} falhou: {detail}')
                raise RuntimeError('Open Dots case action failed: ' + action + ((': ' + detail) if detail else ''))

        await dispatch('evidence')
        with case_evidence_scope(state):
            await dispatch('intake')
            if not self.ready(state):
                return state
            state['completed'] = False
            # Invert fake delay: avisa ANTES de classify/analyze/documents.
            from src.services.intake_progress import MSG_ANALYZING, send_progress
            await send_progress(state, MSG_ANALYZING)
            await dispatch('classify')
            await dispatch('analyze')
            await dispatch('documents')
        state['completed'] = True
        state['current_agent'] = 'dot'
        state.pop('dot_failed_action', None)
        return state

    @staticmethod
    def ready(state):
        from src.services.staged_intake_adapter import attachment_gate
        from src.services.client_intake_record import client_details_required, valid_personal
        return bool(state.get('ready_for_classification') and state.get('validated')
                    and state.get('case_confirmed') and state.get('company_confirmed')
                    and not state.get('pending_form')
                    and (valid_personal(state) or not client_details_required())
                    and attachment_gate(state))

    def get_agent_status(self, state):
        return {'runtime': 'open_dots', 'current_agent': state.get('current_agent', 'intake'),
                'step': state.get('step', 0), 'completed': bool(state.get('completed')),
                'validated': bool(state.get('validated')),
                'ready_for_classification': self.ready(state)}
