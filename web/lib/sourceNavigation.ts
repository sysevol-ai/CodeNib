/** Select the last source fragment whose top crossed the reading threshold. */
export function sourceIndexAtThreshold(
  fragmentTops: readonly number[],
  threshold: number,
): number {
  if (fragmentTops.length === 0) return -1;

  let active = 0;
  for (let index = 0; index < fragmentTops.length; index += 1) {
    const top = fragmentTops[index];
    if (!Number.isFinite(top)) continue;
    if (top > threshold) break;
    active = index;
  }
  return active;
}
