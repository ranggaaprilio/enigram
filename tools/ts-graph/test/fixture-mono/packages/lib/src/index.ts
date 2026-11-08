import { format } from "./format";

function clean(text: string): string {
  return format(text).toLowerCase();
}

export function run(text: string): string {
  return clean(text);
}
