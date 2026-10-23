const express = require("express");
const { OrderStore } = require("../db");

const router = express.Router();
const store = new OrderStore();

export function validateOrder(body) {
  return typeof body.item === "string" && body.quantity > 0;
}

router.post("/orders", (req, res) => {
  if (!validateOrder(req.body)) return res.status(422).json({ error: "bad order" });
  res.status(201).json(store.put({ id: String(Date.now()), ...req.body }));
});

router.get("/orders/:id", (req, res) => {
  const order = store.get(req.params.id);
  return order ? res.json(order) : res.status(404).end();
});

module.exports = { router, validateOrder };
