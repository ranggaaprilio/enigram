import { Router } from "express";
import { list } from "./service";

const router = Router();

function auth(_req, _res, next) {
  next();
}

router.get("/articles", auth, async (_req, res) => {
  res.json(await list());
});

router.delete("/articles/:slug", auth, async (req, res) => {
  res.json(await list(req.params.slug));
});

export default router;
