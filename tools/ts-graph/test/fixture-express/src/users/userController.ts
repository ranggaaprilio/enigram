import { list } from "../service";

class UserController {
  public getUser = async (req, res) => {
    res.json(await list(req.params.id));
  };

  public create = async (_req, res) => {
    res.status(201).send();
  };
}

export const userController = new UserController();
