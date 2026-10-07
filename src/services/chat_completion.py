"""Compatibilidade com o backend que exige valor positivo para finalizar."""
import math

PENDING_MESSAGE = (
    "Seu relato foi recebido. O valor do prejuizo ainda precisa ser apurado. "
    "Anexe os comprovantes, laudos ou orcamentos disponiveis para continuar."
)


def can_finish(analysis):
    value = (analysis or {}).get("estimatedValue")
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def prepare_completion(message, finished, analysis, groups):
    if not finished or can_finish(analysis):
        return message, finished, groups
    # O backend atual rejeita esse fechamento. Preservar relato e mostrar uma
    # pendencia explicita, sem prometer que o chamado ja foi criado.
    return PENDING_MESSAGE, False, [{
        "message": PENDING_MESSAGE, "delay": 0,
        "awaiting_continuation": False, "is_finished": False,
    }]
