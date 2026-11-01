import { createHash } from "node:crypto";
import { ts } from "ts-morph";

/**
 * SHA-256 over the declaration's tokens, joined by one space: whitespace and comments do not
 * change it, any token (a literal, a decorator, the signature) does.
 * @param {string} text declaration text (node.getText(): no leading comments)
 * @returns {string} hex digest
 */
export function bodyHash(text) {
  const scanner = ts.createScanner(ts.ScriptTarget.Latest, true, ts.LanguageVariant.Standard, text);
  const tokens = [];
  for (let kind = scanner.scan(); kind !== ts.SyntaxKind.EndOfFileToken; kind = scanner.scan()) {
    tokens.push(scanner.getTokenText());
  }
  return createHash("sha256").update(tokens.join(" ")).digest("hex");
}
