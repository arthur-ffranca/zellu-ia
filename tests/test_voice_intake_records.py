import json
import sqlite3
from types import SimpleNamespace
from src.services import voice_intake_records as records


def test_contact_saved_and_replayed_without_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(records, 'get_settings', lambda: SimpleNamespace(DOCUMENTS_PATH=str(tmp_path)))
    data = dict(conversationId='test-call', firstName='Teste', lastName='Pessoa',
                phone='+5519999999999', email='teste@example.test', summary='Relato de teste',
                transcricao=[{'role': 'user', 'text': 'Relato de teste'}])
    records.save_voice_intake(data)
    records.save_voice_intake(data)
    with sqlite3.connect(tmp_path / '.voice_intake' / 'records.sqlite3') as conn:
        rows = conn.execute('SELECT record FROM voice_intake').fetchall()
    assert len(rows) == 1
    saved = json.loads(rows[0][0])
    assert saved['email'] == data['email']
    assert saved['transcription'] == data['transcricao']
    assert saved['registrationStatus'] == 'pending_registration'
    assert saved['userId'] is None
