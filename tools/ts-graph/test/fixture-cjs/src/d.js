function first() {
  return 1;
}

const { y } = { y: () => 2 };

module.exports = { first, y };
