import { auth } from "../../auth";

export async function GET(req: Request) {
  return new Response(String(auth(req)));
}

export const POST = async (req: Request) => {
  return new Response(String(auth(req)));
};

export const dynamic = "force-dynamic";
