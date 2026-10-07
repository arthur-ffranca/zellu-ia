"""
Conversation Analyzer Service - Zellu IA Empresa

Analisa conversas para extrair:
- Sentimento do cliente (irritado, neutro, satisfeito)
- Tags/categorias automaticas do chamado
- Nivel de urgencia

Funciona com analise baseada em regras (rapido) + LLM (preciso).
"""

import re
import logging
from typing import Dict, Any, List, Optional
from enum import Enum
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class Sentiment(str, Enum):
    """Sentimentos detectaveis na conversa."""
    MUITO_IRRITADO = "muito_irritado"
    IRRITADO = "irritado"
    NEUTRO = "neutro"
    SATISFEITO = "satisfeito"
    MUITO_SATISFEITO = "muito_satisfeito"


class TicketCategory(str, Enum):
    """Categorias de chamados."""
    FINANCEIRO = "financeiro"
    ENTREGA = "entrega"
    TROCA_DEVOLUCAO = "troca_devolucao"
    PRODUTO_DEFEITO = "produto_defeito"
    DUVIDA = "duvida"
    CANCELAMENTO = "cancelamento"
    RECLAMACAO = "reclamacao"
    ELOGIO = "elogio"
    SUGESTAO = "sugestao"
    OUTRO = "outro"


class UrgencyLevel(str, Enum):
    """Niveis de urgencia."""
    BAIXA = "baixa"
    MEDIA = "media"
    ALTA = "alta"
    CRITICA = "critica"


@dataclass
class ConversationAnalysis:
    """Resultado da analise de conversa."""
    sentiment: Sentiment
    sentiment_score: float  # -1.0 (muito irritado) a 1.0 (muito satisfeito)
    sentiment_indicators: List[str]  # palavras/frases que indicaram o sentimento
    categories: List[TicketCategory]
    primary_category: TicketCategory
    urgency: UrgencyLevel
    summary: str  # resumo curto do problema


class ConversationAnalyzer:
    """
    Analisador de conversas.

    Detecta sentimento, categoriza e avalia urgencia
    baseado no historico de mensagens.
    """

    # Palavras/frases que indicam sentimentos negativos
    NEGATIVE_INDICATORS = {
        "muito_irritado": [
            "absurdo", "ridiculo", "vergonha", "processar", "procon",
            "advogado", "nunca mais", "pessimo", "horrivel", "lixo",
            "roubo", "ladrao", "pilantra", "golpe", "enganado",
            "vou processar", "acionar justica", "reclame aqui",
        ],
        "irritado": [
            "irritado", "chateado", "decepcionado", "frustrado",
            "insatisfeito", "descontente", "bravo", "revoltado",
            "cansado de", "nao aguento mais", "demora", "demorado",
            "ja falei", "de novo", "outra vez", "nao resolvem",
            "descaso", "falta de respeito", "inaceitavel",
        ],
    }

    # Palavras/frases que indicam sentimentos positivos
    POSITIVE_INDICATORS = {
        "muito_satisfeito": [
            "excelente", "perfeito", "maravilhoso", "incrivel",
            "melhor atendimento", "super recomendo", "nota 10",
            "parabens", "muito obrigado", "sensacional",
        ],
        "satisfeito": [
            "obrigado", "agradeco", "resolvido", "otimo", "bom",
            "gostei", "satisfeito", "atendeu", "funcionou",
            "entendi", "ok", "certo", "combinado", "fechado",
            "aceito", "concordo", "pode ser",
        ],
    }

    # Palavras que indicam categorias
    CATEGORY_INDICATORS = {
        TicketCategory.FINANCEIRO: [
            "pagamento", "pagar", "paguei", "cobranca", "boleto",
            "cartao", "pix", "reembolso", "estorno", "dinheiro",
            "valor", "preco", "desconto", "parcela", "fatura",
        ],
        TicketCategory.ENTREGA: [
            "entrega", "entregar", "chegou", "nao chegou", "rastreio",
            "rastreamento", "correios", "transportadora", "prazo",
            "atraso", "atrasado", "pedido", "envio", "despacho",
        ],
        TicketCategory.TROCA_DEVOLUCAO: [
            "trocar", "troca", "devolver", "devolucao", "devolvido",
            "errado", "tamanho errado", "cor errada", "diferente",
            "nao era isso", "arrependimento", "desistir",
        ],
        TicketCategory.PRODUTO_DEFEITO: [
            "defeito", "defeituoso", "quebrado", "estragado",
            "nao funciona", "parou de funcionar", "problema",
            "danificado", "com problema", "veio errado",
        ],
        TicketCategory.CANCELAMENTO: [
            "cancelar", "cancelamento", "cancelei", "desistir",
            "nao quero mais", "cancela", "suspender", "encerrar",
        ],
        TicketCategory.DUVIDA: [
            "duvida", "como funciona", "como faco", "pode me explicar",
            "gostaria de saber", "informacao", "qual", "quando",
            "onde", "porque", "ajuda", "me ajude",
        ],
        TicketCategory.RECLAMACAO: [
            "reclamacao", "reclamar", "queixa", "problema",
            "insatisfeito", "nao gostei", "pessimo", "ruim",
        ],
        TicketCategory.ELOGIO: [
            "elogio", "parabenizar", "otimo atendimento",
            "muito bom", "excelente", "parabens", "adorei",
        ],
        TicketCategory.SUGESTAO: [
            "sugestao", "sugiro", "poderia ter", "seria bom",
            "melhoria", "dica", "feedback",
        ],
    }

    # Indicadores de urgencia
    URGENCY_INDICATORS = {
        UrgencyLevel.CRITICA: [
            "urgente", "urgencia", "emergencia", "agora",
            "imediato", "hoje", "ja", "processar", "advogado",
            "procon", "reclame aqui",
        ],
        UrgencyLevel.ALTA: [
            "rapido", "logo", "preciso", "importante",
            "amanha", "nao posso esperar", "prazo",
        ],
    }

    def analyze(
        self,
        message: str,
        message_history: Optional[List[Dict[str, str]]] = None,
    ) -> ConversationAnalysis:
        """
        Analisa mensagem e historico para extrair insights.

        Args:
            message: Ultima mensagem do cliente
            message_history: Historico de mensagens (opcional)

        Returns:
            ConversationAnalysis com sentimento, categorias e urgencia
        """
        # Juntar texto para analise
        full_text = message.lower()
        if message_history:
            client_messages = [
                m.get("content", m.get("body", "")).lower()
                for m in message_history
                if m.get("role") == "user" or m.get("origin") == "zelinhu"
            ]
            full_text = " ".join(client_messages) + " " + full_text

        # Detectar sentimento
        sentiment, score, indicators = self._detect_sentiment(full_text)

        # Detectar categorias
        categories = self._detect_categories(full_text)
        primary_category = categories[0] if categories else TicketCategory.OUTRO

        # Detectar urgencia
        urgency = self._detect_urgency(full_text, sentiment)

        # Gerar resumo curto
        summary = self._generate_summary(message, primary_category)

        return ConversationAnalysis(
            sentiment=sentiment,
            sentiment_score=score,
            sentiment_indicators=indicators,
            categories=categories,
            primary_category=primary_category,
            urgency=urgency,
            summary=summary,
        )

    def _detect_sentiment(self, text: str) -> tuple:
        """Detecta sentimento baseado em palavras-chave."""
        text_lower = text.lower()
        indicators = []

        # Contar indicadores negativos fortes
        muito_irritado_count = 0
        for word in self.NEGATIVE_INDICATORS["muito_irritado"]:
            if word in text_lower:
                muito_irritado_count += 1
                indicators.append(word)

        # Contar indicadores negativos
        irritado_count = 0
        for word in self.NEGATIVE_INDICATORS["irritado"]:
            if word in text_lower:
                irritado_count += 1
                indicators.append(word)

        # Contar indicadores positivos fortes
        muito_satisfeito_count = 0
        for word in self.POSITIVE_INDICATORS["muito_satisfeito"]:
            if word in text_lower:
                muito_satisfeito_count += 1
                indicators.append(word)

        # Contar indicadores positivos
        satisfeito_count = 0
        for word in self.POSITIVE_INDICATORS["satisfeito"]:
            if word in text_lower:
                satisfeito_count += 1
                indicators.append(word)

        # Calcular score (-1 a 1)
        negative_score = (muito_irritado_count * 2 + irritado_count)
        positive_score = (muito_satisfeito_count * 2 + satisfeito_count)

        total = negative_score + positive_score
        if total == 0:
            score = 0.0
            sentiment = Sentiment.NEUTRO
        else:
            score = (positive_score - negative_score) / max(total, 1)
            score = max(-1.0, min(1.0, score))  # Clamp

            if score <= -0.6:
                sentiment = Sentiment.MUITO_IRRITADO
            elif score <= -0.2:
                sentiment = Sentiment.IRRITADO
            elif score >= 0.6:
                sentiment = Sentiment.MUITO_SATISFEITO
            elif score >= 0.2:
                sentiment = Sentiment.SATISFEITO
            else:
                sentiment = Sentiment.NEUTRO

        # Detectar padroes de frustração (repetição, caps)
        if "!!!" in text or text.upper() == text and len(text) > 20:
            if sentiment == Sentiment.NEUTRO:
                sentiment = Sentiment.IRRITADO
                score = -0.3

        return sentiment, round(score, 2), indicators[:5]  # Limitar a 5 indicadores

    def _detect_categories(self, text: str) -> List[TicketCategory]:
        """Detecta categorias do chamado."""
        text_lower = text.lower()
        category_scores = {}

        for category, words in self.CATEGORY_INDICATORS.items():
            score = sum(1 for word in words if word in text_lower)
            if score > 0:
                category_scores[category] = score

        # Ordenar por score
        sorted_categories = sorted(
            category_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )

        # Retornar categorias com score > 0
        categories = [cat for cat, score in sorted_categories]

        if not categories:
            categories = [TicketCategory.OUTRO]

        return categories

    def _detect_urgency(self, text: str, sentiment: Sentiment) -> UrgencyLevel:
        """Detecta nivel de urgencia."""
        text_lower = text.lower()

        # Verificar indicadores criticos
        for word in self.URGENCY_INDICATORS[UrgencyLevel.CRITICA]:
            if word in text_lower:
                return UrgencyLevel.CRITICA

        # Verificar indicadores altos
        for word in self.URGENCY_INDICATORS[UrgencyLevel.ALTA]:
            if word in text_lower:
                return UrgencyLevel.ALTA

        # Sentimento muito negativo aumenta urgencia
        if sentiment == Sentiment.MUITO_IRRITADO:
            return UrgencyLevel.ALTA
        elif sentiment == Sentiment.IRRITADO:
            return UrgencyLevel.MEDIA

        return UrgencyLevel.BAIXA

    def _generate_summary(self, message: str, category: TicketCategory) -> str:
        """Gera resumo curto do problema."""
        # Mapear categoria para texto
        category_texts = {
            TicketCategory.FINANCEIRO: "Questao financeira",
            TicketCategory.ENTREGA: "Problema com entrega",
            TicketCategory.TROCA_DEVOLUCAO: "Solicitacao de troca/devolucao",
            TicketCategory.PRODUTO_DEFEITO: "Produto com defeito",
            TicketCategory.CANCELAMENTO: "Solicitacao de cancelamento",
            TicketCategory.DUVIDA: "Duvida do cliente",
            TicketCategory.RECLAMACAO: "Reclamacao",
            TicketCategory.ELOGIO: "Elogio/Feedback positivo",
            TicketCategory.SUGESTAO: "Sugestao de melhoria",
            TicketCategory.OUTRO: "Atendimento geral",
        }

        base = category_texts.get(category, "Atendimento")

        # Truncar mensagem para resumo
        if len(message) > 50:
            return f"{base}: {message[:47]}..."
        return f"{base}: {message}"

    def get_sentiment_emoji(self, sentiment: Sentiment) -> str:
        """Retorna emoji para o sentimento."""
        emojis = {
            Sentiment.MUITO_IRRITADO: "😡",
            Sentiment.IRRITADO: "😠",
            Sentiment.NEUTRO: "😐",
            Sentiment.SATISFEITO: "🙂",
            Sentiment.MUITO_SATISFEITO: "😄",
        }
        return emojis.get(sentiment, "😐")

    def get_sentiment_label(self, sentiment: Sentiment) -> str:
        """Retorna label amigavel para o sentimento."""
        labels = {
            Sentiment.MUITO_IRRITADO: "Muito Irritado",
            Sentiment.IRRITADO: "Irritado",
            Sentiment.NEUTRO: "Neutro",
            Sentiment.SATISFEITO: "Satisfeito",
            Sentiment.MUITO_SATISFEITO: "Muito Satisfeito",
        }
        return labels.get(sentiment, "Neutro")

    def get_category_label(self, category: TicketCategory) -> str:
        """Retorna label amigavel para a categoria."""
        labels = {
            TicketCategory.FINANCEIRO: "Financeiro",
            TicketCategory.ENTREGA: "Entrega",
            TicketCategory.TROCA_DEVOLUCAO: "Troca/Devolucao",
            TicketCategory.PRODUTO_DEFEITO: "Produto com Defeito",
            TicketCategory.CANCELAMENTO: "Cancelamento",
            TicketCategory.DUVIDA: "Duvida",
            TicketCategory.RECLAMACAO: "Reclamacao",
            TicketCategory.ELOGIO: "Elogio",
            TicketCategory.SUGESTAO: "Sugestao",
            TicketCategory.OUTRO: "Outro",
        }
        return labels.get(category, "Outro")

    def get_urgency_label(self, urgency: UrgencyLevel) -> str:
        """Retorna label amigavel para urgencia."""
        labels = {
            UrgencyLevel.BAIXA: "Baixa",
            UrgencyLevel.MEDIA: "Media",
            UrgencyLevel.ALTA: "Alta",
            UrgencyLevel.CRITICA: "Critica",
        }
        return labels.get(urgency, "Media")


# Instancia global
conversation_analyzer = ConversationAnalyzer()
