"""Cache persistente de buscas de CNPJ/empresa no MongoDB.

O atendimento nao pode depender desse cache: se Mongo nao estiver configurado,
indisponivel ou sem pymongo instalado, a busca continua normalmente.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from config import get_settings
from src.utils.company_normalizer import is_valid_cnpj, normalize_cnpj
import re
import unicodedata

def normalize_lookup_text(value: str) -> str:
    value = unicodedata.normalize('NFKD', value or '')
    return re.sub(r'\s+', ' ', ''.join(c for c in value if not unicodedata.combining(c)).lower()).strip()


def find_company_by_cnpj(cnpj):
    """Consulta pelo indice de CNPJ, antes de qualquer busca externa."""
    normalized = normalize_cnpj(cnpj)
    if not is_valid_cnpj(normalized):
        return None
    try:
        collection = _get_collection()
        if collection is None:
            return None
        doc = collection.find_one({'cnpj': normalized}, {'_id': 0, 'cnpj': 1,
            'razao_social': 1, 'nome_fantasia': 1, 'endereco': 1,
            'uf': 1, 'municipio': 1, 'favicon_url': 1, 'site': 1})
        return {**doc, **(doc.get('endereco') or {})} if doc else None
    except Exception as exc:
        print(f'[COMPANY-LOOKUP-CACHE] CNPJ lookup failed: {type(exc).__name__}')
        return None

def find_brand_icon(cnpj_root):
    """Icone/site ja gravados para qualquer unidade da mesma raiz de CNPJ (matriz ou filial).

    Os 8 primeiros caracteres do CNPJ identificam a empresa; o icone e da marca.
    Prefixo ancorado usa o indice de cnpj.
    """
    root = re.sub(r'[^0-9A-Z]', '', str(cnpj_root or '').upper())[:8]
    if len(root) != 8:
        return None
    try:
        collection = _get_collection()
        if collection is None:
            return None
        doc = collection.find_one(
            {'cnpj': {'$regex': '^' + re.escape(root)}, 'favicon_url': {'$nin': [None, '']}},
            {'_id': 0, 'favicon_url': 1, 'site': 1},
            max_time_ms=int(_setting('MONGODB_TIMEOUT_MS', 3000)))
        return doc if doc and doc.get('favicon_url') else None
    except Exception as exc:
        print(f'[COMPANY-LOOKUP-CACHE] icone da marca indisponivel: {type(exc).__name__}')
        return None


def find_company_lookup_results(name: str, location: str = '', segment: str = '', limit: int = 5) -> List[Dict[str, Any]]:
    """Consulta apenas dados cadastrados; falha de Mongo permite fallback externo."""
    try:
        collection = _get_collection()
        if collection is None or not name.strip():
            return []
        # Tokens escapados evitam interpretar entrada do cliente como regex.
        tokens = re.findall(r'[a-z0-9]+', normalize_lookup_text(name))
        tokens = [token for token in tokens if token not in {'de', 'do', 'da', 'e', 'no', 'na'}]
        if not tokens:
            return []
        # A base de producao tem indice text em razao_social. Nao varrer
        # quatro milhoes de registros com regex de nome sem indice.
        generic = {'shopping', 'center', 'empresa', 'empresas', 'loja', 'lojas', 'condominio',
                   'civil', 'ltda', 'sa', 'veiculo', 'veiculos', 'comercio', 'servicos'}
        distinct = [token for token in tokens if token not in generic] or tokens
        # Text search usa OR. Buscar varias palavras genericas ampliaria a
        # consulta; escolher a mais especifica e conferir as demais depois.
        anchor = max(reversed(distinct), key=len)
        clauses = [{'$text': {'$search': '"' + anchor + '"'}}]
        for token in re.findall(r'[a-z0-9]+', normalize_lookup_text(location)):
            if token not in {'na', 'no', 'em', 'de', 'do', 'da', 'shopping', 'bairro', 'cidade', 'loja'}:
                clauses.append({'$or': [
                    {'search_location': {'$regex': re.escape(token)}},
                    {'endereco.municipio': {'$regex': re.escape(token), '$options': 'i'}},
                    {'endereco.bairro': {'$regex': re.escape(token), '$options': 'i'}},
                    {'endereco.logradouro': {'$regex': re.escape(token), '$options': 'i'}},
                    {'endereco.complemento': {'$regex': re.escape(token), '$options': 'i'}},
                    {'municipio': {'$regex': re.escape(token), '$options': 'i'}},
                    {'bairro': {'$regex': re.escape(token), '$options': 'i'}},
                    {'logradouro': {'$regex': re.escape(token), '$options': 'i'}},
                    {'busca.local': {'$regex': re.escape(token), '$options': 'i'}},
                ]})
        if not clauses:
            return []
        projection = {'_id': 0, 'cnpj': 1, 'nome_fantasia': 1, 'razao_social': 1,
                      'endereco': 1, 'municipio': 1, 'bairro': 1, 'logradouro': 1,
                      'numero': 1, 'cep': 1, 'uf': 1, 'site': 1, 'favicon_url': 1,
                      'source_url': 1, 'public_page_verified': 1,
                      'score': {'$meta': 'textScore'}}
        docs = list(collection.find({'$and': clauses}, projection).sort([('score', {'$meta': 'textScore'})])
                    .limit(25).max_time_ms(int(_setting('MONGODB_TIMEOUT_MS', 3000))))
        # Mongo text faz OR; exigir todos os tokens evita nomes parecidos.
        docs = [doc for doc in docs if all(token in normalize_lookup_text(
            (doc.get('nome_fantasia') or '') + ' ' + (doc.get('razao_social') or '')) for token in tokens)]
        return [{**doc, **(doc.get('endereco') or {})} for doc in docs[:min(max(limit, 1), 10)]]
    except Exception as exc:
        print(f'[COMPANY-LOOKUP-CACHE] leitura falhou: {type(exc).__name__}')
        return []

try:  # dependencia opcional em runtime; requirements.txt a instala em producao
    from pymongo import MongoClient, UpdateOne
except Exception:  # pragma: no cover - coberto indiretamente pelo fallback
    MongoClient = None  # type: ignore[assignment]
    UpdateOne = None  # type: ignore[assignment]


_client = None
_collection = None


def _setting(name: str, default: Any = None) -> Any:
    return getattr(get_settings(), name, default)


def _get_collection():
    """Retorna a collection configurada ou None se cache estiver desligado."""
    global _client, _collection

    if _collection is not None:
        return _collection

    uri = _setting("MONGODB_URI", "")
    if not uri or MongoClient is None:
        return None

    database_name = _setting("MONGODB_DATABASE", "zellu")
    collection_name = _setting("COMPANY_LOOKUP_CACHE_COLLECTION", "company_lookup_cache")
    timeout_ms = int(_setting("MONGODB_TIMEOUT_MS", 3000))

    _client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms)
    _collection = _client[database_name][collection_name]
    return _collection


def _first_text(company: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = company.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _address(company: Dict[str, Any]) -> Dict[str, str]:
    endereco = company.get("endereco") if isinstance(company.get("endereco"), dict) else {}
    return {
        "uf": _first_text(company, "uf") or _first_text(endereco, "uf"),
        "municipio": _first_text(company, "municipio") or _first_text(endereco, "municipio"),
        "bairro": _first_text(company, "bairro") or _first_text(endereco, "bairro"),
        "logradouro": _first_text(company, "logradouro") or _first_text(endereco, "logradouro"),
        "numero": _first_text(company, "numero") or _first_text(endereco, "numero"),
        "cep": _first_text(company, "cep") or _first_text(endereco, "cep"),
    }


def _clean_cnpj(value: Any) -> str:
    return normalize_cnpj(value)


def build_lookup_document(
    company: Dict[str, Any],
    *,
    searched_name: str = "",
    searched_location: str = "",
    searched_segment: str = "",
    source: str = "unknown",
) -> Optional[Dict[str, Any]]:
    cnpj = _clean_cnpj(company.get("cnpj"))
    if not is_valid_cnpj(cnpj):
        return None

    now = datetime.now(timezone.utc)
    return {
        "cnpj": cnpj,
        "nome_fantasia": _first_text(company, "nome_fantasia", "nomeFantasia", "tradeName"),
        "razao_social": _first_text(company, "razao_social", "razaoSocial", "companyName"),
        "endereco": _address(company),
        "site": _first_text(company, "site", "homepage", "url", "website"),
        "favicon_url": _first_text(company, "favicon_url", "faviconUrl"),
        "source_url": _first_text(company, "source_url"),
        "public_page_verified": bool(company.get("public_page_verified")),
        "search_name": normalize_lookup_text(' '.join([_first_text(company, 'nome_fantasia', 'nomeFantasia', 'tradeName'), _first_text(company, 'razao_social', 'razaoSocial', 'companyName'), searched_name])),
        "search_location": normalize_lookup_text(' '.join([* _address(company).values(), searched_location])),
        "fonte": source,
        "busca": {
            "nome": searched_name or "",
            "local": searched_location or "",
            "segmento": searched_segment or "",
        },
        "updated_at": now,
        "created_at": now,
    }


def save_company_lookup_results(
    companies: Iterable[Dict[str, Any]],
    *,
    searched_name: str = "",
    searched_location: str = "",
    searched_segment: str = "",
    source: str = "unknown",
) -> int:
    """Faz upsert dos resultados de CNPJ no MongoDB.

    Retorna quantos documentos foram preparados para upsert. Qualquer falha fica
    contida aqui para nao quebrar o atendimento do cliente.
    """
    try:
        collection = _get_collection()
    except Exception as exc:
        print(f'[COMPANY-LOOKUP-CACHE] conexao falhou: {type(exc).__name__}')
        return 0
    if collection is None or UpdateOne is None:
        return 0

    docs: List[Dict[str, Any]] = []
    seen = set()
    for company in companies:
        doc = build_lookup_document(
            company,
            searched_name=searched_name,
            searched_location=searched_location,
            searched_segment=searched_segment,
            source=source,
        )
        if not doc or doc["cnpj"] in seen:
            continue
        seen.add(doc["cnpj"])
        # Resultado de busca sem icone nao pode apagar o icone/site ja gravados.
        for key in ("favicon_url", "site"):
            if not doc.get(key):
                doc.pop(key, None)
        docs.append(doc)

    if not docs:
        return 0

    operations = []
    for doc in docs:
        created_at = doc.pop("created_at")
        operations.append(
            UpdateOne(
                {"cnpj": doc["cnpj"]},
                {
                    "$set": doc,
                    "$setOnInsert": {"created_at": created_at},
                    "$inc": {"hit_count": 1},
                },
                upsert=True,
            )
        )

    try:
        collection.bulk_write(operations, ordered=False)
        return len(operations)
    except Exception as exc:
        print(f"[COMPANY-LOOKUP-CACHE] Erro ao salvar no MongoDB: {type(exc).__name__}")
        return 0
