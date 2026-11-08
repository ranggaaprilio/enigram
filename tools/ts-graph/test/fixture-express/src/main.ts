import express from "express";
import routes from "./routes";

const app = express();
app.use(express.json());
app.use(routes);

app.get("/", (_req, res) => {
  res.send("ok");
});

app.listen(3000);
