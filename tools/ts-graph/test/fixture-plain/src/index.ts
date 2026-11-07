import { load } from "./load";

export function run(path: string) {
  return load(path).length;
}
