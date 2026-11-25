// Property check for the map router: on many random maps, no line segment
// may cross a box other than the two it connects, and no boxes overlap.
//
//   node --experimental-strip-types tools/route-check.mts
import { BOX_H, routeMap, type MapColumn, type MapLink } from "../src/lib/mapRoute.ts";

let seed = 12345;
const random = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
const pick = (n: number) => Math.floor(random() * n);

let failures = 0;
let lines = 0;
for (let trial = 0; trial < 2000; trial++) {
  const columns: MapColumn[] = Array.from({ length: 1 + pick(5) }, (_, c) => ({
    head: random() < 0.7 ? { title: `area ${c}`, meta: "", hue: "" } : undefined,
    boxes: Array.from({ length: 1 + pick(6) }, (_, r) => ({ id: `${c}-${r}`, title: "", meta: "", hue: "", page: "", here: false })),
  }));
  const ids = columns.flatMap((c) => c.boxes.map((b) => b.id));
  const links: MapLink[] = Array.from({ length: pick(24) }, () => ({
    source: ids[pick(ids.length)],
    target: ids[pick(ids.length)],
    calls: 1 + pick(200),
  }));
  const map = routeMap(columns, links, 700 + pick(600));
  const boxes = map.placed.map((p) => ({ id: p.box.id, left: p.x, right: p.x + map.boxW, top: p.y, bottom: p.y + BOX_H }));
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i];
      const b = boxes[j];
      if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) {
        failures++;
        console.log(`trial ${trial}: boxes ${a.id} and ${b.id} overlap`);
      }
    }
  for (const route of map.routes) {
    lines++;
    const ends = new Set([route.link.source, route.link.target]);
    for (let i = 1; i < route.points.length; i++) {
      const [x1, y1] = route.points[i - 1];
      const [x2, y2] = route.points[i];
      if (x1 !== x2 && y1 !== y2) {
        failures++;
        console.log(`trial ${trial}: a diagonal segment`);
      }
      const seg = { left: Math.min(x1, x2), right: Math.max(x1, x2), top: Math.min(y1, y2), bottom: Math.max(y1, y2) };
      for (const box of boxes) {
        if (ends.has(box.id)) continue;
        if (seg.left < box.right && seg.right > box.left && seg.top < box.bottom && seg.bottom > box.top) {
          failures++;
          if (failures < 10) console.log(`trial ${trial}: ${route.link.source}>${route.link.target} crosses ${box.id}`);
        }
      }
    }
  }
}
console.log(failures ? `${failures} failures in ${lines} lines` : `2000 random maps, ${lines} lines: no line crosses a box, no boxes overlap`);
process.exitCode = failures ? 1 : 0;
