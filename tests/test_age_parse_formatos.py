"""Formatos de data aceitos por /phone/age-check (rodada 4, 2026-10-07)."""
from datetime import date

import pytest

from agents.conversational_agent.age_check import parse_data_de_nascimento


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("1998-08-01", date(1998, 8, 1)),
        ("01/08/1998", date(1998, 8, 1)),
        ("1/8/98", date(1998, 8, 1)),
        ("01.08.1998", date(1998, 8, 1)),
        ("01081998", date(1998, 8, 1)),
        ("010898", date(1998, 8, 1)),
        ("5-3-05", date(2005, 3, 5)),
        ("31/07/03", date(2003, 7, 31)),
        ("07/31/2003", date(2003, 7, 31)),
        ("31/09/2003", None),  # setembro tem 30 dias: continua invalido
        ("abc", None),
    ],
)
def test_formatos_de_data_de_nascimento(entrada, esperado):
    assert parse_data_de_nascimento(entrada) == esperado
