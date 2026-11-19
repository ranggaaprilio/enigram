const models = require("./models");

exports.make = function make() {
  return new models.Model();
};
