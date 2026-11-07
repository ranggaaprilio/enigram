export function load(path: string) {
  return format(path).split("/");
}

export function format(path: string) {
  return path.trim();
}
