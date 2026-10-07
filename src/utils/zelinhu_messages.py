# -*- coding: utf-8 -*-
"""
Modulo centralizado para criacao de mensagens do ZelinhU.

Este modulo contem todas as funcoes para gerar mensagens do ZelinhU
(agente que representa o cliente na negociacao com a empresa).

Usado por:
- src/main.py (endpoint /api/company/base-dados quando autoStart)
- api/routes/negotiation.py (Agente Sender)
"""

from typing import Optional, Dict, Any


def create_zelinhu_initial_message(
    client_name: str,
    ticket_title: str,
    ticket_description: str,
    ticket_category: Optional[str] = None,
    estimated_value: Optional[float] = None,
    solution_type: str = "amigavel"
) -> str:
    """
    Cria a mensagem inicial do ZelinhU para apresentar o caso a empresa.

    O ZelinhU representa o cliente na negociacao e deve:
    1. Apresentar-se de forma profissional
    2. Expor claramente o problema do cliente
    3. Solicitar uma resolucao

    Args:
        client_name: Nome completo do cliente
        ticket_title: Titulo/assunto do ticket
        ticket_description: Descricao detalhada do problema
        ticket_category: Categoria do ticket (BILLING, DELIVERY, etc)
        estimated_value: Valor estimado do dano em R$
        solution_type: Tipo de solucao buscada (amigavel ou extrajudicial)

    Returns:
        Mensagem formatada para enviar a empresa
    """
    # Extrair primeiro nome
    first_name = client_name.split()[0] if client_name else "cliente"

    # Mapear categoria para texto legivel
    category_map = {
        # Categorias padrao
        "BILLING": "cobranca",
        "DELIVERY": "entrega",
        "PRODUCT": "produto",
        "SERVICE": "servico",
        "CONTRACT": "contrato",
        "OTHER": "atendimento",
        # Categorias legadas (compatibilidade)
        "cobranca_indevida": "cobranca indevida",
        "produto_defeituoso": "produto com defeito",
        "servico_nao_prestado": "servico nao prestado",
        "cancelamento": "problema com cancelamento",
        "atraso_entrega": "atraso na entrega",
        "propaganda_enganosa": "propaganda enganosa",
        "direito_consumidor": "direito do consumidor",
        "direito_trabalhista": "direito trabalhista",
        "consumer_law": "direito do consumidor",
        "labor_law": "direito trabalhista",
    }
    category_text = category_map.get(ticket_category, "reclamacao") if ticket_category else "reclamacao"

    # Construir mensagem
    parts = []

    # Saudacao e apresentacao
    parts.append(f"Ola! Sou o ZelinhU, assistente juridico que representa {first_name} nesta negociacao.")
    parts.append("\n\n")

    # Assunto do ticket
    if ticket_title:
        parts.append(f"**Problema reportado:** {ticket_title}")
        parts.append("\n\n")

    # Descricao do problema
    if ticket_description:
        parts.append(f"**Detalhes:** {ticket_description}")
        parts.append("\n\n")
    else:
        parts.append(f"{first_name} abriu uma reclamacao e esta aguardando uma solucao.")
        parts.append("\n\n")

    # Valor estimado (se houver)
    if estimated_value and estimated_value > 0:
        parts.append(f"**Valor estimado do dano:** R$ {estimated_value:,.2f}")
        parts.append("\n\n")

    # Categoria
    if category_text and category_text != "reclamacao":
        parts.append(f"**Categoria:** {category_text}")
        parts.append("\n\n")

    # Tipo de resolucao
    if solution_type == "extrajudicial":
        parts.append("O cliente busca uma resolucao extrajudicial para esta questao.")
    else:
        parts.append("Solicitamos que a empresa analise o caso e apresente uma proposta de solucao para resolver esta questao de forma amigavel.")

    message = "".join(parts)

    print(f"[ZELINHU] Mensagem inicial criada para {first_name} - {len(message)} chars")

    return message


def create_zelinhu_counter_message(
    client_name: str,
    current_round: int,
    estimated_value: float = 0,
    max_rounds: int = 6
) -> str:
    """
    Cria mensagem de contraproposta do ZelinhU baseada na rodada atual.

    Progressao da negociacao:
    - Rodada 1-2: Pede proposta melhor, tom amigavel
    - Rodada 3-4: Reconhece esforco mas pede ajuste final
    - Rodada 5: Ultima tentativa antes de enviar ao cliente

    Args:
        client_name: Nome do cliente
        current_round: Rodada atual da negociacao
        estimated_value: Valor estimado do dano
        max_rounds: Numero maximo de rodadas (default 6)

    Returns:
        Mensagem de contraproposta
    """
    first_name = client_name.split()[0] if client_name else "cliente"

    if current_round <= 2:
        message = f"""Agradeco a proposta apresentada. Entendo a posicao da empresa, mas considerando o prejuizo de R$ {estimated_value:,.2f} sofrido por {first_name}, acredito que podemos chegar a uma solucao mais equilibrada.

O cliente busca uma compensacao que cubra o dano e os transtornos causados. A empresa poderia melhorar a oferta?"""

    elif current_round <= 4:
        message = f"""Reconheco o esforco da empresa em melhorar a proposta e {first_name} aprecia a disposicao para negociar.

Porem, para fecharmos um acordo amigavel, seria importante um pequeno ajuste na compensacao oferecida. Estamos proximos de um consenso.

A empresa pode fazer um ultimo esforco para chegarmos a um acordo satisfatorio para ambas as partes?"""

    else:
        message = f"""Estamos na rodada {current_round} e reconheco que a empresa tem feito esforcos para resolver a questao de {first_name}.

Para evitar que este caso se prolongue, peco que a empresa apresente sua melhor e ultima proposta. Caso seja razoavel, encaminharei diretamente ao cliente para avaliacao e aceite."""

    print(f"[ZELINHU] Contraproposta criada para {first_name} (rodada {current_round}) - {len(message)} chars")

    return message
