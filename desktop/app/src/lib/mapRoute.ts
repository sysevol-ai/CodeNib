// Lay out a map of areas and route its lines so that no line passes through
// or under a box it does not connect. Boxes sit in a grid, one column per
// area; every line runs only in the gutters between columns and the channels
// between rows, each on its own lane. Pure: no React, so it can be tested.

export interface Anchor {
  file: string;
  line: number;
}

export interface MapBox {
  id: string;
  title: string;
  meta: string;
  hue: string;
  page: string;
  here: boolean;
}

export interface MapColumn {
  /** The area a column holds; absent when each box is an area itself. */
  head?: { title: string; meta: string; hue: string };
  boxes: MapBox[];
}

export interface MapLink {
  source: string;
  target: string;
  calls: number;
  example?: Anchor;
}

export type Point = [number, number];

export interface Routed {
  link: MapLink;
  points: Point[];
  hue: string;
  width: number;
}

// Gutters between columns and channels between rows carry every line, so no
// line can pass through or under a box it does not connect.
const MIN_GUTTER = 56;
const MAX_GUTTER = 88;
const CHANNEL = 36;
export const BOX_H = 64;
const HEAD = 40;

export function routeMap(columns: MapColumn[], links: MapLink[], available: number) {
  const count = Math.max(1, columns.length);
  // Wider gutters when there is room: they carry every line.
  const GUTTER = Math.max(MIN_GUTTER, Math.min(MAX_GUTTER, (available - count * 220) / (count + 1)));
  const boxW = Math.max(150, Math.min(300, (available - (count + 1) * GUTTER) / count));
  const rows = Math.max(1, ...columns.map((c) => c.boxes.length));
  const head = columns.some((c) => c.head) ? HEAD : 0;
  const colX = (c: number) => GUTTER + c * (boxW + GUTTER);
  const rowY = (r: number) => head + CHANNEL + r * (BOX_H + CHANNEL);
  const gutterX = (k: number) => k * (boxW + GUTTER);
  const channelY = (q: number) => head + q * (BOX_H + CHANNEL);
  const placed = new Map<string, { box: MapBox; col: number; row: number; x: number; y: number }>();
  columns.forEach((c, col) => c.boxes.forEach((box, row) => placed.set(box.id, { box, col, row, x: colX(col), y: rowY(row) })));
  type Place = NonNullable<ReturnType<typeof placed.get>>;
  const max = Math.max(1, ...links.map((l) => l.calls));

  interface Plan {
    link: MapLink;
    a: Place;
    b: Place;
    out: "l" | "r";
    in: "l" | "r";
    gA: number;
    gB: number;
    ya: number;
    yb: number;
    channel: number | null;
  }
  const plans: Plan[] = [];
  const ordered = [...links].sort(
    (x, y) => y.calls - x.calls || `${x.source}>${x.target}`.localeCompare(`${y.source}>${y.target}`),
  );
  const load = new Map<number, number>();
  const use = (g: number) => load.set(g, (load.get(g) ?? 0) + 1);
  const within: MapLink[] = [];
  for (const link of ordered) {
    const a = placed.get(link.source);
    const b = placed.get(link.target);
    if (!a || !b || a === b) continue;
    if (a.col === b.col) within.push(link);
    else if (b.col > a.col) {
      plans.push({ link, a, b, out: "r", in: "l", gA: a.col + 1, gB: b.col, ya: 0, yb: 0, channel: null });
      use(a.col + 1);
      use(b.col);
    } else {
      plans.push({ link, a, b, out: "l", in: "r", gA: a.col, gB: b.col + 1, ya: 0, yb: 0, channel: null });
      use(a.col);
      use(b.col + 1);
    }
  }
  // A line inside one area takes the quieter of the column's two gutters.
  for (const link of within) {
    const a = placed.get(link.source)!;
    const b = placed.get(link.target)!;
    const left = (load.get(a.col) ?? 0) < (load.get(a.col + 1) ?? 0);
    const g = left ? a.col : a.col + 1;
    const side = left ? "l" : "r";
    plans.push({ link, a, b, out: side, in: side, gA: g, gB: g, ya: 0, yb: 0, channel: null });
    use(g);
  }

  // Ports: the lines on one side of a box are spread along it, ordered by
  // the height of their other end so they do not cross at the box.
  const sides = new Map<string, { plan: Plan; end: "a" | "b" }[]>();
  for (const plan of plans) {
    for (const end of ["a", "b"] as const) {
      const p = plan[end];
      const key = `${p.box.id}:${end === "a" ? plan.out : plan.in}`;
      sides.set(key, [...(sides.get(key) ?? []), { plan, end }]);
    }
  }
  for (const list of sides.values()) {
    const other = (x: { plan: Plan; end: "a" | "b" }) => (x.end === "a" ? x.plan.b : x.plan.a);
    list.sort((x, y) => other(x).y - other(y).y || other(x).x - other(y).x);
    list.forEach((x, i) => {
      const p = x.plan[x.end];
      const y = p.y + (BOX_H * (i + 1)) / (list.length + 1);
      if (x.end === "a") x.plan.ya = y;
      else x.plan.yb = y;
    });
  }

  // A line that changes gutter crosses in the gap between rows nearest both ends.
  for (const plan of plans) {
    if (plan.gA === plan.gB) continue;
    let best = 0;
    let cost = Infinity;
    for (let q = 0; q <= rows; q++) {
      const mid = channelY(q) + CHANNEL / 2;
      const c = Math.abs(mid - plan.ya) + Math.abs(mid - plan.yb);
      if (c < cost - 0.01) {
        cost = c;
        best = q;
      }
    }
    plan.channel = best;
  }

  // Lanes: each line gets its own track in every gutter and channel it uses.
  const lanes = new Map<string, Plan[]>();
  const lane = (key: string, plan: Plan) => lanes.set(key, [...(lanes.get(key) ?? []), plan]);
  for (const plan of plans) {
    lane(`g${plan.gA}`, plan);
    if (plan.gB !== plan.gA) lane(`g${plan.gB}`, plan);
    if (plan.channel !== null) lane(`c${plan.channel}`, plan);
  }
  const track = (key: string, plan: Plan, start: number, span: number) => {
    const list = lanes.get(key) ?? [plan];
    const i = list.indexOf(plan);
    return start + 8 + ((span - 16) * (i + 0.5)) / list.length;
  };

  const routes: Routed[] = plans.map((plan) => {
    const xa = plan.out === "r" ? plan.a.x + boxW : plan.a.x;
    const xb = plan.in === "r" ? plan.b.x + boxW : plan.b.x;
    const ga = track(`g${plan.gA}`, plan, gutterX(plan.gA), GUTTER);
    let points: Point[];
    if (plan.channel === null) {
      points = [
        [xa, plan.ya],
        [ga, plan.ya],
        [ga, plan.yb],
        [xb, plan.yb],
      ];
    } else {
      const gb = track(`g${plan.gB}`, plan, gutterX(plan.gB), GUTTER);
      const cy = track(`c${plan.channel}`, plan, channelY(plan.channel), CHANNEL);
      points = [
        [xa, plan.ya],
        [ga, plan.ya],
        [ga, cy],
        [gb, cy],
        [gb, plan.yb],
        [xb, plan.yb],
      ];
    }
    return {
      link: plan.link,
      points,
      hue: plan.a.box.hue,
      width: 1.5 + 3.5 * Math.sqrt(plan.link.calls / max),
    };
  });

  return {
    boxW,
    head,
    gutter: GUTTER,
    width: count * boxW + (count + 1) * GUTTER,
    height: rowY(rows - 1) + BOX_H + CHANNEL,
    placed: [...placed.values()],
    routes,
    colX,
  };
}
