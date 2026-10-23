const express = require("express");
const orders = require("./routes/orders");

const app = express();
app.use(express.json());
app.get("/healthcheck", (req, res) => res.json({ ok: true }));
app.use("/api", orders.router);

function start(port) {
  return app.listen(port);
}

module.exports = { app, start };
