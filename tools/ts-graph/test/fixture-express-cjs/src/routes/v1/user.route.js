const { Router } = require("express");
const { userController } = require("../../controllers");

const router = Router();

router.route("/:userId").get(userController.getUser).delete(userController.deleteUser);

module.exports = router;
