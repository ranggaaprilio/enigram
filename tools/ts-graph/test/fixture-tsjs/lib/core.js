import { merge } from "./helpers.js";

export function request(config) {
  return merge({ method: "get" }, config);
}
