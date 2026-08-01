export interface Runner {
  kind: string;
  run(): string;
}

export type RunnerId = string;

export enum Mode {
  Fast = "fast",
  Safe = "safe",
}

export class DefaultRunner implements Runner {
  run(): string {
    return Mode.Fast;
  }
}
