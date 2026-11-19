const express = require("express");
const routes = require("./routes/v1");

const app = express();
app.use("/v1", routes);
app.use("/health", require("./routes/health"));

module.exports = app;
