const getUser = async (req, res) => res.send(find());
const deleteUser = async (req, res) => res.send(find());

function find() {
  return {};
}

module.exports = { getUser, deleteUser };
