function Model() {}

Model.prototype.save = function save() {
  return persist();
};

function persist() {
  return true;
}

module.exports = Model;
