const { validateOrder } = require("../src/routes/orders");

test("accepts a real order", () => {
  expect(validateOrder({ item: "bread", quantity: 2 })).toBe(true);
});

it("refuses zero quantity", () => {
  expect(validateOrder({ item: "bread", quantity: 0 })).toBe(false);
});
