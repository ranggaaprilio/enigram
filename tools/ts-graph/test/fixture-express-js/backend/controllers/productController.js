const getProducts = async (req, res) => res.json(list());
const getProductById = async (req, res) => res.json(list()[0]);
const createProduct = async (req, res) => res.json(list());

function list() {
  return [];
}

function remove(id) {
  return id;
}

export { createProduct, getProductById, getProducts, remove };
