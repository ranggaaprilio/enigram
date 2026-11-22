export function list() {
  return normalise([3, 1, 2]);
}

function normalise(items) {
  return [...items].sort();
}
