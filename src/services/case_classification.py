from agents.state import ConversationState
from src.services.service_context import ServiceContext
from src.llm import ChatModel
import json

class CaseClassification(ServiceContext):
    def __init__(self, llm):
        super().__init__("classifier", llm=llm)

    def validate_state(self, state: ConversationState) -> bool:
        """Validar se o estado tem informações necessárias para classificação."""
        # Apenas nome e descrição são necessários - tipo é inferido aqui
        required = ['client_name', 'problem_description']
        return all(state.get(field) for field in required)

    async def _classify_demand(self, state: ConversationState) -> dict:
        """Classificação usando few-shot prompting."""

        prompt = """
Você é um especialista em classificação jurídica. Analise o caso e forneça uma classificação detalhada.

{collected_context}

INSTRUÇÕES:
Identifique automaticamente a categoria jurídica baseando-se APENAS na descrição fornecida.
Você DEVE escolher UMA das seguintes categorias:

1. "Direito do Consumidor" - Relações de consumo B2C (cliente vs empresa fornecedora)
2. "Direito Trabalhista" - Relações de trabalho CLT (empregado vs empregador)
3. "Direito Civil" - Contratos e responsabilidade civil entre particulares
4. "Direito Empresarial" - Relações entre empresas B2B
5. "Direito Imobiliário" - Questões de imóveis, aluguéis, vizinhança
6. "Direito Tributário" - Impostos, taxas e questões fiscais
7. "Direito Previdenciário" - INSS, aposentadoria, benefícios
8. "Direito de Família" - Divórcio, pensão alimentícia, guarda de filhos
9. "Direito Penal" - Crimes, discriminação, racismo, assédio, agressão, ameaça, violência
10. "Geral" - Somente se não se enquadrar em nenhuma das anteriores

Se o caso envolver múltiplas áreas, escolha a área PRINCIPAL (causa raiz do problema).
Use "Geral" como fallback APENAS se não conseguir determinar com confiança.

EXEMPLOS:

Exemplo 1:
Caso: "Comprei um celular que veio com defeito e a loja se recusa a trocar"
Classificação:
{{
  "category": "Direito do Consumidor",
  "urgency": "Média",
  "rights": ["Troca de produto defeituoso (CDC Art. 18)", "Devolução em dobro (CDC Art. 42)", "Danos morais"],
  "potential_gain": 3000.00,
  "reasoning": "Vício do produto, garantia legal de 90 dias"
}}

Exemplo 2:
Caso: "Fui demitido sem justa causa e não recebi verbas rescisórias"
Classificação:
{{
  "category": "Direito Trabalhista",
  "urgency": "Alta",
  "rights": ["Aviso prévio", "13º salário proporcional", "Férias proporcionais", "FGTS + 40%", "Seguro desemprego"],
  "potential_gain": 15000.00,
  "reasoning": "Rescisão sem justa causa, direitos garantidos pela CLT"
}}

Exemplo 3:
Caso: "Meu vizinho construiu um muro que invade meu terreno"
Classificação:
{{
  "category": "Direito Civil",
  "urgency": "Baixa",
  "rights": ["Reintegração de posse", "Indenização por danos", "Demolição da construção irregular"],
  "potential_gain": 8000.00,
  "reasoning": "Esbulho possessório, direito de propriedade"
}}

Exemplo 4:
Caso: "Meu INSS foi negado mesmo tendo tempo de contribuição suficiente"
Classificação:
{{
  "category": "Direito Previdenciário",
  "urgency": "Alta",
  "rights": ["Concessão de aposentadoria por tempo de contribuição", "Revisão de benefício negado"],
  "potential_gain": 50000.00,
  "reasoning": "Benefício previdenciário negado indevidamente"
}}

Exemplo 5:
Caso: "O inquilino não paga aluguel há 3 meses e se recusa a sair"
Classificação:
{{
  "category": "Direito Imobiliário",
  "urgency": "Alta",
  "rights": ["Ação de despejo por falta de pagamento", "Cobrança de aluguéis atrasados", "Multa contratual"],
  "potential_gain": 12000.00,
  "reasoning": "Inadimplência de aluguel, Lei do Inquilinato"
}}

Exemplo 6:
Caso: "Preciso de divórcio e definir pensão alimentícia para meu filho"
Classificação:
{{
  "category": "Direito de Família",
  "urgency": "Média",
  "rights": ["Divórcio consensual ou litigioso", "Pensão alimentícia", "Guarda compartilhada"],
  "potential_gain": 5000.00,
  "reasoning": "Dissolução conjugal com questão alimentar"
}}

Exemplo 7:
Caso: "Durante uma aula online, o professor fez comentários racistas sobre minha cor de pele e me humilhou na frente da turma"
Classificação:
{{
  "category": "Direito Penal",
  "urgency": "Alta",
  "rights": ["Crime de racismo (Lei 7.716/89)", "Injúria racial (Art. 140 §3 CP)", "Indenização por danos morais (CF Art. 5, V e X)", "Boletim de Ocorrência"],
  "potential_gain": 30000.00,
  "reasoning": "Racismo é crime inafiançável e imprescritível (CF Art. 5, XLII). Necessária ação penal e cível para reparação."
}}

Exemplo 8:
Caso: "Meu chefe me persegue no trabalho, faz comentários constrangedores de cunho sexual e ameaça me demitir se eu reclamar"
Classificação:
{{
  "category": "Direito Penal",
  "urgency": "Crítica",
  "rights": ["Assédio sexual (Art. 216-A CP)", "Assédio moral no trabalho", "Indenização por danos morais", "Boletim de Ocorrência", "Rescisão indireta (CLT Art. 483)"],
  "potential_gain": 40000.00,
  "reasoning": "Assédio sexual é crime (Art. 216-A CP). Conduta reiterada configura também assédio moral. Cabe ação penal e trabalhista."
}}

AGORA CLASSIFIQUE O CASO ATUAL:
Responda APENAS com JSON válido, sem explicações adicionais.
"""
        
        response = await self.llm.complete(
            prompt.format(
                collected_context=self.build_collected_context(state)
            )
        )

        try:
            # Limpar resposta removendo markdown code blocks
            content = response.content.strip()
            if content.startswith('```'):
                # Remove code blocks
                content = content.replace('```json', '').replace('```', '').strip()

            classification = json.loads(content)

            # Validar que tem os campos obrigatórios
            if not all(k in classification for k in ['category', 'urgency', 'rights', 'potential_gain']):
                raise ValueError("Missing required fields in classification")

        except Exception as e:
            self.log_decision("classification_parse_error", {"error": str(e), "content": response.content[:200]})
            raise ValueError('Invalid classification response') from e


        return classification

    async def process(self, state):
        if not state.get('case_confirmed'):
            raise ValueError('Classification requires a confirmed case')
        result = await self._classify_demand(state)
        state.update(legal_category=result['category'], urgency=result['urgency'],
                     client_rights=result['rights'], potential_gain=result['potential_gain'],
                     description_complete=True, current_agent='analyst')
        state['step'] = state.get('step', 0) + 1
        return state
