import asyncio
from types import SimpleNamespace

from src.services import document_jev_validator as jev
from src.services import document_fidelity as fidelity


def _sources():
    return {
        "document_type": "agreement",
        "lawyer_instructions": "Inclua uma cláusula chamada 'CLÁUSULA 2ª – PAGAMENTO'.",
        "template_reference": "Modelo de acordo",
        "confirmed_case": {"description": "Pagamento de R$ 1.000,00."},
    }


def _document():
    return {
        "document_title": "ACORDO",
        "location_date": "[LOCAL A DEFINIR]",
        "sections": [{"title": "CLÁUSULA 1ª – OBJETO", "content": "<p>Objeto.</p>"}],
        "signatures": [],
        "summary": "Acordo",
        "case_summary": "Caso",
    }


def test_parse_decisive_fail_becomes_repair_issue():
    result = jev._parse_answers(
        {
            "answers": {
                "required_content": {
                    "choice": "FAIL",
                    "confidence": 0.96,
                    "probabilities": {"PASS": 0.02, "FAIL": 0.96, "UNCERTAIN": 0.02},
                }
            }
        },
        ["required_content"],
        confidence_threshold=0.65,
        margin_threshold=0.40,
    )
    assert [i["rule"] for i in result["issues"]] == ["jev_required_content"]
    assert result["checks"]["required_content"]["decisive"] is True


def test_low_margin_fail_does_not_trigger_repair():
    result = jev._parse_answers(
        {
            "answers": {
                "required_content": {
                    "choice": "FAIL",
                    "confidence": 0.55,
                    "probabilities": {"PASS": 0.40, "FAIL": 0.55, "UNCERTAIN": 0.05},
                }
            }
        },
        ["required_content"],
        confidence_threshold=0.65,
        margin_threshold=0.40,
    )
    assert result["issues"] == []
    assert result["checks"]["required_content"]["decisive"] is False


def test_provider_error_fails_open(monkeypatch):
    monkeypatch.setenv("DOCUMENT_JEV_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "test-key")

    class BrokenClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, *args, **kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(jev.httpx, "AsyncClient", lambda *args, **kwargs: BrokenClient())
    result = asyncio.run(jev.validate_document(sources=_sources(), parsed=_document()))
    assert result["available"] is False
    assert result["reason"] == "provider_error"
    assert result["issues"] == []


def test_validate_document_high_confidence_fail(monkeypatch):
    monkeypatch.setenv("DOCUMENT_JEV_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.setenv("DOCUMENT_JEV_CONFIDENCE_THRESHOLD", "0.65")
    monkeypatch.setenv("DOCUMENT_JEV_MARGIN_THRESHOLD", "0.40")

    answers = {}
    for name in jev._questions(_sources()):
        choice = "FAIL" if name == "required_content" else "PASS"
        answers[name] = {
            "choice": choice,
            "confidence": 0.95,
            "probabilities": {
                "PASS": 0.95 if choice == "PASS" else 0.03,
                "FAIL": 0.95 if choice == "FAIL" else 0.03,
                "UNCERTAIN": 0.02,
            },
        }

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {"model": "jev-test", "answers": answers}

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(jev.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    result = asyncio.run(jev.validate_document(sources=_sources(), parsed=_document()))
    assert result["available"] is True
    assert result["model"] == "jev-test"
    assert [i["rule"] for i in result["issues"]] == ["jev_required_content"]


def test_decisive_jev_fail_is_delivered_as_point_to_fix_not_blocked():
    issue = {
        "rule": "jev_required_content",
        "source_quote": "Regra obrigatória",
        "output_quote": "",
        "reason": "Conteúdo obrigatório ausente",
    }
    assert fidelity.blocking([issue], "fail") is False


def test_real_world_fail_probability_beats_generic_confidence():
    """Regression for 06/10: confidence=0.66, but JEV returned P(FAIL)=0.78."""
    result = jev._parse_answers(
        {
            "answers": {
                "type_rules": {
                    "choice": "FAIL",
                    "confidence": 0.66,
                    "probabilities": {"FAIL": 0.78, "UNCERTAIN": 0.0, "PASS": 0.22},
                }
            }
        },
        ["type_rules"],
        confidence_threshold=0.65,
        margin_threshold=0.40,
    )
    assert [i["rule"] for i in result["issues"]] == ["jev_type_rules"]
    check = result["checks"]["type_rules"]
    assert check["decisive"] is True
    assert check["fail_probability"] == 0.78
    assert check["confidence"] == 0.66


def test_source_fidelity_question_is_always_present():
    questions = jev._questions(_sources())
    assert "source_fidelity" in questions
    instructions = questions["source_fidelity"]["instructions"]
    assert "ressarcimento" in instructions
    assert "dado pessoal" in instructions
    assert "analysis_suggestions_not_confirmed_facts" in instructions


def test_source_fidelity_fail_becomes_repair_issue():
    result = jev._parse_answers(
        {
            "answers": {
                "source_fidelity": {
                    "choice": "FAIL",
                    "confidence": 0.61,
                    "probabilities": {"FAIL": 0.72, "PASS": 0.20, "UNCERTAIN": 0.08},
                }
            }
        },
        ["source_fidelity"],
        confidence_threshold=0.65,
        margin_threshold=0.40,
    )
    assert [i["rule"] for i in result["issues"]] == ["jev_source_fidelity"]
    assert fidelity.blocking(result["issues"], "fail") is False


def test_weak_fail_stays_non_decisive():
    result = jev._parse_answers(
        {
            "answers": {
                "source_fidelity": {
                    "choice": "FAIL",
                    "confidence": 0.80,
                    "probabilities": {"FAIL": 0.60, "PASS": 0.35, "UNCERTAIN": 0.05},
                }
            }
        },
        ["source_fidelity"],
        confidence_threshold=0.65,
        margin_threshold=0.40,
    )
    assert result["issues"] == []
    assert result["checks"]["source_fidelity"]["decisive"] is False


def test_personal_data_is_pseudonymized_consistently(monkeypatch):
    monkeypatch.setenv("DOCUMENT_JEV_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    sent = {}

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {"answers": {}}

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, *args, **kwargs):
            sent.update(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(jev.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    sources = dict(_sources(), client={"name": "Felipe", "cpf": "123.456.789-09", "email": "a@b.com"})
    document = dict(_document(), signatures=["Felipe - Contratante - CPF 123.456.789-09"])
    result = asyncio.run(jev.validate_document(sources=sources, parsed=document))
    payload = str(sent["state"])
    assert "123.456.789-09" not in payload and "a@b.com" not in payload
    alias = sent["state"]["sources"]["client"]["cpf"]
    assert alias in sent["state"]["document"]["signatures"][0]
    assert result["checks"]["required_content"] == {"choice": "MISSING", "decisive": False}
