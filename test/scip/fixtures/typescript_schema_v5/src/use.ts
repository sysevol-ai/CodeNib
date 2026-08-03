import { DefaultRunner, type Runner, type RunnerId } from "./types";
import type { MissingType } from "./missing";

export function execute(id: RunnerId, runner: Runner = new DefaultRunner()): string {
  return `${id}:${runner.run()}`;
}
