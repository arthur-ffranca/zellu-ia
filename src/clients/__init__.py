# -*- coding: utf-8 -*-
"""
Clientes HTTP para comunicacao com backend Zellu.

- ZelluClient: Envia respostas da IA via webhook
- ZelluAPIClient: Busca configuracoes (instructions, documents) da API Zellu
"""

from src.zellu_client import ZelluClient
from .api_client import ZelluAPIClient

__all__ = ["ZelluClient", "ZelluAPIClient"]
