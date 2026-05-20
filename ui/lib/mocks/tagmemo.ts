/**
 * Empty data stubs for `/tagmemo` page (B5 prototype).
 * `summariseChunks` is kept as a pure aggregator (works on whatever is
 * passed in, returns zeros for empty input).
 *
 * TODO(B3-BE4 / B3-BE5): swap to `apiFetch<TagMemoChunk[]>(...)` once
 * the corlinman-tagmemo gateway endpoint + backfill land.
 */

export interface AxisProjection {
  label: string;
  energy: number;
  projection: number;
}

export interface PyramidLevel {
  axis_label: string;
  coefficient: number;
  explained_energy: number;
  cumulative_explained: number;
}

export interface TagMemoChunk {
  chunk_id: number;
  projections: number[];
  probabilities: number[];
  entropy: number;
  logic_depth: number;
  dominant_axes: AxisProjection[];
  pyramid_levels: PyramidLevel[];
  features: { tag_memo_activation: number; coverage: number };
}

export const MOCK_CHUNK_COUNT = 0;

export function generateMockChunks(
  _count: number = MOCK_CHUNK_COUNT,
): TagMemoChunk[] {
  void _count;
  return [];
}

export interface TagMemoStats {
  chunkCount: number;
  avgEntropy: number;
  avgLogicDepth: number;
  uniqueAxes: number;
}

export function summariseChunks(chunks: TagMemoChunk[]): TagMemoStats {
  if (chunks.length === 0) {
    return { chunkCount: 0, avgEntropy: 0, avgLogicDepth: 0, uniqueAxes: 0 };
  }
  let eSum = 0;
  let dSum = 0;
  const axes = new Set<string>();
  for (const c of chunks) {
    eSum += c.entropy;
    dSum += c.logic_depth;
    for (const a of c.dominant_axes) axes.add(a.label);
  }
  return {
    chunkCount: chunks.length,
    avgEntropy: eSum / chunks.length,
    avgLogicDepth: dSum / chunks.length,
    uniqueAxes: axes.size,
  };
}
