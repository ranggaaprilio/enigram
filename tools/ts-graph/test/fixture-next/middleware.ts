import { auth } from "./app/auth";

export default function middleware(req: Request) {
  return auth(req);
}
