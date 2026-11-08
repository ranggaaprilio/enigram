import { Router } from "express";
import articles from "./articles";
import { userRouter } from "./users/userRouter";

// A chain of mounts, as the RealWorld example writes it.
const api = Router().use(articles);

export default Router().use("/api", api).use("/users", userRouter);
