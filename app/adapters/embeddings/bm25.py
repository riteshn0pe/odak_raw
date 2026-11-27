"""BM25 sparse vectors, computed locally.

Sparse retrieval catches exact tokens that embeddings blur: part numbers, error
codes, statute references, proper nouns. It costs no API call, so hybrid search
is the default rather than an upsell.

Term ids are hashes of the token, so no vocabulary has to be fitted or stored,
and a new tenant needs no warm up. Qdrant supplies the idf side at query time.
"""

import math
import re
from collections import Counter
from collections.abc import Sequence

from app.domain.ports import SparseVector

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._\-/][a-z0-9]+)*")
HASH_SPACE = 2**31 - 1

K1 = 1.5
B = 0.75
AVG_DOC_LEN = 220.0

STOPWORDS = frozenset(
    """a an and are as at be by for from has have how i in is it its of on or that the
    to was were what when where which who will with you your this these those""".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, keeping internal dots and dashes.

    Preserving those is the point: invoice-2024-11 and v1.2.3 are exactly the
    strings a user searches for verbatim and an embedding is worst at.
    """
    return [
        token
        for token in TOKEN_RE.findall(text.lower())
        if token not in STOPWORDS and len(token) > 1
    ]


def _stable_hash(token: str) -> int:
    # python's hash is salted per process, which would make ids unstable across
    # restarts and therefore across ingest and query
    value = 2166136261
    for char in token.encode():
        value = ((value ^ char) * 16777619) & 0xFFFFFFFF
    return value % HASH_SPACE


def term_id(token: str) -> int:
    return _stable_hash(token)


class BM25Encoder:
    def __init__(self, k1: float = K1, b: float = B, avg_len: float = AVG_DOC_LEN) -> None:
        self._k1 = k1
        self._b = b
        self._avg_len = avg_len

    def _encode(self, text: str, is_query: bool) -> SparseVector:
        tokens = tokenize(text)
        if not tokens:
            return SparseVector(indices=[], values=[])

        counts = Counter(tokens)
        if is_query:
            # queries carry no length normalisation, only presence weighting
            pairs = {_stable_hash(token): float(count) for token, count in counts.items()}
        else:
            length_norm = self._k1 * (1 - self._b + self._b * len(tokens) / self._avg_len)
            pairs = {}
            for token, count in counts.items():
                weight = (count * (self._k1 + 1)) / (count + length_norm)
                pairs[_stable_hash(token)] = weight

        indices = sorted(pairs)
        return SparseVector(indices=indices, values=[pairs[index] for index in indices])

    async def encode_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [self._encode(text, is_query=False) for text in texts]

    async def encode_query(self, text: str) -> SparseVector:
        return self._encode(text, is_query=True)


def idf(document_frequency: int, total_documents: int) -> float:
    """Standard BM25 idf. Used by the memory store, Qdrant computes its own."""
    if total_documents <= 0:
        return 0.0
    numerator = total_documents - document_frequency + 0.5
    denominator = document_frequency + 0.5
    return math.log(1 + numerator / denominator)
