"""Pure-Python HNSW approximate-nearest-neighbour index (G2 phase 2).

Zero new dependencies on purpose — no hnswlib, no faiss, no numpy: this
package's dependency set stays aiosqlite + httpx, and the corpora we
index (memory.sqlite chunk embeddings) are small enough that a readable
stdlib implementation beats carrying a native wheel across every deploy
target. The algorithm is the standard Hierarchical Navigable Small World
graph (Malkov & Yashunin 2016):

- layered skip-list structure: each node draws a top level from an
  exponential distribution (``-ln(U) * mL``); higher layers form coarser
  navigation graphs over the same points;
- greedy descent from the entry point through the upper layers, then a
  best-first beam search (``ef``) on layer 0;
- heuristic neighbour selection (Algorithm 4 of the paper, with the
  keep-pruned-connections variant) so links spread across directions
  instead of clustering on the nearest blob.

Metric: cosine. Vectors are L2-normalised at insert/search time so the
internal distance is ``1 - dot`` and reported scores are true cosine
similarities, directly comparable to
:func:`corlinman_memory_host.dense.cosine_similarity` on the brute path.

Determinism: level draws come from a per-index ``random.Random(seed)``,
so the same insertion sequence always yields the same graph and the same
search results — recall tests are exactly reproducible.

The index is in-memory only and append-only: there is deliberately no
``remove``. Callers (``local_sqlite._SqliteStore``) treat deletions and
re-embeddings as coarse staleness signals and rebuild when drift exceeds
a threshold, re-scoring candidates against the live store so a stale
graph entry can never surface wrong data.
"""

from __future__ import annotations

import heapq
import math
import random
from collections.abc import Sequence

#: Max links per node on the upper layers (layer 0 gets ``2 * M``) — the
#: paper's sweet spot for recall/size on small-to-medium corpora.
DEFAULT_M = 16
#: Beam width while building. Higher = better graph, slower inserts.
DEFAULT_EF_CONSTRUCTION = 100
#: Beam width while searching. Sized for recall@10 >= 0.9 on the corpus
#: sizes the memory host sees (thousands of chunks).
DEFAULT_EF_SEARCH = 96

#: Cap on the drawn level — with M=16 the odds of exceeding this are
#: astronomically small; the cap just bounds pathological RNG draws.
_MAX_LEVEL = 32

_DEFAULT_SEED = 0x5EED


class HnswIndex:
    """In-memory cosine HNSW over ``(int id, vector)`` pairs.

    Not thread-safe; built for a single asyncio event loop. ``add`` is
    O(ef_construction * log n) distance evaluations, ``search`` is
    O(ef * log n).
    """

    def __init__(
        self,
        dim: int,
        *,
        m: int = DEFAULT_M,
        ef_construction: int = DEFAULT_EF_CONSTRUCTION,
        ef_search: int = DEFAULT_EF_SEARCH,
        seed: int = _DEFAULT_SEED,
    ) -> None:
        if dim <= 0:
            raise ValueError(f"HnswIndex: dim must be positive, got {dim}")
        self._dim = dim
        self._m = max(2, m)
        self._m0 = self._m * 2
        self._ef_construction = max(self._m, ef_construction)
        self._ef_search = max(1, ef_search)
        # mL = 1/ln(M) — the level-generation factor from the paper.
        self._ml = 1.0 / math.log(self._m)
        self._rng = random.Random(seed)
        # Parallel per-node arrays (internal node id = list index).
        self._vectors: list[tuple[float, ...]] = []
        self._external_ids: list[int] = []
        self._id_to_node: dict[int, int] = {}
        # node -> level -> list of neighbour node ids. A node's list has
        # ``top_level + 1`` entries; links at level L only ever reference
        # nodes whose top level is >= L.
        self._links: list[list[list[int]]] = []
        self._entry = -1
        self._max_level = -1

    # ---- introspection -----------------------------------------------------

    @property
    def dim(self) -> int:
        return self._dim

    def __len__(self) -> int:
        return len(self._external_ids)

    def __contains__(self, item_id: int) -> bool:
        return item_id in self._id_to_node

    # ---- distances ---------------------------------------------------------

    @staticmethod
    def _normalize(vector: Sequence[float]) -> tuple[float, ...] | None:
        norm_sq = 0.0
        for x in vector:
            norm_sq += x * x
        if norm_sq <= 0.0 or not math.isfinite(norm_sq):
            return None
        inv = 1.0 / math.sqrt(norm_sq)
        return tuple(x * inv for x in vector)

    def _dist(self, node: int, q: tuple[float, ...]) -> float:
        """Cosine distance between stored ``node`` and normalized ``q``."""
        dot = 0.0
        for x, y in zip(self._vectors[node], q, strict=True):
            dot += x * y
        return 1.0 - dot

    def _node_dist(self, a: int, b: int) -> float:
        return self._dist(a, self._vectors[b])

    # ---- construction ------------------------------------------------------

    def add(self, item_id: int, vector: Sequence[float]) -> bool:
        """Insert one item. Returns ``False`` (never raises) when the item
        can't participate: wrong dimension, duplicate id, or a zero-norm /
        non-finite vector (which cosine can't rank anyway)."""
        if len(vector) != self._dim or item_id in self._id_to_node:
            return False
        q = self._normalize(vector)
        if q is None:
            return False

        node = len(self._vectors)
        r = self._rng.random()
        level = min(int(-math.log(r if r > 0.0 else 1e-12) * self._ml), _MAX_LEVEL)
        self._vectors.append(q)
        self._external_ids.append(item_id)
        self._id_to_node[item_id] = node
        self._links.append([[] for _ in range(level + 1)])

        if self._entry < 0:
            self._entry = node
            self._max_level = level
            return True

        ep = self._entry
        # Greedy 1-NN descent through the layers above the new node's level.
        for lvl in range(self._max_level, level, -1):
            ep = self._greedy_closest(q, ep, lvl)

        # Beam-search + heuristic linking on the shared layers.
        for lvl in range(min(level, self._max_level), -1, -1):
            candidates = self._search_layer(q, ep, self._ef_construction, lvl)
            neighbors = self._select_neighbors(q, candidates, self._m)
            self._links[node][lvl] = [n for _, n in neighbors]
            max_links = self._m0 if lvl == 0 else self._m
            for _, n in neighbors:
                links_n = self._links[n][lvl]
                links_n.append(node)
                if len(links_n) > max_links:
                    self._prune(n, lvl, max_links)
            ep = candidates[0][1]

        if level > self._max_level:
            self._max_level = level
            self._entry = node
        return True

    def _greedy_closest(self, q: tuple[float, ...], ep: int, level: int) -> int:
        best = ep
        best_d = self._dist(ep, q)
        improved = True
        while improved:
            improved = False
            for n in self._links[best][level]:
                d = self._dist(n, q)
                if d < best_d:
                    best = n
                    best_d = d
                    improved = True
        return best

    def _search_layer(
        self,
        q: tuple[float, ...],
        entry_point: int,
        ef: int,
        level: int,
    ) -> list[tuple[float, int]]:
        """Best-first beam search on one layer. Returns up to ``ef``
        ``(distance, node)`` pairs sorted ascending by distance (ties break
        on the internal node id, so the result is deterministic)."""
        d0 = self._dist(entry_point, q)
        visited = {entry_point}
        # Min-heap of frontier candidates; max-heap (negated) of results.
        candidates: list[tuple[float, int]] = [(d0, entry_point)]
        results: list[tuple[float, int]] = [(-d0, entry_point)]
        while candidates:
            d, c = heapq.heappop(candidates)
            if d > -results[0][0] and len(results) >= ef:
                break
            for n in self._links[c][level]:
                if n in visited:
                    continue
                visited.add(n)
                d_n = self._dist(n, q)
                if len(results) < ef or d_n < -results[0][0]:
                    heapq.heappush(candidates, (d_n, n))
                    heapq.heappush(results, (-d_n, n))
                    if len(results) > ef:
                        heapq.heappop(results)
        return sorted((-neg_d, n) for neg_d, n in results)

    def _select_neighbors(
        self,
        q: tuple[float, ...],
        candidates: list[tuple[float, int]],
        m: int,
    ) -> list[tuple[float, int]]:
        """Heuristic selection (paper Algorithm 4): a candidate is kept only
        if it is closer to ``q`` than to every already-selected neighbour —
        this favours links that span distinct directions. Pruned candidates
        backfill remaining slots (keepPrunedConnections) so low-degree
        nodes still reach ``m`` links."""
        selected: list[tuple[float, int]] = []
        discarded: list[tuple[float, int]] = []
        for d_q, node in candidates:  # candidates arrive sorted by d_q
            if len(selected) >= m:
                break
            diverse = True
            for _, s in selected:
                if self._node_dist(node, s) < d_q:
                    diverse = False
                    break
            if diverse:
                selected.append((d_q, node))
            else:
                discarded.append((d_q, node))
        for d_q, node in discarded:
            if len(selected) >= m:
                break
            selected.append((d_q, node))
        return selected

    def _prune(self, node: int, level: int, max_links: int) -> None:
        q = self._vectors[node]
        current = sorted((self._dist(n, q), n) for n in self._links[node][level])
        self._links[node][level] = [
            n for _, n in self._select_neighbors(q, current, max_links)
        ]

    # ---- search ------------------------------------------------------------

    def search(
        self,
        vector: Sequence[float],
        k: int,
        *,
        ef: int | None = None,
    ) -> list[tuple[int, float]]:
        """Return up to ``k`` ``(external_id, cosine_similarity)`` pairs,
        best first with an ascending-id tie-break — the same ordering
        contract as the brute-force ``search_dense`` path. Empty on an
        empty index, a dimension mismatch, or a degenerate query vector."""
        if k <= 0 or self._entry < 0 or len(vector) != self._dim:
            return []
        q = self._normalize(vector)
        if q is None:
            return []
        ep = self._entry
        for lvl in range(self._max_level, 0, -1):
            ep = self._greedy_closest(q, ep, lvl)
        beam = max(self._ef_search if ef is None else ef, k)
        found = self._search_layer(q, ep, beam, 0)
        out = [(self._external_ids[n], 1.0 - d) for d, n in found[:k]]
        out.sort(key=lambda pair: (-pair[1], pair[0]))
        return out


__all__ = [
    "DEFAULT_EF_CONSTRUCTION",
    "DEFAULT_EF_SEARCH",
    "DEFAULT_M",
    "HnswIndex",
]
