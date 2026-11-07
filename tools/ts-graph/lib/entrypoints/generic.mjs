import { defaultExportSymbol, isFunctionValued } from "./common.mjs";

/**
 * Fallback entry points for a repository no framework adapter matched: exported top-level
 * functions (an `export default X` alias counts as exported) that nothing in the repository
 * calls and that call at least one repository symbol themselves. The last rule drops leaf
 * utilities and markup-only components, which would otherwise be most of a React repository.
 * @param {{symbols: {row: object, node: import("ts-morph").Node}[], edges: object[]}} input
 */
export function roots({ symbols, edges }) {
  const called = new Set();
  const calling = new Set();
  for (const e of edges) {
    if (e.kind !== "calls" || e.src === e.dst) continue; // recursion is not a caller
    called.add(e.dst);
    calling.add(e.src);
  }
  const byFile = new Map();
  for (const sym of symbols) {
    if (!byFile.has(sym.row.file)) byFile.set(sym.row.file, []);
    byFile.get(sym.row.file).push(sym);
  }
  const defaults = new Set();
  for (const fileSymbols of byFile.values()) {
    const sym = defaultExportSymbol(fileSymbols);
    if (sym) defaults.add(sym.row.id);
  }
  const entrypoints = symbols
    .filter((s) => !s.row.parent && isFunctionValued(s) && (s.row.exported || defaults.has(s.row.id)))
    .filter((s) => !called.has(s.row.id) && calling.has(s.row.id))
    .map((s) => ({ symbol: s.row.id, kind: "root", route: s.row.qualified_name, framework: "generic", version: "" }));
  return { entrypoints, matched: entrypoints.length };
}
