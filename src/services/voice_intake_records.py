"""Registro local de contatos de voz; nao representa cadastro autenticado ou CRM comercial."""
import json
import sqlite3
from pathlib import Path
from config import get_settings


def save_voice_intake(data):
    root = Path(get_settings().DOCUMENTS_PATH) / '.voice_intake'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {
        'conversationId': data['conversationId'],
        'firstName': data.get('firstName') or '',
        'lastName': data.get('lastName') or '',
        'phone': data.get('phone') or data.get('callerPhone') or '',
        'email': data.get('email') or '',
        'summary': data.get('summary') or '',
        'transcription': data.get('transcricao') or [],
        'userId': data.get('userId'),
        'registrationStatus': 'authenticated' if data.get('userId') else 'pending_registration',
        'source': 'elevenlabs',
    }
    with sqlite3.connect(root / 'records.sqlite3', timeout=10) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS voice_intake (conversation_id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        conn.execute('INSERT OR REPLACE INTO voice_intake VALUES (?, ?)',
                     (data['conversationId'], json.dumps(record, ensure_ascii=False)))
    return {'status': 'saved', 'conversationId': data['conversationId']}
