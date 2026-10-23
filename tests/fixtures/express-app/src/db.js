const rows = new Map();

class OrderStore {
  get(id) { return rows.get(id); }
  put(order) { rows.set(order.id, order); return order; }
}

module.exports = { OrderStore };
