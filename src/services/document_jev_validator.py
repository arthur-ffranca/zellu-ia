"""JEV semantic validation for Writer documents.

This module is an *extra* fidelity layer. It never generates document text.
It evaluates whether the rendered draft respected the same rules already used by
``document_fidelity`` and returns structured issues that the Writer's existing
repair loop can feed back to the drafting model.

Design goals:
- one source of truth for rules (``document_fidelity``);
- one JEV request with multiple typed questions;
- fail-open on infrastructure/API errors, because the current LLM reviewer remains
  the authoritative fallback during rollout;
- FAIL gating is asymmetric and based on the returned FAIL probability + margin,
  not on the provider's generic ``confidence`` field;
- semantic source-fidelity checks explicitly catch unsupported facts, values,
  obligations, remedies, deadlines, purposes and personal data;
- no retry loop beyond the Writer's existing ``FIDELITY_ATTEMPTS``;
- decisive FAILs trigger the repair and, if they persist, are delivered as
  "pontos a corrigir" (they do not block: a blocked document reaches nobody);
- personal data is pseudonymized before leaving for the provider (LGPD).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable

import httpx

from config import get_settings
from src.services import document_fidelity as fidelity


_PASS = "PASS"
_FAIL = "FAIL"
_UNCERTAIN = "UNCERTAIN"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on", "sim"}


def _api_key() -> str:
    settings = get_settings()
    return (
        os.getenv("JEV_API_KEY")
        or getattr(settings, "JEV_API_KEY", "")
        or os.getenv("zellu-typesafe-ia")
        or ""
    ).strip()


def _margin(probabilities: Dict[str, float]) -> float:
    values = sorted((float(v) for v in probabilities.values()), reverse=True)
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return values[0] - values[1]


def _compact(value: Any, *, limit: int = 24000) -> Any:
    """Bound very large free-text source fields without changing structured data.

    JEV is a decision layer, not the primary document reader. The Writer's normal
    reviewer still receives the complete sources. For the extra JEV pass we retain
    both the beginning and end of oversized text so a giant OCR payload cannot make
    this safety check dominate latency/cost.
    """
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        half = max(1, (limit - 80) // 2)
        return value[:half] + "\n...[TRECHO INTERMEDIARIO OMITIDO PARA A CONFERENCIA JEV]...\n" + value[-half:]
    if isinstance(value, list):
        return [_compact(v, limit=limit) for v in value]
    if isinstance(value, dict):
        return {k: _compact(v, limit=limit) for k, v in value.items()}
    return value


# Dado pessoal nao sai para o provedor externo (LGPD). Cada valor vira um
# apelido estavel ("[CPF-1]"), o MESMO nas fontes e no documento, para que a
# pergunta source_fidelity continue comparando um com o outro.
_PII_PATTERNS = (
    ("CPF", re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("TELEFONE", re.compile(r"\(?\b\d{2}\)?\s?9?\d{4}-?\d{4}\b")),
    ("RG", re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-?[\dxX]\b")),
)
_PII_KEYS = re.compile(r"cpf|rg$|email|e-mail|phone|telefone|celular|address|endereco|endereço|cep|street|logradouro",
                       re.IGNORECASE)


class _Pseudonymizer:
    def __init__(self) -> None:
        self.aliases: Dict[str, str] = {}

    def _alias(self, kind: str, value: str) -> str:
        key = re.sub(r"\D", "", value) if kind in ("CPF", "TELEFONE", "RG") else value.casefold().strip()
        if key not in self.aliases:
            self.aliases[key] = f"[{kind}-{sum(1 for a in self.aliases.values() if a.startswith('[' + kind)) + 1}]"
        return self.aliases[key]

    def __init_literals(self) -> Dict[str, str]:
        if not hasattr(self, "_literals"):
            self._literals: Dict[str, str] = {}
        return self._literals

    def text(self, value: str) -> str:
        # Endereco etc. ja vistos nas fontes recebem o mesmo apelido no documento.
        for literal, alias in sorted(self.__init_literals().items(), key=lambda kv: -len(kv[0])):
            value = value.replace(literal, alias)
        for kind, pattern in _PII_PATTERNS:
            value = pattern.sub(lambda m, k=kind: self._alias(k, m.group(0)), value)
        return value

    def __call__(self, value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            masked = self.text(value)
            if key and _PII_KEYS.search(key) and masked.strip() and masked == value:
                alias = self._alias("DADO_PESSOAL", value)
                if len(value.strip()) >= 6:
                    self.__init_literals()[value.strip()] = alias
                return alias
            return masked
        if isinstance(value, list):
            return [self(v, key) for v in value]
        if isinstance(value, dict):
            return {k: self(v, str(k)) for k, v in value.items()}
        return value


def _question(instructions: str) -> Dict[str, Any]:
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {
            _PASS: (
                "O documento cumpre integralmente este critério com base nas regras e fontes fornecidas."
            ),
            _FAIL: (
                "Existe ao menos uma violação material deste critério que deve ser corrigida no documento."
            ),
            _UNCERTAIN: (
                "As fontes fornecidas não permitem decidir este critério com segurança suficiente."
            ),
        },
    }


def _questions(sources: Dict[str, Any]) -> Dict[str, Any]:
    document_type = str((sources or {}).get("document_type") or "")
    questions: Dict[str, Any] = {
        "required_content": _question(
            "Confira SOMENTE omissões obrigatórias. Considere o livro de regras, as instruções "
            "explícitas do advogado e o template apenas onde ele não foi sobrescrito pelo advogado. "
            "Marque FAIL se algum título, seção, frase, marcador, poder, obrigação ou outro conteúdo "
            "positivamente exigido estiver ausente. Não exija dado factual inexistente nas fontes; "
            "quando a regra manda usar marcador para dado ausente, o marcador satisfaz o requisito."
        ),
        "source_fidelity": _question(
            "Confira se TODO conteúdo material afirmado no documento tem suporte nas fontes confirmadas "
            "ou foi explicitamente autorizado nas lawyer_instructions. Marque FAIL se o documento "
            "acrescentar ou transformar em fato qualquer dado pessoal, data, valor, pagamento, promessa, "
            "finalidade, obrigação, prazo, garantia, condição, estratégia, remédio, ressarcimento, "
            "indenização, situação processual ou consequência que não esteja sustentada pelas fontes. "
            "analysis_suggestions_not_confirmed_facts e template_reference NÃO confirmam fatos por si só. "
            "Placeholders explícitos para dados ausentes são válidos. Não reprove linguagem jurídica "
            "meramente conectiva que não acrescente conteúdo material."
        ),
        "type_rules": _question(
            f"Confira as regras específicas do tipo documental '{document_type}'. Marque FAIL apenas "
            "se alguma regra de forma/conteúdo específica desse tipo, presente no livro de regras, "
            "não tiver sido respeitada. Não invente novas convenções jurídicas ou de estilo."
        ),
    }
    if fidelity.plain((sources or {}).get("lawyer_instructions")):
        questions["lawyer_precedence"] = _question(
            "Confira a precedência das instruções explícitas do advogado. Quando houver divergência "
            "entre essas instruções e template_reference, o documento deve seguir o advogado. Marque "
            "FAIL se o template tiver prevalecido indevidamente ou se uma instrução positiva do "
            "advogado tiver sido perdida."
        )
    return questions


_ISSUE_META = {
    "required_content": (
        "jev_required_content",
        "Regras obrigatórias / instruções positivas do advogado",
        "O JEV sinalizou conteúdo obrigatório ausente. Recompare o documento com as regras, "
        "lawyer_instructions e template_reference (respeitando a precedência do advogado) e inclua "
        "somente o que estiver efetivamente exigido e sustentado pelas fontes.",
    ),
    "source_fidelity": (
        "jev_source_fidelity",
        "Fatos e conteúdo material sustentados pelas fontes confirmadas",
        "O JEV sinalizou conteúdo material sem suporte nas fontes confirmadas. Remova ou substitua por "
        "placeholder qualquer fato, dado pessoal, valor, prazo, obrigação, garantia, finalidade, remédio "
        "ou consequência não sustentada; preserve apenas o que estiver confirmado ou explicitamente "
        "autorizado nas lawyer_instructions.",
    ),
    "type_rules": (
        "jev_type_rules",
        "Regras específicas do tipo documental",
        "O JEV sinalizou descumprimento de regra específica do tipo documental. Corrija apenas a "
        "estrutura/conteúdo exigido pelas TYPE_RULES aplicáveis, sem acrescentar fatos novos.",
    ),
    "lawyer_precedence": (
        "jev_lawyer_precedence",
        "Precedência da instrução do advogado sobre o template",
        "O JEV sinalizou que uma instrução explícita do advogado foi perdida ou que o template "
        "prevaleceu indevidamente. Repare o documento preservando a instrução do advogado.",
    ),
}


def _parse_answers(
    data: Dict[str, Any],
    question_names: Iterable[str],
    *,
    confidence_threshold: float,
    margin_threshold: float,
) -> Dict[str, Any]:
    answers = data.get("answers") or {}
    issues = []
    checks: Dict[str, Any] = {}

    for name in question_names:
        answer = answers.get(name) or {}
        if not answer:
            # Pergunta sem resposta nao pode sumir do log em silencio.
            checks[name] = {"choice": "MISSING", "decisive": False}
            continue
        choice = str(answer.get("choice") or "").upper().strip()
        probabilities = {
            str(k).upper(): float(v)
            for k, v in (answer.get("probabilities") or {}).items()
        }
        confidence = float(answer.get("confidence") or probabilities.get(choice) or 0.0)
        choice_probability = float(probabilities.get(choice, confidence) or 0.0)
        fail_probability = float(probabilities.get(_FAIL, confidence if choice == _FAIL else 0.0) or 0.0)
        pass_probability = float(probabilities.get(_PASS, confidence if choice == _PASS else 0.0) or 0.0)
        margin = _margin(probabilities)

        # A decisão de segurança usa a probabilidade da própria classe escolhida.
        # O campo genérico "confidence" do provedor é mantido apenas para observação:
        # no caso real de 06/10 ele veio 0.66 enquanto P(FAIL)=0.78.
        if choice == _FAIL:
            decisive = fail_probability >= confidence_threshold and margin >= margin_threshold
        elif choice == _PASS:
            decisive = (
                pass_probability >= max(confidence_threshold, 0.75)
                and margin >= max(margin_threshold, 0.50)
            )
        else:
            decisive = False

        checks[name] = {
            "choice": choice,
            "confidence": confidence,
            "choice_probability": choice_probability,
            "fail_probability": fail_probability,
            "pass_probability": pass_probability,
            "margin": margin,
            "decisive": decisive,
            "probabilities": probabilities,
        }

        if choice == _FAIL and decisive:
            rule, source_quote, reason = _ISSUE_META[name]
            # Se o provedor justificar, a justificativa vai junto: sem ela a
            # correcao recebe "ha algo errado" sem saber onde.
            why = next((str(answer[k]).strip() for k in ("reason", "reasoning", "explanation", "rationale", "evidence")
                        if isinstance(answer.get(k), str) and answer[k].strip()), "")
            issues.append(
                {
                    "rule": rule,
                    "source_quote": source_quote,
                    "output_quote": why[:400],
                    "reason": reason,
                }
            )

    return {"issues": issues, "checks": checks}


async def validate_document(
    *,
    sources: Dict[str, Any],
    parsed: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the extra JEV fidelity pass.

    Returns a stable dictionary and intentionally fails open on transport/provider
    failures. ``issues`` contains only decisive FAILs and can be concatenated with
    the Writer's existing fidelity issues to trigger the existing targeted repair.
    """
    settings = get_settings()
    enabled = _env_bool(
        "DOCUMENT_JEV_VALIDATION_ENABLED",
        bool(getattr(settings, "DOCUMENT_JEV_VALIDATION_ENABLED", True)),
    )
    key = _api_key()

    if not enabled:
        return {"enabled": False, "available": False, "issues": [], "checks": {}, "reason": "disabled"}
    if not key:
        return {"enabled": True, "available": False, "issues": [], "checks": {}, "reason": "missing_api_key"}

    questions = _questions(sources)
    if not questions:
        return {"enabled": True, "available": True, "issues": [], "checks": {}, "reason": "no_questions"}

    confidence_threshold = float(
        os.getenv("DOCUMENT_JEV_CONFIDENCE_THRESHOLD")
        or getattr(settings, "DOCUMENT_JEV_CONFIDENCE_THRESHOLD", None)
        or getattr(settings, "JEV_CONFIDENCE_THRESHOLD", 0.65)
    )
    margin_threshold = float(
        os.getenv("DOCUMENT_JEV_MARGIN_THRESHOLD")
        or getattr(settings, "DOCUMENT_JEV_MARGIN_THRESHOLD", None)
        or getattr(settings, "JEV_MARGIN_THRESHOLD", 0.40)
    )
    timeout = float(
        os.getenv("DOCUMENT_JEV_TIMEOUT_S")
        or getattr(settings, "DOCUMENT_JEV_TIMEOUT_S", 25.0)
    )

    mask = _Pseudonymizer()
    state = {
        "document_type": (sources or {}).get("document_type"),
        "rulebook": fidelity.generation_rules((sources or {}).get("document_type")),
        "sources": mask(_compact(sources)),
        "document": mask(_compact(parsed)),
    }
    payload = {
        "model": os.getenv("JEV_MODEL") or getattr(settings, "JEV_MODEL", "jev-latest"),
        "state": state,
        "questions": questions,
    }
    url = os.getenv("JEV_API_URL") or getattr(
        settings, "JEV_API_URL", "https://api.typesafe.ai/v1/systemone"
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        parsed_result = _parse_answers(
            data,
            questions.keys(),
            confidence_threshold=confidence_threshold,
            margin_threshold=margin_threshold,
        )
        return {
            "enabled": True,
            "available": True,
            "model": data.get("model") or payload["model"],
            "issues": parsed_result["issues"],
            "checks": parsed_result["checks"],
            "reason": "ok",
        }
    except Exception as exc:
        # Extra safety layer: do not make document generation unavailable because
        # the external decision service is temporarily unavailable.
        return {
            "enabled": True,
            "available": False,
            "issues": [],
            "checks": {},
            "reason": "provider_error",
            "error": f"{type(exc).__name__}: {exc}",
        }
