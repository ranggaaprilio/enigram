import * as bcrypt from 'bcrypt';

export async function check(plain: string, hash: string): Promise<boolean> {
  return bcrypt.compare(plain, hash);
}
