import os
import re
import pathlib
import logging
from rag.store import _profile

logger = logging.getLogger(__name__)

_CV_PATH = pathlib.Path(__file__).parent.parent / "cv" / "base_cv.txt"
_SECTION_RE = re.compile(r"^[A-ZА-ЯЁ][A-ZА-ЯЁ\s]{2,}$", re.MULTILINE)


def load_cv_text() -> str:
    raw = os.environ.get("CV") or _CV_PATH.read_text(encoding="utf-8")
    parts = raw.split("\n---")
    return parts[0].strip()


def _chunk(text: str) -> list[dict]:
    lines = [ln for ln in text.splitlines() if not ln.startswith("//")]
    clean = "\n".join(lines).strip()

    section_matches = list(_SECTION_RE.finditer(clean))
    if not section_matches:
        return [{"section": "full", "text": clean}]

    chunks = []
    header_text = clean[: section_matches[0].start()].strip()
    if header_text:
        chunks.append({"section": "header", "text": header_text})

    for i, match in enumerate(section_matches):
        section_name = match.group().strip()
        start = match.end()
        end = section_matches[i + 1].start() if i + 1 < len(section_matches) else len(clean)
        body = clean[start:end].strip()
        if body:
            chunks.append({"section": section_name, "text": f"{section_name}\n{body}"})

    return chunks


def index_candidate_profile(cv_text: str | None = None) -> None:
    text = cv_text or load_cv_text()

    if not text.strip():
        logger.error(
            "CV text is empty — profile will not be indexed. "
            "Set CV= in .env or replace cv/base_cv.txt with your real CV."
        )
        return

    chunks = _chunk(text)
    if not chunks:
        logger.error("CV produced no indexable chunks — check cv/base_cv.txt format")
        return

    col = _profile()
    ids = [f"profile_chunk_{i}" for i in range(len(chunks))]
    docs = [c["text"] for c in chunks]
    metas = [{"section": c["section"], "type": "profile"} for c in chunks]

    col.upsert(ids=ids, documents=docs, metadatas=metas)
    logger.info("Indexed %d profile chunks", len(chunks))
