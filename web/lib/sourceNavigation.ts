/** Select the last source fragment whose top crossed the reading threshold. */
export function sourceIndexAtThreshold(
  fragmentTops: readonly number[],
  threshold: number,
  atEnd = false,
): number {
  if (fragmentTops.length === 0) return -1;

  if (atEnd) {
    for (let index = fragmentTops.length - 1; index >= 0; index -= 1) {
      if (Number.isFinite(fragmentTops[index])) return index;
    }
    return -1;
  }

  let active = 0;
  for (let index = 0; index < fragmentTops.length; index += 1) {
    const top = fragmentTops[index];
    if (!Number.isFinite(top)) continue;
    if (top > threshold) break;
    active = index;
  }
  return active;
}
