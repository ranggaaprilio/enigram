import express from "express";
import { createProduct, getProductById, getProducts, remove } from "../controllers/productController.js";
import { admin, protect } from "../middleware/auth.js";

const router = express.Router();

router.route("/").get(getProducts).post(protect, admin, createProduct);
router
  .route("/:id")
  .get(getProductById)
  .delete(protect, (req, res) => {
    res.json(remove(req.params.id));
  });
router.get("/top", getProducts);

export default router;
