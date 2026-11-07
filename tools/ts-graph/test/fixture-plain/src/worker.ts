import { load } from "./load";

export async function tick() {
  return load("/tmp/queue");
}
