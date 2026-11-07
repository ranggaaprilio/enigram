export function auth(req: Request) {
  return req.headers.get("authorization") !== null;
}
