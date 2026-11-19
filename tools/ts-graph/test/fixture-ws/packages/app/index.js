import { fmt } from "@ws/util";
import { twice } from "src/lib/math";
import chunk from "lodash";

export function start() {
  return fmt(twice(2)) + chunk([1]);
}
