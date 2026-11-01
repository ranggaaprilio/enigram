import { ConfigService } from './config.service';
import { UserRepo } from './repo';

export class AppService {
  constructor(
    private readonly repo: UserRepo,
    private readonly config: ConfigService,
  ) {}

  find(id: string): string {
    const limit = Number(process.env.LIMIT);
    const db = this.config.get('db.type');
    this.helper();
    return `${id}:${limit}:${db}:${this.repo.load()}`;
  }

  private helper(): number {
    return 42;
  }
}
