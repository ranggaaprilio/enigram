export interface Options {
  retries: number;
}

export function defaults(): Options {
  return { retries: 3 };
}
