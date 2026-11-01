export interface UserRepo {
  load(): string;
}

export class SqlUserRepo implements UserRepo {
  load(): string {
    return 'row';
  }
}
