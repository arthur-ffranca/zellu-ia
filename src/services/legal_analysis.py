from agents.state import ConversationState
from src.services.service_context import ServiceContext
from src.llm import ChatModel
import json

class LegalAnalysis(ServiceContext):
    """Consulta base de conhecimento e sugere próximos passos"""
    
    def __init__(self, llm, rag_system):
        super().__init__("analyst", llm)
        self.rag = rag_system
    
    async def process(self, state: ConversationState) -> ConversationState:
        """Análise com RAG e recomendações"""
        print(f"[ANALYST] Iniciando analise juridica completa...")
        self.log_decision("starting_analysis", {
            "category": state.get('legal_category')
        })

        query = self._build_rag_query(state)
        relevant_docs = await self.rag.search(query, top_k=3)
        from config import get_settings
        if state.get('case_confirmed'):
            from src.services.staged_intake_adapter import route_legal_candidates
            relevant_docs = await route_legal_candidates(state, relevant_docs)
            from src.services.client_intake_record import save_record
            save_record(state)

        state['relevant_documents'] = relevant_docs
        print(f"[ANALYST] Documentos RAG recuperados: {len(relevant_docs)}")

        next_steps = await self._generate_next_steps(state, relevant_docs)
        state['suggested_next_steps'] = next_steps
        print(f"[ANALYST] Proximos passos gerados: {len(next_steps)}")

        required_docs = self._suggest_required_documents(state)
        state['required_documents'] = required_docs

        # NÃO adiciona mensagem longa - usaremos group_messages no main.py
        # response = self._format_analysis_response(state, relevant_docs, next_steps, required_docs)
        # state['messages'].append({
        #     'role': 'assistant',
        #     'content': response,
        #     'agent': self.name
        # })

        state['analysis_completed'] = True
        print(f"[ANALYST] Analise completa! Marcando completed=True")
        print(f"[ANALYST] is_finished sera enviado para Zellu")

        self.log_decision("analysis_completed", {
            "docs_retrieved": len(relevant_docs),
            "steps_suggested": len(next_steps),
            "completed": True
        })

        return state

    def validate_state(self, state: ConversationState) -> bool:
        """Validar se o estado tem informações necessárias para análise."""
        required = ['legal_category', 'problem_description']
        return all(state.get(field) for field in required)

    def _build_rag_query(self, state: ConversationState) -> str:
        """Construir query para RAG"""
        parts = []
        if state.get('legal_category'):
            parts.append(state['legal_category'])
        if state.get('problem_description'):
            parts.append(state.get('case_summary') or state['problem_description'])
        if state.get('opposing_party_name'):
            parts.append(state['opposing_party_name'])
        return " ".join(parts) if parts else "consulta juridica geral"
    
    async def _generate_next_steps(self, state: ConversationState, docs: list) -> list:
        """Gerar próximos passos baseado em RAG"""
        
        docs_context = "\n\n".join([
            f"Documento: {doc.get('metadata', {}).get('source', 'Legislação')}\n{doc['content'][:300]}..."
            for doc in docs
        ])
        
        prompt = """
Baseado nas informações jurídicas e no caso do cliente, sugira 3-5 próximos passos práticos.

{collected_context}

DOCUMENTOS JURÍDICOS RELEVANTES:
{docs_context}

Forneça passos práticos, objetivos e acionáveis.
Responda apenas com uma lista numerada, sem explicações adicionais.
"""
        
        response = await self.llm.complete(
            prompt.format(
                collected_context=self.build_collected_context(state),
                docs_context=docs_context
            )
        )
        
        steps = [line.strip() for line in response.content.split('\n') if line.strip() and (line[0].isdigit() or line.startswith('-'))]
        return steps[:5]
    
    def _suggest_required_documents(self, state: ConversationState) -> list:
        """Sugerir documentos que cliente deve providenciar"""
        category = state['legal_category']
        
        docs_by_category = {
            "Direito do Consumidor": [
                "Nota fiscal da compra",
                "Fotos do produto/serviço",
                "Comprovante de pagamento",
                "Registro de reclamação (Procon, SAC)",
                "Mensagens/emails com a empresa"
            ],
            "Direito Trabalhista": [
                "Carteira de trabalho (CTPS)",
                "Contracheques (últimos 12 meses)",
                "Rescisão de contrato (se aplicável)",
                "Comprovantes de horas extras",
                "Testemunhas (contatos)"
            ],
            "Direito Civil": [
                "Documentos pessoais (RG, CPF)",
                "Comprovante de residência",
                "Contrato (se houver)",
                "Fotos/vídeos do problema",
                "Boletim de ocorrência (se aplicável)"
            ]
        }
        
        return docs_by_category.get(category, [
            "Documentos pessoais (RG, CPF)",
            "Comprovantes relacionados ao caso",
            "Testemunhas ou evidências"
        ])
    
    def _format_analysis_response(self, state: ConversationState, docs: list, 
                                  steps: list, required_docs: list) -> str:
        """Formatar resposta final da análise"""
        
        docs_section = "\n\n".join([
            f"**{doc.get('metadata', {}).get('source', 'Legislação')}**\n{doc['content'][:400]}...\n[Relevancia: {doc['score']:.2%}]"
            for doc in docs
        ])

        steps_section = "\n".join([f"{i+1}. {step}" for i, step in enumerate(steps)])

        required_docs_section = "\n".join([f"  - {doc}" for doc in required_docs])

        return f"""
**ANALISE JURIDICA COMPLETA**

---

**BASE DE CONHECIMENTO CONSULTADA:**

{docs_section}

---

**PROXIMOS PASSOS RECOMENDADOS:**

{steps_section}

---

**DOCUMENTOS QUE VOCE DEVE PROVIDENCIAR:**

{required_docs_section}

---

**RESUMO DO SEU ATENDIMENTO:**
- Nome: {state['client_name']}
- Categoria: {state['legal_category']}
- Urgencia: {state['urgency']}
- Potencial de Ganho: R$ {state['potential_gain']:,.2f}

Seu caso foi registrado e nossa equipe juridica entrara em contato em ate 48 horas.
Mantenha os documentos solicitados em maos para agilizar o processo.

Em caso de duvidas, entre em contato conosco.
"""
