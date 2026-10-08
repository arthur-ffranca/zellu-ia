"""Shared case gates and legal candidate routing; no conversational agent dependency."""
import math
from src.services import staged_intake_flow as flow

def security_blocked_record(record):
    """Arquivo que nao pode entrar no caso: malware ou varredura obrigatoria sem resposta."""
    from src.services.document_audit import security_blocked
    audit = record.get('audit') or {}
    return bool(record.get('securityBlocked')
                or str(record.get('note') or '') in {'malware_detected', 'security_scan_required'}
                or (audit and security_blocked(audit))
                or str(audit.get('status', '')).upper() in {'BLOCKED', 'MALICIOUS'}
                or 'MALWARE_DETECTED' in (audit.get('securityFlags') or []))


def attachment_gate(state):
    """O cliente entregou anexo e o fluxo pode seguir.

    Anexo recebido conta mesmo sem leitura: falha de download ou de visao e
    problema do sistema, e uma foto ilegivel continua sendo prova que uma pessoa
    pode abrir. O que nao foi lido vira aviso (unread_notice), nao bloqueio.
    So nao contam arquivo bloqueado por seguranca e anexo ainda em processamento.
    """
    records = state.get('case_evidence') or []
    if state.get('evidence_storage_error') or not records:
        return False
    if any(str(r.get('status', '')).lower() in {'processing', 'pending'} or
           str((r.get('audit') or {}).get('status', '')).upper() in {'PROCESSING', 'PENDING'} for r in records):
        return False
    return any(not security_blocked_record(r) for r in records)


# Por que um anexo recebido nao pode ser usado. O codigo vai na mensagem ao
# cliente (sem rotulo interno) para o suporte achar a causa sem abrir o log.
_UNREAD_REASONS = {
    # nosso lado: o servico nao conseguiu abrir o arquivo no storage
    'storage_host_not_allowed': ('acesso', 'A1'), 'download_access_denied': ('acesso', 'A2'),
    'download_not_found': ('acesso', 'A3'), 'download_returned_html': ('acesso', 'A4'),
    'download_not_ok': ('acesso', 'A5'), 'falha_no_download_armazenamento_ou_leitura': ('acesso', 'A9'),
    # nosso lado: abriu, mas a leitura (visao/transcricao) nao devolveu conteudo
    'visao_desligada': ('leitura', 'L1'), 'visao_timeout': ('leitura', 'L2'),
    'transcricao_timeout': ('leitura', 'L4'), 'falha_na_leitura': ('leitura', 'L9'),
    # lado do cliente: outro arquivo resolve
    'pdf_password_required': ('senha', 'C1'), 'pdf_read_error': ('arquivo', 'C2'),
    'file_too_large': ('tamanho', 'C3'), 'empty_file': ('arquivo', 'C4'),
    'formato_nao_suportado': ('formato', 'C5'), 'imagem_excede_limite_de_visao': ('tamanho', 'C6'),
    'turn_file_limit': ('quantidade', 'C7'),
    'malware_detected': ('seguranca', 'S1'), 'security_scan_required': ('seguranca', 'S2'),
}


def unread_reason(record):
    """(categoria, codigo) do motivo pelo qual o anexo nao foi lido."""
    note = str(record.get('note') or '').split(';')[0].strip()
    if note in _UNREAD_REASONS:
        return _UNREAD_REASONS[note]
    if record.get('securityBlocked'):
        return 'seguranca', 'S2'
    # Abriu e a leitura voltou vazia (foto ilegivel, PDF so com imagem, visao sem resposta).
    return ('leitura', 'L3') if record.get('original_stored') else ('acesso', 'A9')


_CLIENT_HINTS = {
    'senha': 'o PDF está protegido por senha; envie uma versão sem senha',
    'tamanho': 'o arquivo passa do tamanho permitido; envie uma versão menor ou uma foto',
    'formato': 'o formato não é aceito; envie em PDF, DOCX, imagem ou áudio',
    'quantidade': 'foram enviados arquivos demais de uma vez; envie em partes',
    'arquivo': 'o arquivo parece vazio ou corrompido; envie novamente ou tire uma foto do documento',
}


def unread_notice(state, only_ids=None):
    """Aviso (nao bloqueio) sobre anexos recebidos que nao puderam ser lidos.

    Devolve None quando tudo foi lido. `message` vai no texto do chat; o dict
    inteiro vai em messagedata.evidenceUpload.notice para o front destacar.
    """
    records = [r for r in state.get('case_evidence') or []
               if r.get('status') not in {'read', 'partial', 'processing', 'pending'}
               and not security_blocked_record(r)
               and (only_ids is None or r.get('id') in only_ids)]
    if not records:
        return None
    reasons = [unread_reason(r) for r in records]
    categories = {category for category, _ in reasons}
    codes = sorted({code for _, code in reasons})
    count = len(records)
    subject = f"{count} anexo{'s' if count != 1 else ''}"
    kept = 'ficou registrado' if count == 1 else 'ficaram registrados'
    if categories <= {'acesso', 'leitura'}:
        message = (f"Aviso: não consegui ler {subject}. O problema é do nosso lado, não do arquivo; "
                   f"{kept} no seu caso e você pode seguir normalmente. (código {', '.join(codes)})")
    else:
        hint = '; '.join(_CLIENT_HINTS[c] for c in sorted(categories) if c in _CLIENT_HINTS)
        message = (f"Aviso: não consegui ler {subject} ({hint}). {kept.capitalize()} no seu caso e você pode "
                   f"seguir; se quiser que o conteúdo entre no resumo, envie de novo ajustado. "
                   f"(código {', '.join(codes)})")
    names = ', '.join(str(r.get('name') or 'arquivo') for r in records)
    return {'level': 'warning', 'codes': codes, 'message': message,
            # Texto para a notificacao dentro do caso ja aberto (pedido de reenvio).
            'action': 'resend_documents',
            'caseMessage': (f"{subject.capitalize()} não {'pôde ser lido' if count == 1 else 'puderam ser lidos'} "
                            f"na abertura do caso: {names}. Reenvie para que o conteúdo entre na análise."),
            'files': [{'id': r.get('id'), 'name': r.get('name'), 'code': code}
                      for r, (_, code) in zip(records, reasons)]}


def attachments_blocked_message(state):
    """O que dizer quando ha anexos recebidos e o fluxo ainda nao pode seguir.

    Vazio quando ainda nao chegou anexo (vale o pedido normal de documentos).
    """
    records = state.get('case_evidence') or []
    if not records:
        return ''
    if state.get('evidence_storage_error'):
        return ('Recebi seus anexos, mas não consegui confirmar o registro deles no sistema. '
                'O problema é do nosso lado. Tente enviar novamente em alguns minutos. (código R1)')
    if any(str(r.get('status', '')).lower() in {'processing', 'pending'} for r in records):
        return 'Seus anexos ainda estão sendo processados. Aguarde um instante e envie uma mensagem para continuar.'
    codes = ', '.join(sorted({unread_reason(r)[1] for r in records}))
    total = len(records)
    return (f"Recebi {total} anexo{'s' if total != 1 else ''}, mas não posso usar esse arquivo por segurança. "
            f"Envie outro documento do caso, em PDF, imagem ou áudio. (código {codes})")


def emit(state, message):
    state.setdefault('messages', []).append({'role': 'assistant', 'content': message, 'agent': 'intake'})
    state['step'] = state.get('step', 0) + 1
    state['ready_for_classification'] = False
    state['validated'] = False
    return state

async def route_legal_candidates(state, docs):
    """Select only among retrieved legislation. No indexing/upload here."""
    if not state.get('case_confirmed') or not attachment_gate(state):
        raise ValueError('Legal routing requires confirmed case and audited evidence')
    candidates = []
    for index, doc in enumerate(docs):
        metadata = doc.get('metadata') or {}
        text = doc.get('content') or flow.metadata_text(metadata)
        if text:
            candidates.append({'article_id': str(doc.get('id') or metadata.get('article_id') or f'C{index}'),
                               'title': flow.metadata_title(metadata), 'text': text})
    if not candidates:
        print('[ANALYST] Busca sem texto de lei; analise segue sem artigo')
        state['legal_decision'] = {'source': 'NONE', 'reason': 'sem texto de lei nos documentos recuperados'}
        return docs
    intake = flow.IntakeState.model_validate(state.get('staged_intake') or {})
    intake.problem_description = state.get('case_summary') or state.get('problem_description')
    try:
        decision = await flow.choose_with_jev(intake, candidates)
    except Exception as exc:
        print(f'[ANALYST] JEV nao escolheu ({type(exc).__name__}: {exc}); usando GPT')
        chosen = await flow.choose_with_gpt5(intake, candidates)
        decision = {'chosen_article_id': chosen, 'confidence': 0.0, 'margin': 0.0, 'source': 'GPT5_FALLBACK'}
        state['legal_decision'] = decision
        selected = next(i for i, c in enumerate(candidates) if c['article_id'] == chosen)
        nonempty = [d for d in docs if d.get('content') or flow.metadata_text(d.get('metadata') or {})]
        return [nonempty[selected]]
    confidence, margin = decision['confidence'], decision['margin']
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in (confidence, margin)):
        raise ValueError('Invalid JEV confidence or margin')
    source = 'JEV'
    chosen = decision['chosen_article_id']
    if confidence < flow.JEV_CONFIDENCE_THRESHOLD or margin < flow.JEV_MARGIN_THRESHOLD:
        chosen = await flow.choose_with_gpt5(intake, candidates)
        source = 'GPT5_FALLBACK'
    if chosen not in {c['article_id'] for c in candidates}:
        raise ValueError('Choice outside retrieved candidates')
    state['legal_decision'] = {**decision, 'chosen_article_id': chosen, 'source': source}
    selected = next(i for i, c in enumerate(candidates) if c['article_id'] == chosen)
    nonempty = [d for d in docs if d.get('content') or flow.metadata_text(d.get('metadata') or {})]
    return [nonempty[selected]]
