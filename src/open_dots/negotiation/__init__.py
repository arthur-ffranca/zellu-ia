"""Negociacao automatica: ZelinhU (cliente) e voz da empresa.

- ``reading``: o que a empresa disse, extraido como fatos;
- ``policy``:  a jogada do ZelinhU, decidida em codigo;
- ``writer``:  a redacao da jogada, com persona e historico como conversa;
- ``zelinhu``: o agente que junta os tres;
- ``company``: persona e fecho da IA da empresa.
"""
from .policy import COUNTER, TO_CLIENT, Move, NegotiationTrack
from .reading import AgreementDetails, CompanyTurnReading, NegotiationRequest, ReadingError
from .zelinhu import ZelinhuNegotiator

__all__ = [
    "COUNTER", "TO_CLIENT", "Move", "NegotiationTrack",
    "AgreementDetails", "CompanyTurnReading", "NegotiationRequest", "ReadingError",
    "ZelinhuNegotiator",
]
