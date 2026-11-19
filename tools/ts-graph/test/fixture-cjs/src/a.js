const { list } = require("./b");
const b = require("./b");

exports.run = function run() {
  return list() + b.other();
};

module.exports.two = () => b.helperAlias();
