function list() {
  return 1;
}

function other() {
  return 2;
}

const helper = () => 3;

module.exports = { list, other, helperAlias: helper };
