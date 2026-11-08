import express, { type Router } from "express";
import { userController } from "./userController";

export const userRouter: Router = express.Router();

userRouter.get("/:id", userController.getUser);
userRouter.post("/", userController.create);
