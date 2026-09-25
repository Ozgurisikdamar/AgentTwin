"""Text embeddings behind a provider interface (spec §22 "Semantic scenario
matching", §49 "Embeddings", §50).

``HashingEmbedder`` is the local default: deterministic, dependency-free and
fast. It is *lexical*: a text becomes a signed feature-hashed bag of words
and word pairs, so two texts are similar when they share (stemmed) words and
identifiers — "refunds above the limit" and "refund_payment over the refund
limit" are, "reimbursement" and "refund" are not. A hosted provider can give
semantic similarity; it plugs in behind the same interface. Every vector is
stored with the ``model`` that produced it and only vectors of the same model
are ever compared.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

__all__ = [
    "HASHING_DIMS",
    "HASHING_MODEL",
    "MAX_TEXT_CHARS",
    "EmbeddingProvider",
    "HashingEmbedder",
    "cosine",
    "features",
    "is_zero",
    "vector_literal",
]

HASHING_MODEL = "hashing-v1"
HASHING_DIMS = 256
# Longer text is cut: what an embedding describes is its beginning.
MAX_TEXT_CHARS = 20_000


class EmbeddingProvider(Protocol):
    """Turns texts into vectors of one space.

    ``model`` names the space (a vector of another model is never compared
    with one of this); ``dims`` is the length of every vector. A text without
    anything to embed yields the zero vector."""

    model: str
    dims: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# Words that carry no meaning of their own in the texts compared here.
# fmt: off
_STOPWORDS = frozenset((
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "can", "could",
    "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from", "further", "had", "has",
    "have", "having", "he", "her", "here", "hers", "him", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "itself", "just", "me", "more", "most", "my", "no", "nor", "not", "now", "of", "off",
    "on", "once", "only", "or", "other", "our", "ours", "out", "over", "own", "same", "she", "should",
    "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "then", "there", "these",
    "they", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "we", "were",
    "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would", "you",
    "your", "yours", "must", "may", "might", "shall", "also", "via", "per",
))
# fmt: on

# Words (letters and digits), joined into one identifier by "_", "-" or ".".
_WORD = re.compile(r"[^\W_]+(?:[_.\-][^\W_]+)*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_JOINERS = re.compile(r"[_.\-]")

# Weight of each kind of feature: a pair of words says less than a word.
_WEIGHT = {"w": 1.0, "c": 1.0, "b": 0.5}


def _stem(word: str) -> str:
    """A light suffix stripper: "refunds", "refunded" and "refunding" are
    "refund"; "policies" is "policy". Both sides of a comparison are stemmed
    the same way, so the stems only need to agree, not to be words."""
    if len(word) > 5 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith(("ed", "sses", "xes", "zes", "ches", "shes")):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def _keep(word: str) -> bool:
    return len(word) > 1 and word not in _STOPWORDS


def features(text: str) -> Counter[str]:
    """The weighted features of a text: stemmed words (``w:``), joined
    identifiers as a whole (``c:``, "refund_payment" besides "refund" and
    "payment") and pairs of neighbouring words (``b:``)."""
    text = unicodedata.normalize("NFKC", text[:MAX_TEXT_CHARS])
    text = _CAMEL.sub("_", text).casefold()
    out: Counter[str] = Counter()
    words: list[str] = []
    for m in _WORD.finditer(text):
        parts = [_stem(p) for p in _JOINERS.split(m.group()) if p]
        kept = [p for p in parts if _keep(p)]
        if len(parts) > 1 and kept:
            out["c:" + "_".join(parts)] += 1
        for p in kept:
            out["w:" + p] += 1
        words.extend(kept)
    for a, b in itertools.pairwise(words):
        out["b:" + a + " " + b] += 1
    return out


def _slot(feature: str, dims: int) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
    return h % dims, 1.0 if (h >> 63) & 1 else -1.0


class HashingEmbedder:
    """Signed feature hashing of :func:`features` with sublinear term
    frequency, L2-normalized. Deterministic across processes and versions of
    Python (no ``hash()``)."""

    model = HASHING_MODEL

    def __init__(self, dims: int = HASHING_DIMS) -> None:
        if dims < 16:
            raise ValueError("dims must be at least 16")
        self.dims = dims

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for feature, count in features(text).items():
            index, sign = _slot(feature, self.dims)
            vec[index] += sign * _WEIGHT[feature[0]] * (1.0 + math.log(count))
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


def is_zero(vec: Sequence[float]) -> bool:
    return not any(vec)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """The cosine similarity of two vectors of one model (0 when either is zero)."""
    if len(a) != len(b):
        raise ValueError("vectors of different dimensions")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def vector_literal(vec: Sequence[float]) -> str:
    """The pgvector text form of a vector (``'[0.1,-0.2,…]'``)."""
    return "[" + ",".join(format(x, ".7g") for x in vec) + "]"
