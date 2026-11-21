"""Text embeddings for C09 retrieval, behind a small port.

shortcut: ``HashingEmbedder`` is a deterministic, offline stand-in (feature-hashed
bag of words). Its scores are not calibrated like a real model's; swap in the
agreed embedding model (contract checklist item 15) behind the same port.
"""

import hashlib
import math
import re
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9]+")


class EmbeddingUnavailableError(Exception):
    """The embedding model cannot be reached; callers report Unavailable."""


class Embedder(Protocol):
    model_name: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one unit-length vector per text.

        Raises:
            EmbeddingUnavailableError: when the model cannot be reached.
        """
        ...


class HashingEmbedder:
    """Feature-hashed bag of words, L2-normalised. No network, no randomness."""

    def __init__(self, dimension: int = 256) -> None:
        self.dimension = dimension
        self.model_name = f"hashing-bow-{dimension} (stand-in)"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.sha1(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector


def cosine_score(a: list[float], b: list[float]) -> float:
    """Similarity of two unit vectors, clipped to the contract's 0..1 range."""
    return round(max(0.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True)))), 4)
