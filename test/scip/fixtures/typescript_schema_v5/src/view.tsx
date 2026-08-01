import { Runner } from "./types";

export function View(props: { runner: Runner }) {
  return <span>{props.runner.run("view")}</span>;
}
