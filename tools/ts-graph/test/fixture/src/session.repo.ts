export abstract class SessionRepo {
  abstract findById(id: string): string;
}

export class SqlSessionRepo extends SessionRepo {
  findById(id: string): string {
    return id;
  }
}

export class SessionService {
  constructor(private readonly sessions: SessionRepo) {}

  get(id: string): string {
    return this.sessions.findById(id);
  }
}
