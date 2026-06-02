// cytoscape-dagre ships no type declarations; it's a cytoscape layout plugin
// registered via cytoscape.use(). A module shim is enough for our usage.
declare module "cytoscape-dagre" {
  import type { Ext } from "cytoscape";
  const ext: Ext;
  export default ext;
}
