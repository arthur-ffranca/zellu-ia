"""
Document Analyzer Service - Zellu IA Empresa

Serviço para análise de documentos/imagens enviados pelo chat.
Utiliza OpenAI Vision API para:
- Identificar tipo de documento (comprovante, recibo, nota fiscal, etc)
- Extrair informações relevantes
- Validar legitimidade aparente do documento

Classes:
    DocumentAnalyzer: Serviço de análise de documentos
"""

import base64
import json
import logging
import time
from typing import Dict, Any, List, Optional
from openai import OpenAI

from config import get_settings, llm_default_headers
from src.services.ai_usage_tracker import track_usage

logger = logging.getLogger(__name__)


class DocumentAnalyzer:
    """
    Serviço para análise de documentos enviados no chat.

    Usa OpenAI Vision (GPT-4o) para analisar imagens de documentos
    como comprovantes de pagamento, recibos, notas fiscais, etc.

    Attributes:
        client: Cliente OpenAI
        model: Modelo com suporte a Vision (gpt-4o ou gpt-4o-mini)

    Example:
        >>> analyzer = DocumentAnalyzer()
        >>> result = analyzer.analyze_document(
        ...     image_base64="...",
        ...     content_type="image/jpeg",
        ...     context="Cliente diz que pagou mas não recebeu"
        ... )
        >>> print(result["document_type"])
        "comprovante_pagamento"
    """

    # Tipos de documentos que a IA pode identificar
    DOCUMENT_TYPES = [
        "comprovante_pagamento",  # PIX, TED, boleto pago
        "recibo",                 # Recibo de compra/serviço
        "nota_fiscal",            # NF-e, NFC-e, cupom fiscal
        "nota_fiscal_servico",    # NFS-e
        "laudo",                  # laudo tecnico/medico/avaria
        "orcamento",              # orcamento/proposta de reparo
        "cnh",                    # CNH identificada especificamente
        "boleto",                 # Boleto bancário (não pago)
        "contrato",               # Contratos, termos
        "documento_identidade",   # RG, CPF, CNH
        "comprovante_endereco",   # Conta de luz, água, etc
        "extrato_bancario",       # Extrato de conta
        "print_conversa",         # Screenshot de conversas
        "foto_produto",           # Foto de produto/defeito
        "outro"                   # Não identificado
    ]

    def __init__(self):
        """Inicializa o analisador com cliente OpenAI."""
        self.settings = get_settings()
        self.client = OpenAI(default_headers=llm_default_headers(), api_key=self.settings.openai_api_key)
        # Usa gpt-4o para Vision (gpt-4o-mini também suporta)
        self.model = "gpt-4o"

    def analyze_document(
        self,
        image_base64: str,
        content_type: str,
        filename: str = "",
        context: str = ""
    ) -> Dict[str, Any]:
        """
        Analisa um documento/imagem enviado pelo chat.

        Args:
            image_base64: Imagem codificada em base64
            content_type: MIME type (image/jpeg, image/png, etc)
            filename: Nome do arquivo (opcional)
            context: Contexto da conversa para melhor análise

        Returns:
            Dict contendo:
                - document_type: Tipo identificado
                - is_legitimate: Se parece legítimo
                - confidence: Confiança (0.0 a 1.0)
                - extracted_info: Informações extraídas
                - observations: Observações relevantes
        """
        try:
            # Verifica se é um tipo de imagem suportado
            if not self._is_supported_image(content_type):
                return self._unsupported_format_response(content_type, filename)

            # Monta o prompt de análise
            analysis_prompt = self._build_analysis_prompt(context)

            # Prepara a imagem para a API
            image_url = f"data:{content_type};base64,{image_base64}"

            # Chama a API com Vision
            _t0 = time.perf_counter()
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self._get_system_prompt()
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": analysis_prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": image_url,
                                    "detail": "high"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=1000,
                temperature=0.3  # Baixa temperatura para análise mais precisa
            )

            track_usage(
                module="doc_analyzer",
                model=self.model,
                response=response,
                event_type="document_analysis",
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )

            # Processa a resposta
            result_text = response.choices[0].message.content
            return self._parse_analysis_response(result_text)

        except Exception as e:
            logger.error(f"Erro ao analisar documento: {e}")
            return {
                "document_type": "outro",
                "is_legitimate": False,
                "confidence": 0.0,
                "extracted_info": {},
                "observations": f"Erro na análise: {str(e)}"
            }

    def analyze_multiple(
        self,
        attachments: List[Dict[str, str]],
        context: str = ""
    ) -> List[Dict[str, Any]]:
        """
        Analisa múltiplos documentos.

        Args:
            attachments: Lista de dicts com filename, content_type, data
            context: Contexto da conversa

        Returns:
            Lista de resultados de análise
        """
        results = []
        for attachment in attachments:
            result = self.analyze_document(
                image_base64=attachment.get("data", ""),
                content_type=attachment.get("content_type", ""),
                filename=attachment.get("filename", ""),
                context=context
            )
            result["filename"] = attachment.get("filename", "")
            results.append(result)
        return results

    def _is_supported_image(self, content_type: str) -> bool:
        """Verifica se o tipo de arquivo é suportado pela Vision API."""
        supported = [
            "image/jpeg",
            "image/jpg",
            "image/png",
            "image/gif",
            "image/webp"
        ]
        return content_type.lower() in supported

    def _unsupported_format_response(
        self,
        content_type: str,
        filename: str
    ) -> Dict[str, Any]:
        """Retorna resposta para formatos não suportados."""
        return {
            "document_type": "outro",
            "is_legitimate": False,
            "confidence": 0.0,
            "extracted_info": {
                "filename": filename,
                "content_type": content_type
            },
            "observations": (
                f"Formato '{content_type}' não suportado para análise visual. "
                "Formatos aceitos: JPEG, PNG, GIF, WebP. "
                "Para PDFs, converta para imagem ou envie como texto."
            )
        }

    def _get_system_prompt(self) -> str:
        """Retorna o system prompt para análise de documentos."""
        return """Você é um especialista em análise de documentos para atendimento ao cliente.

Sua função é analisar imagens de documentos enviados por clientes e identificar:
1. O TIPO de documento (comprovante de pagamento, recibo, nota fiscal, etc)
2. Se o documento PARECE LEGÍTIMO (não se pode afirmar com certeza, apenas aparência)
3. INFORMAÇÕES RELEVANTES visíveis no documento
4. OBSERVAÇÕES importantes para o atendimento

IMPORTANTE:
- Nunca afirme categoricamente que um documento é falso ou verdadeiro
- Use termos como "aparenta ser", "parece legítimo", "apresenta características de"
- NÃO invente, complete ou corrija nenhum dado. Campo não visível deve ser null
- Extraia literalmente CPF, CNPJ, número do documento, datas, valores, chave/código de verificação e registro profissional quando visíveis
- Se a imagem estiver ilegível ou de má qualidade, informe isso
- Se houver sinais de edição ou inconsistências, mencione como observação
- O resultado será auditado por regras determinísticas; precisão dos campos é mais importante que texto bonito

TIPOS DE DOCUMENTO que você pode identificar:
- comprovante_pagamento: PIX, TED, DOC, boleto pago, depósito
- recibo: Recibo de compra ou serviço
- nota_fiscal: NF-e, NFC-e, cupom fiscal
- nota_fiscal_servico: NFS-e municipal
- laudo: laudo técnico, médico, de avaria ou parecer técnico
- orcamento: orçamento/proposta de reparo ou serviço
- cnh: Carteira Nacional de Habilitação
- boleto: Boleto bancário (não pago)
- contrato: Contratos, termos de serviço
- documento_identidade: RG, CPF, CNH, passaporte
- comprovante_endereco: Conta de luz, água, gás, internet
- extrato_bancario: Extrato de conta corrente/poupança
- print_conversa: Screenshot de WhatsApp, email, chat
- foto_produto: Foto de produto, defeito, entrega
- outro: Quando não se encaixa em nenhuma categoria

Responda SEMPRE em formato JSON válido."""

    def _build_analysis_prompt(self, context: str = "") -> str:
        """Constrói o prompt de análise."""
        prompt = """Analise esta imagem de documento e retorne um JSON com a seguinte estrutura:

{
    "document_type": "tipo_do_documento",
    "is_legitimate": true/false,
    "confidence": 0.0 a 1.0,
    "extracted_info": {
        "nome": "se visível, senão null",
        "cpf": "se visível, senão null",
        "cnpj": "se visível, senão null",
        "numero_documento": "se visível, senão null",
        "data_emissao": "se visível, senão null",
        "validade": "se visível, senão null",
        "valor_total": "se visível, senão null",
        "codigo_verificacao": "se visível, senão null",
        "chave_acesso": "se visível, senão null",
        "registro_profissional": "se visível, senão null",
        "categoria": "se visível, senão null",
        "emissor": "se visível, senão null",
        "tomador": "se visível, senão null",
        "endereco": "se visível, senão null",
        "numero_transacao": "se visível, senão null",
        "outros_dados": "apenas dados relevantes literalmente visíveis"
    },
    "observations": "Observações sobre o documento"
}"""

        if context:
            prompt += f"\n\nCONTEXTO DA CONVERSA: {context}"

        prompt += "\n\nResponda APENAS com o JSON, sem texto adicional."
        return prompt

    def _parse_analysis_response(self, response_text: str) -> Dict[str, Any]:
        """Parseia a resposta da API em um dicionário estruturado."""
        try:
            # Tenta extrair JSON da resposta
            # Remove possíveis marcadores de código
            cleaned = response_text.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            result = json.loads(cleaned)

            # Valida e normaliza o tipo de documento
            doc_type = result.get("document_type", "outro")
            if doc_type not in self.DOCUMENT_TYPES:
                doc_type = "outro"

            return {
                "document_type": doc_type,
                "is_legitimate": bool(result.get("is_legitimate", False)),
                "confidence": float(result.get("confidence", 0.5)),
                "extracted_info": result.get("extracted_info", {}),
                "observations": result.get("observations", "")
            }

        except json.JSONDecodeError:
            # Se não conseguir parsear JSON, extrai o que puder
            logger.warning(f"Não foi possível parsear resposta como JSON: {response_text[:200]}")
            return {
                "document_type": "outro",
                "is_legitimate": False,
                "confidence": 0.3,
                "extracted_info": {},
                "observations": response_text[:500]
            }

    def get_document_description(self, doc_type: str) -> str:
        """Retorna descrição amigável do tipo de documento."""
        descriptions = {
            "comprovante_pagamento": "Comprovante de Pagamento",
            "recibo": "Recibo",
            "nota_fiscal": "Nota Fiscal",
            "nota_fiscal_servico": "Nota Fiscal de Serviços (NFS-e)",
            "laudo": "Laudo Técnico",
            "orcamento": "Orçamento",
            "cnh": "Carteira Nacional de Habilitação",
            "boleto": "Boleto Bancário",
            "contrato": "Contrato",
            "documento_identidade": "Documento de Identidade",
            "comprovante_endereco": "Comprovante de Endereço",
            "extrato_bancario": "Extrato Bancário",
            "print_conversa": "Print de Conversa",
            "foto_produto": "Foto de Produto",
            "outro": "Documento"
        }
        return descriptions.get(doc_type, "Documento")


# Instância singleton para uso global
document_analyzer = DocumentAnalyzer()
