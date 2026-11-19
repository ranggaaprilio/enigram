const wrap = (fn) => fn;

module.exports = wrap(function main() {
  return require("./a").run();
});
