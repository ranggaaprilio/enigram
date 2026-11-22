import express from "express";
import { list } from "./service.js";

const app = express();

app.get("/health", (req, res) => {
  res.json(list());
});

export default app;
