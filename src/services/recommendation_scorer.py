"""
Servico de scoring dinamico para recomendacoes de resolucao.

Calcula scores para 3 tipos de resolucao (amigavel, extrajudicial, judicial)
baseado nos dados coletados do caso (categoria juridica, valor, urgencia, etc).

Nao faz chamadas LLM - usa heuristica deterministica com base + modificadores.

Escala: 0-10 (unificada para todos os fluxos).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass
class ScoringInput:
    """Dados de entrada para o scoring de recomendacoes."""
    legal_category: str = "Geral"
    urgency: str = "Media"
    estimated_value: float = 0.0
    client_rights: List[str] = field(default_factory=list)
    problem_description: str = ""
    opposing_party_found_in_db: bool = False
    opposing_party_name: str = ""


# Tipo auxiliar: (amigavel, extrajudicial, judicial)
Scores = Tuple[float, float, float]


def _remove_accents(text: str) -> str:
    """Remove acentos para comparacao case-insensitive."""
    nfkd = unicodedata.normalize('NFD', text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


class RecommendationScorer:
    """
    Calcula scores dinamicos para recomendacoes amigavel/extrajudicial/judicial.

    Algoritmo: base por categoria + modificadores por valor, urgencia,
    palavras-chave, presenca da empresa no DB e quantidade de direitos.
    """

    # Etapa 1: Scores base por categoria juridica
    # (amigavel, extrajudicial, judicial)
    CATEGORY_BASE_SCORES: Dict[str, Scores] = {
        "Direito do Consumidor":   (7.5, 5.5, 3.5),
        "Direito Trabalhista":     (4.0, 6.0, 7.5),
        "Direito Civil":           (5.0, 6.0, 6.5),
        "Direito Empresarial":     (5.5, 7.0, 5.0),
        "Direito Imobiliário":     (4.0, 5.5, 7.0),
        "Direito Imobiliario":     (4.0, 5.5, 7.0),
        "Direito Tributário":      (3.0, 5.0, 7.5),
        "Direito Tributario":      (3.0, 5.0, 7.5),
        "Direito Previdenciário":  (2.0, 4.0, 8.0),
        "Direito Previdenciario":  (2.0, 4.0, 8.0),
        "Direito de Família":      (5.0, 4.0, 7.0),
        "Direito de Familia":      (5.0, 4.0, 7.0),
        "Direito da Saúde":        (3.5, 5.0, 7.5),
        "Direito da Saude":        (3.5, 5.0, 7.5),
        "Direito Penal":           (1.5, 3.0, 9.0),
        "Geral":                   (6.5, 5.5, 5.0),
    }

    DEFAULT_BASE: Scores = (6.5, 5.5, 5.0)

    # Etapa 3: Modificadores por urgencia
    # (amigavel_mod, extrajudicial_mod, judicial_mod)
    URGENCY_MODIFIERS: Dict[str, Scores] = {
        "baixa":  (+0.5,  0.0, -0.5),
        "media":  ( 0.0,  0.0,  0.0),
        "alta":   (-0.5, +0.5, +0.5),
        "critica": (-1.0, +0.5, +1.0),
    }

    # Etapa 4: Modificadores por palavras-chave no problema
    # (keywords, amigavel_mod, extrajudicial_mod, judicial_mod, reason_amigavel, reason_extrajudicial, reason_judicial)
    KEYWORD_MODIFIERS: List[tuple] = [
        (
            ["plano de saude", "convenio", "cirurgia negada", "negou cobertura", "plano negou"],
            -1.5, +0.5, +1.5,
            "Planos de saude raramente resolvem sem intervencao formal",
            "Notificacao pode acelerar resposta do plano",
            "Disputas com planos de saude geralmente exigem via judicial",
        ),
        (
            ["dano moral", "danos morais", "humilhacao", "constrangimento", "abuso"],
            -0.5, +0.5, +1.0,
            "Danos morais sao dificeis de resolver amigavelmente",
            "Notificacao extrajudicial pode pressionar acordo",
            "Indenizacao por danos morais geralmente requer acao judicial",
        ),
        (
            ["7 dias", "arrependimento", "compra online", "comprei pela internet"],
            +1.0, -0.5, -0.5,
            "Direito de arrependimento (7 dias) favorece resolucao rapida",
            "Arrependimento em 7 dias normalmente resolve sem notificacao",
            "Arrependimento em 7 dias raramente precisa de acao judicial",
        ),
        (
            ["procon", "reclame aqui", "consumidor.gov"],
            +0.5, +1.0, 0.0,
            "Cliente ja buscou canais de reclamacao",
            "Canais extrajudiciais como Procon/Reclame Aqui sao adequados",
            None,
        ),
        (
            ["advogado", "processar", "processo", "acionar justica", "juizado"],
            -1.0, +0.5, +1.0,
            "Cliente ja menciona via judicial",
            "Notificacao formal pode evitar processo",
            "Cliente demonstra intencao de buscar via judicial",
        ),
        (
            ["acordo", "resolver", "negociar", "conversar", "amigavel"],
            +1.0, +0.5, -0.5,
            "Cliente aberto a negociacao direta",
            "Disposicao para acordo facilita mediacao",
            "Cliente prefere resolver sem processo",
        ),
        (
            ["rescisao", "verbas rescisorias", "fgts", "hora extra", "horas extras"],
            -0.5, +0.5, +1.0,
            "Direitos trabalhistas sao dificeis de negociar diretamente",
            "Notificacao pode pressionar pagamento de verbas",
            "Verbas rescisorias e FGTS sao direitos claros na CLT",
        ),
        (
            ["despejo", "despejar", "aluguel atrasado", "desocupar imovel"],
            -1.0, 0.0, +1.5,
            "Acoes de despejo exigem procedimento judicial",
            None,
            "Despejo requer acao judicial propria",
        ),
        (
            ["pensao", "guarda", "divorcio", "separacao", "alimentos"],
            -0.5, +0.5, +1.0,
            "Questoes familiares raramente se resolvem informalmente",
            "Mediacao familiar pode ser alternativa",
            "Questoes de familia geralmente tramitam na vara judicial",
        ),
        (
            ["aposentadoria", "inss", "beneficio negado", "auxilio doenca", "previdencia"],
            -1.5, 0.0, +2.0,
            "INSS raramente reverte decisoes administrativamente",
            None,
            "Disputas com INSS quase sempre exigem acao judicial",
        ),
        (
            ["ma fe", "fraude", "golpe", "enganado", "estelionato"],
            -1.0, +0.5, +1.5,
            "Ma-fe dificulta resolucao amigavel",
            "Notificacao formal registra a ma-fe",
            "Fraude e ma-fe fortalecem a necessidade de acao judicial",
        ),
        # Discriminacao e Racismo (crime - Lei 7.716/89)
        (
            ["racismo", "racista", "discriminacao", "preconceito", "cor da pele",
             "raca", "injuria racial", "xenofobia", "homofobia", "lgbtfobia",
             "intolerancia religiosa", "olha a sua cor", "por ser negro", "por ser preto"],
            -2.5, +0.5, +3.0,
            "Discriminacao e crime (Lei 7.716/89), resolucao amigavel e insuficiente",
            "Notificacao extrajudicial pode documentar a discriminacao",
            "Discriminacao e crime inafiancavel - via judicial e essencial",
        ),
        # Assedio (moral ou sexual)
        (
            ["assedio", "assedio moral", "assedio sexual", "perseguicao",
             "intimidacao", "coagir", "coacao", "stalking"],
            -1.5, +0.5, +2.0,
            "Assedio raramente se resolve sem intervencao formal",
            "Notificacao pode registrar formalmente o assedio",
            "Assedio exige medidas judiciais para protecao da vitima",
        ),
        # Violencia e Agressao
        (
            ["violencia", "agressao", "agredido", "agrediu", "bateu", "espancou",
             "lesao corporal", "ameaca de morte", "soco", "empurrou",
             # Formas conjugadas de ameaçar (ameacou != ameaca como substring)
             "ameaca", "ameacou", "ameacaram",
             # Destruicao de patrimonio
             "destruiu", "destruiram", "destruicao",
             # Dano patrimonial
             "quebrou", "quebraram", "danificou", "danificaram"],
            -2.0, 0.0, +2.5,
            "Violencia exige medidas judiciais, nao negociacao",
            None,
            "Casos de violencia devem ser tratados pela via judicial e policial",
        ),
    ]

    # Etapa 7: Termos de gravidade nos direitos identificados
    # Se os direitos contem termos que indicam crime/gravidade alta,
    # o scoring deve refletir isso independente da categoria base.
    GRAVITY_HIGH_TERMS = [
        "crime", "penal", "lei 7.716", "injuria racial", "agressao",
        "violencia", "assedio sexual", "inafiancavel", "imprescritivel",
        "lesao corporal", "ameaca", "ameacou", "ameacaram",
        "racismo", "boletim de ocorrencia",
        "destruiu", "destruiram", "destruicao",
    ]
    GRAVITY_MEDIUM_TERMS = [
        "dano moral", "danos morais", "abuso", "constrangimento",
        "humilhacao", "dignidade", "assedio moral",
    ]

    # Reasons default (quando nenhum modificador se aplica)
    DEFAULT_REASONS: Dict[str, str] = {
        "amigavel": "Primeira tentativa de resolucao, com economia de tempo e custos",
        "extrajudicial": "Notificacao formal demonstra seriedade sem custos judiciais",
        "judicial": "Via judicial garante protecao legal completa",
    }

    def score(self, input_data: ScoringInput) -> List[Dict[str, Any]]:
        """
        Calcula scores de recomendacao para os 3 tipos de resolucao.

        Returns:
            Lista com 3 dicts: [{"type", "score", "reason"}, ...]
        """
        # Coletores de razoes por tipo
        reasons: Dict[str, List[str]] = {
            "amigavel": [],
            "extrajudicial": [],
            "judicial": [],
        }

        # Etapa 1: Base por categoria
        scores = self._get_base_scores(input_data.legal_category, reasons)

        # Etapa 2: Modificador por valor
        scores = self._apply_value_modifiers(scores, input_data.estimated_value, reasons)

        # Etapa 3: Modificador por urgencia
        scores = self._apply_urgency_modifiers(scores, input_data.urgency, reasons)

        # Etapa 4: Modificador por palavras-chave
        scores = self._apply_keyword_modifiers(scores, input_data.problem_description, reasons)

        # Etapa 5: Modificador por empresa
        scores = self._apply_opposing_party_modifiers(
            scores, input_data.opposing_party_found_in_db, reasons
        )

        # Etapa 6: Modificador por direitos identificados
        scores = self._apply_rights_modifiers(scores, input_data.client_rights, reasons)

        # Etapa 7: Modificador por gravidade (analisa conteudo dos direitos)
        scores = self._apply_gravity_modifiers(
            scores, input_data.client_rights, input_data.problem_description, reasons
        )

        # Clamp e monta resultado
        amigavel = self._clamp(scores[0])
        extrajudicial = self._clamp(scores[1])
        judicial = self._clamp(scores[2])

        return [
            {
                "type": "amigavel",
                "score": amigavel,
                "reason": self._build_reason(reasons["amigavel"], "amigavel", amigavel),
            },
            {
                "type": "extrajudicial",
                "score": extrajudicial,
                "reason": self._build_reason(reasons["extrajudicial"], "extrajudicial", extrajudicial),
            },
            {
                "type": "judicial",
                "score": judicial,
                "reason": self._build_reason(reasons["judicial"], "judicial", judicial),
            },
        ]

    # ---- Etapas internas ----

    def _get_base_scores(self, category: str, reasons: Dict[str, List[str]]) -> Scores:
        """Etapa 1: Score base pela categoria juridica."""
        base = self.CATEGORY_BASE_SCORES.get(category, self.DEFAULT_BASE)

        # Tenta sem acentos se nao encontrou
        if base == self.DEFAULT_BASE and category != "Geral":
            cat_normalized = _remove_accents(category)
            for key, value in self.CATEGORY_BASE_SCORES.items():
                if _remove_accents(key) == cat_normalized:
                    base = value
                    break

        # Adiciona razao da categoria
        if category and category != "Geral":
            cat_short = category.replace("Direito ", "").replace("do ", "").replace("da ", "").replace("de ", "")
            if base[0] >= 6.0:
                reasons["amigavel"].append(f"Casos de {cat_short} costumam resolver amigavelmente")
            if base[1] >= 6.0:
                reasons["extrajudicial"].append(f"Casos de {cat_short} respondem bem a notificacao formal")
            if base[2] >= 6.5:
                reasons["judicial"].append(f"Casos de {cat_short} frequentemente requerem via judicial")

        return base

    def _apply_value_modifiers(
        self, scores: Scores, value: float, reasons: Dict[str, List[str]]
    ) -> Scores:
        """Etapa 2: Modificador pelo valor estimado."""
        if value <= 0:
            return scores

        a, e, j = scores

        if value < 5_000:
            a += 1.5
            e -= 0.5
            j -= 1.0
            reasons["amigavel"].append(f"Valor baixo (R$ {value:,.0f}) favorece resolucao direta")
            reasons["judicial"].append(f"Valor baixo (R$ {value:,.0f}) torna processo desproporcional")
        elif value < 20_000:
            a += 0.5
            e += 1.0
            j += 0.5
            reasons["extrajudicial"].append(f"Valor medio (R$ {value:,.0f}) justifica notificacao formal")
        elif value < 50_000:
            a -= 0.5
            e += 1.0
            j += 1.5
            reasons["judicial"].append(f"Valor alto (R$ {value:,.0f}) justifica via judicial")
            reasons["extrajudicial"].append(f"Valor alto (R$ {value:,.0f}) merece abordagem formal")
        else:
            a -= 1.0
            e += 0.5
            j += 2.0
            reasons["judicial"].append(f"Valor muito alto (R$ {value:,.0f}) justifica plenamente via judicial")
            reasons["amigavel"].append(f"Valor muito alto (R$ {value:,.0f}) reduz chance de acordo informal")

        return (a, e, j)

    def _apply_urgency_modifiers(
        self, scores: Scores, urgency: str, reasons: Dict[str, List[str]]
    ) -> Scores:
        """Etapa 3: Modificador pela urgencia."""
        if not urgency:
            return scores

        urgency_key = _remove_accents(urgency.strip())
        mods = self.URGENCY_MODIFIERS.get(urgency_key, (0.0, 0.0, 0.0))

        if mods == (0.0, 0.0, 0.0):
            return scores

        a, e, j = scores
        a += mods[0]
        e += mods[1]
        j += mods[2]

        if urgency_key == "critica":
            reasons["judicial"].append("Urgencia critica favorece intervencao judicial imediata")
            reasons["amigavel"].append("Alta urgencia reduz tempo para negociacao amigavel")
        elif urgency_key == "alta":
            reasons["judicial"].append("Urgencia alta pode exigir acao mais rapida")
        elif urgency_key == "baixa":
            reasons["amigavel"].append("Baixa urgencia permite tentar resolucao amigavel primeiro")

        return (a, e, j)

    def _apply_keyword_modifiers(
        self, scores: Scores, description: str, reasons: Dict[str, List[str]]
    ) -> Scores:
        """Etapa 4: Modificador por palavras-chave no problema."""
        if not description:
            return scores

        text = _remove_accents(description)
        a, e, j = scores

        for keywords, mod_a, mod_e, mod_j, reason_a, reason_e, reason_j in self.KEYWORD_MODIFIERS:
            if any(_remove_accents(kw) in text for kw in keywords):
                a += mod_a
                e += mod_e
                j += mod_j
                if reason_a and mod_a != 0:
                    reasons["amigavel"].append(reason_a)
                if reason_e and mod_e != 0:
                    reasons["extrajudicial"].append(reason_e)
                if reason_j and mod_j != 0:
                    reasons["judicial"].append(reason_j)

        return (a, e, j)

    def _apply_opposing_party_modifiers(
        self, scores: Scores, found_in_db: bool, reasons: Dict[str, List[str]]
    ) -> Scores:
        """Etapa 5: Modificador pela presenca da empresa na plataforma."""
        a, e, j = scores

        if found_in_db:
            a += 1.0
            e -= 0.5
            reasons["amigavel"].append("Empresa cadastrada na plataforma, facilitando contato direto")
        else:
            a -= 0.5
            e += 0.5
            reasons["extrajudicial"].append("Empresa nao cadastrada pode exigir notificacao formal")

        return (a, e, j)

    def _apply_rights_modifiers(
        self, scores: Scores, rights: List[str], reasons: Dict[str, List[str]]
    ) -> Scores:
        """Etapa 6: Modificador pela quantidade de direitos identificados."""
        a, e, j = scores
        count = len(rights) if rights else 0

        if count >= 4:
            a += 0.5
            j += 0.5
            reasons["amigavel"].append(f"{count} direitos identificados dao boa base para negociar")
            reasons["judicial"].append(f"{count} direitos identificados fortalecem acao judicial")
        elif count == 0:
            a += 0.5
            j -= 0.5
            reasons["amigavel"].append("Caso com poucos elementos definidos, melhor tentar via amigavel")

        return (a, e, j)

    def _apply_gravity_modifiers(
        self, scores: Scores, rights: List[str], description: str,
        reasons: Dict[str, List[str]]
    ) -> Scores:
        """Etapa 7: Modificador por gravidade do caso.

        Analisa o CONTEUDO dos direitos identificados e da descricao
        para detectar casos graves (crimes, violencia, discriminacao)
        e ajustar os scores de acordo.
        """
        if not rights and not description:
            return scores

        # Concatena direitos + descricao para analise unificada
        combined = " ".join(rights) if rights else ""
        if description:
            combined += " " + description
        combined = _remove_accents(combined)

        a, e, j = scores
        applied_high = False
        applied_medium = False

        # Verifica termos de gravidade ALTA (crimes, violencia)
        for term in self.GRAVITY_HIGH_TERMS:
            if _remove_accents(term) in combined:
                applied_high = True
                break

        # Verifica termos de gravidade MEDIA (danos morais, constrangimento)
        if not applied_high:
            for term in self.GRAVITY_MEDIUM_TERMS:
                if _remove_accents(term) in combined:
                    applied_medium = True
                    break

        if applied_high:
            a -= 2.0
            e += 0.5
            j += 2.0
            reasons["amigavel"].append("Caso envolve conduta grave/criminal, inadequado para acordo informal")
            reasons["judicial"].append("Gravidade do caso exige protecao judicial e possivel acao penal")
        elif applied_medium:
            a -= 0.5
            j += 0.5
            reasons["judicial"].append("Caso envolve ofensa a dignidade, fortalece via judicial")

        return (a, e, j)

    # ---- Helpers ----

    @staticmethod
    def _clamp(value: float, min_val: float = 1.0, max_val: float = 10.0) -> float:
        """Limita score ao range [1.0, 10.0] com 1 casa decimal."""
        return round(max(min_val, min(max_val, value)), 1)

    def _build_reason(self, fragments: List[str], res_type: str, score: float) -> str:
        """Monta razao final a partir dos fragmentos coletados."""
        if not fragments:
            return self.DEFAULT_REASONS.get(res_type, "")

        # Pega os 3 fragmentos mais relevantes (primeiros = mais fortes)
        top = fragments[:3]
        joined = ". ".join(top)

        # Conclusao baseada no score
        if score >= 7.5:
            joined += ". Opcao altamente recomendada."
        elif score >= 5.0:
            joined += ". Opcao viavel a considerar."
        else:
            joined += ". Opcao menos indicada para este caso."

        return joined


# Singleton
recommendation_scorer = RecommendationScorer()


def score_recommendations(state: dict) -> List[Dict[str, Any]]:
    """
    Funcao de conveniencia que extrai dados do state e calcula scores.

    Funciona com ConversationState (fluxo principal) e dicts do external chat.
    Faz fallback para valores seguros quando campos estao ausentes.
    """
    input_data = ScoringInput(
        legal_category=state.get("legal_category") or "Geral",
        urgency=state.get("urgency") or "Media",
        estimated_value=parse_estimated_value(state.get("potential_gain") or state.get("estimated_value") or 0.0),
        client_rights=state.get("client_rights") or [],
        problem_description=state.get("problem_description") or "",
        opposing_party_found_in_db=state.get("opposing_party_found_in_db", False),
        opposing_party_name=state.get("opposing_party_name") or "",
    )
    return recommendation_scorer.score(input_data)

def parse_estimated_value(value: Any) -> float:
    """Converte valores de valor estimado para float de forma segura."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return 0.0

        # Remover simbolos de moeda, espacos e outros caracteres nao numericos.
        cleaned = re.sub(r"[^0-9,\.\-]", "", normalized)
        if not cleaned:
            return 0.0

        # Reconhece formatos brasileiros e americanos.
        if cleaned.count(",") > 0 and cleaned.count(".") == 0:
            cleaned = cleaned.replace(",", ".")
        elif cleaned.count(",") > 0 and cleaned.count(".") > 0:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")

        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    return 0.0