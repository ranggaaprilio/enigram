export async function list(slug?: string) {
  return slug ? [slug] : [];
}
