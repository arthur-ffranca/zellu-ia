"""
Models de Empresa - Zellu IA Empresa

Define os modelos relacionados à empresa e suas configurações de IA.
Estes modelos controlam como a IA se comporta para cada empresa.

Classes:
    ToneOfVoice: Enum com os tons de voz disponíveis
    CompanyAIConfig: Configuração completa da IA por empresa
    Company: Dados básicos da empresa
"""

from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from enum import Enum


class ToneOfVoice(str, Enum):
    """
    Tom de voz que a IA deve usar nas respostas.

    Attributes:
        FORMAL: Linguagem formal e profissional
        CASUAL: Linguagem descontraída e amigável
        TECNICO: Linguagem técnica e precisa
        AMIGAVEL: Linguagem acolhedora e empática
    """
    FORMAL = "formal"
    CASUAL = "casual"
    TECNICO = "tecnico"
    AMIGAVEL = "amigavel"


class CompanyAIConfig(BaseModel):
    """
    Configuração da IA para uma empresa específica.

    Esta classe define todas as regras e comportamentos que a IA
    deve seguir ao atender clientes de uma empresa.

    Attributes:
        company_id: Identificador único da empresa
        system_prompt: Instruções base para o comportamento da IA
        business_rules: Regras específicas do negócio que a IA deve seguir
        tone_of_voice: Tom de voz nas respostas
        escalation_keywords: Palavras que disparam transferência para humano
        forbidden_topics: Assuntos que a IA não deve abordar

    Example:
        >>> config = CompanyAIConfig(
        ...     company_id="emp_123",
        ...     system_prompt="Você é o assistente da Loja X...",
        ...     business_rules=["Nunca ofereça desconto"],
        ...     escalation_keywords=["falar com gerente"]
        ... )
    """

    # Identificação
    company_id: str = Field(
        ...,
        description="ID único da empresa no sistema Zellu"
    )

    # Comportamento da IA
    system_prompt: str = Field(
        default="Você é um assistente virtual da empresa. Responda de forma educada e profissional.",
        description="Prompt de sistema que define o comportamento base da IA"
    )
    business_rules: List[str] = Field(
        default_factory=list,
        description="Lista de regras de negócio que a IA DEVE seguir"
    )
    tone_of_voice: ToneOfVoice = Field(
        default=ToneOfVoice.FORMAL,
        description="Tom de voz que a IA deve usar nas respostas"
    )
    language: str = Field(
        default="pt-BR",
        description="Idioma das respostas (código ISO)"
    )

    # Limites e Restrições
    max_response_length: int = Field(
        default=1000,
        description="Limite máximo de caracteres por resposta"
    )
    allowed_topics: List[str] = Field(
        default_factory=list,
        description="Tópicos permitidos (vazio = todos permitidos)"
    )
    forbidden_topics: List[str] = Field(
        default_factory=list,
        description="Tópicos que a IA NUNCA deve abordar"
    )

    # Escalação
    escalation_keywords: List[str] = Field(
        default_factory=lambda: ["falar com humano", "advogado", "processo", "justiça"],
        description="Palavras-chave que disparam transferência para atendente humano"
    )

    # Mensagens Padrão
    greeting_message: Optional[str] = Field(
        default=None,
        description="Mensagem de saudação personalizada (None = usar padrão)"
    )
    fallback_message: str = Field(
        default="Não encontrei informações sobre isso em nossa base. Posso transferir para um atendente humano?",
        description="Mensagem quando a IA não encontra resposta adequada"
    )

    # Timestamps
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Data de criação da configuração"
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Data da última atualização"
    )


class CompanyInstructions(BaseModel):
    """
    Instrucoes estruturadas da empresa para a IA.

    Representa os 5 campos de instrucoes que definem o comportamento da IA.

    Attributes:
        company_id: ID da empresa
        persona: Definicao da persona/identidade da IA
        rules: Regras de conduta
        legal: Consideracoes legais
        budget: Limites de orcamento e negociacao
        tone: Tom de voz
    """
    company_id: str = Field(..., description="ID da empresa")
    # Campos opcionais - empresa pode nao ter configurado ainda
    persona: Optional[str] = Field(default=None, description="Persona/identidade da IA")
    rules: Optional[str] = Field(default=None, description="Regras de conduta")
    legal: Optional[str] = Field(default=None, description="Consideracoes legais")
    budget: Optional[str] = Field(default=None, description="Orcamento e negociacao")
    tone: Optional[str] = Field(default="profissional", description="Tom de voz")


class TemporaryRule(BaseModel):
    """
    Regra temporaria da empresa.

    Regras que tem prazo de validade e podem ser pausadas.

    Attributes:
        id: ID da regra
        company_id: ID da empresa
        content: Conteudo/texto da regra
        description: Descricao opcional
        valid_from: Data de inicio
        valid_until: Data de fim
        is_paused: Se esta pausada
    """
    id: str = Field(..., description="ID da regra")
    company_id: str = Field(..., description="ID da empresa")
    content: str = Field(..., description="Conteudo da regra")
    description: Optional[str] = Field(default=None, description="Descricao")
    valid_from: Optional[datetime] = Field(default=None, description="Data inicio")
    valid_until: Optional[datetime] = Field(default=None, description="Data fim")
    is_paused: bool = Field(default=False, description="Se esta pausada")


class Company(BaseModel):
    """
    Modelo básico de empresa.

    Representa os dados cadastrais de uma empresa na plataforma Zellu.

    Attributes:
        id: Identificador único
        name: Nome da empresa
        cnpj: CNPJ (opcional)
        ai_config: Configuração da IA (opcional)
        is_active: Se a empresa está ativa

    Example:
        >>> empresa = Company(
        ...     id="emp_123",
        ...     name="Loja Exemplo LTDA",
        ...     cnpj="12.345.678/0001-90"
        ... )
    """

    id: str = Field(
        ...,
        description="ID único da empresa"
    )
    name: str = Field(
        ...,
        description="Nome/Razão social da empresa"
    )
    cnpj: Optional[str] = Field(
        default=None,
        description="CNPJ da empresa (formato: XX.XXX.XXX/XXXX-XX)"
    )
    ai_config: Optional[CompanyAIConfig] = Field(
        default=None,
        description="Configuração da IA para esta empresa"
    )
    is_active: bool = Field(
        default=True,
        description="Se a empresa está ativa no sistema"
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Data de cadastro"
    )
