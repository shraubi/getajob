import hashlib
import chromadb
import config

_client = None
_profile_col = None
_applications_col = None


def _embedding_function():
    if config.OPENAI_API_KEY:
        from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
        return OpenAIEmbeddingFunction(
            api_key=config.OPENAI_API_KEY,
            model_name=config.EMBED_MODEL,
        )
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    return DefaultEmbeddingFunction()


def _get_client():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=config.CHROMA_DIR)
    return _client


def _profile():
    global _profile_col
    if _profile_col is None:
        _profile_col = _get_client().get_or_create_collection(
            name="candidate_profile",
            embedding_function=_embedding_function(),
        )
    return _profile_col


def _applications():
    global _applications_col
    if _applications_col is None:
        _applications_col = _get_client().get_or_create_collection(
            name="past_applications",
            embedding_function=_embedding_function(),
        )
    return _applications_col


def query_profile(query: str, n: int = 3) -> list[str]:
    col = _profile()
    count = col.count()
    if count == 0:
        return []
    results = col.query(query_texts=[query], n_results=min(n, count))
    return results["documents"][0]


def query_applications(query: str, n: int = 2) -> list[dict]:
    col = _applications()
    count = col.count()
    if count == 0:
        return []
    results = col.query(
        query_texts=[query],
        n_results=min(n, count),
        include=["documents", "metadatas"],
    )
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    return [{"jd_snippet": d, "meta": m} for d, m in zip(docs, metas)]


def save_application(
    jd: str, cv: str, message: str, score: int, role: str, company: str
) -> None:
    app_id = hashlib.sha256(jd.encode()).hexdigest()[:16]
    _applications().upsert(
        ids=[app_id],
        documents=[jd[:1000]],
        metadatas=[{
            "company": company,
            "role": role,
            "score": score,
            "cv_snippet": cv[:500],
            "message_snippet": message[:200],
            "outcome": "sent",
        }],
    )
