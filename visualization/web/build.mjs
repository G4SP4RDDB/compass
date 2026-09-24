// Builds visualization/web/graph_template.html from src/**/*.ts + styles/*.css
// + index.template.html. This is the *only* build step for the graph viewer —
// there is no dev server: edit the TS/CSS, run `npm run build`, then reload
// graph.html via the existing Python pipeline
// (visualization/web_view.py::renderGraphHtml, python -m visualization.server).
//
// Why not esbuild for this: esbuild's printer drops essentially all regular
// `//`/`/* */` comments, during both bundling AND single-file transforms
// (verified empirically — it's not a config gap, esbuild just doesn't keep
// them). This codebase relies heavily on WHY-comments (see web_view.py's
// extensive docstrings), so that trade is unacceptable. `tsc` preserves
// comments faithfully, so it does the actual TS -> JS compilation (and the
// type-checking — `noEmitOnError` in tsconfig.json makes a type error fail
// the build loudly). The only thing tsc doesn't do is combine the compiled
// per-file JS into the single inline <script> the page needs; this script
// does that part itself, by topologically ordering modules from their
// `import` statements and stripping `import`/`export` syntax textually
// (regex-level, not a re-parse — comments already survived tsc's emit and
// this step never touches them).
//
// The output graph_template.html stays a single self-contained file (inlined
// <style>/<script>, no external asset requests) — see the plan's "hard
// constraint" note: web_view.py/server.py must not need to change.

import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, rmSync, statSync, watch, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const isWatch = process.argv.includes("--watch");
const srcDir = join(here, "src");
const distDir = join(here, ".dist-ts");
const stylesDir = join(here, "styles");
const shellPath = join(here, "index.template.html");
const outPath = join(here, "graph_template.html");

// CSS partials are concatenated in this explicit order (cascade order
// matters for CSS, unlike the JS dependency graph below, so this can't be
// auto-discovered the same way). Phase A has a single file; Phase A.5 (see
// the plan) replaces this with the real partial list.
const CSS_FILES = ["main.css"];

function compileTypeScript() {
  if (existsSync(distDir)) rmSync(distDir, { recursive: true, force: true });
  const tscBin = join(here, "node_modules", ".bin", "tsc");
  execFileSync(tscBin, ["-p", join(here, "tsconfig.json")], { cwd: here, stdio: "inherit" });
}

function listTsFiles(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...listTsFiles(full));
    else if (entry.endsWith(".ts")) out.push(full);
  }
  return out;
}

// module key = path relative to srcDir, without extension, e.g. "graph/layout"
function moduleKey(absTsPath) {
  return relative(srcDir, absTsPath).replace(/\.ts$/, "");
}

const IMPORT_RE = /^import\s+(?:type\s+)?(?:\{[^}]*\}|\*\s+as\s+\w+|\w+)\s+from\s+["'](\.[^"']+)["'];?\s*$/gm;

function importedModuleKeys(tsSource, fromTsPath) {
  const keys = [];
  for (const match of tsSource.matchAll(IMPORT_RE)) {
    const specifier = match[1];
    const absTarget = resolve(dirname(fromTsPath), specifier);
    keys.push(relative(srcDir, absTarget).replace(/\.ts$/, ""));
  }
  return keys;
}

// `import { x as y }` type-checks fine under tsc (each file is its own real
// module there) but is silently WRONG once flattened: bundleJs() strips the
// whole import line, so the local alias `y` never gets bound to anything —
// any use of `y` becomes a `ReferenceError` at runtime (found the hard way
// once, via jsdom actually executing the page — see stripModuleSyntax's
// comment for the sibling issues found the same way). Caught here instead,
// loudly, at build time.
const IMPORT_ALIAS_RE = /^import\s+(?:type\s+)?\{[^}]*\bas\b[^}]*\}\s+from\s+["']\.[^"']+["'];?\s*$/gm;

function checkForImportAliases(allTsFiles) {
  const offenders = [];
  for (const tsPath of allTsFiles) {
    const source = readFileSync(tsPath, "utf-8");
    if (IMPORT_ALIAS_RE.test(source)) offenders.push(relative(srcDir, tsPath));
  }
  if (offenders.length > 0) {
    throw new Error(
      `"import { x as y }" aliasing doesn't survive this build's flat concatenation (the alias only existed in the stripped import line) — rename at the source instead:\n${offenders.map((f) => `  ${f}`).join("\n")}`
    );
  }
}

// Topological sort (DFS post-order) from src/main.ts: dependencies must be
// concatenated before the modules that use them, since top-level `const`/
// `class` in the flat concatenated scope are not hoisted the way `function`
// declarations are.
function topoSortFromEntry() {
  const allTsFiles = listTsFiles(srcDir);
  checkForImportAliases(allTsFiles);
  const byKey = new Map(allTsFiles.map((p) => [moduleKey(p), p]));

  const order = [];
  const visited = new Set();
  const visiting = new Set();

  function visit(key) {
    if (visited.has(key)) return;
    if (visiting.has(key)) throw new Error(`circular import involving "${key}"`);
    const tsPath = byKey.get(key);
    if (!tsPath) throw new Error(`import references unknown module "${key}"`);
    visiting.add(key);
    const source = readFileSync(tsPath, "utf-8");
    for (const dep of importedModuleKeys(source, tsPath)) visit(dep);
    visiting.delete(key);
    visited.add(key);
    order.push(key);
  }

  visit("main");

  const unreached = allTsFiles.map(moduleKey).filter((k) => !visited.has(k));
  if (unreached.length > 0) {
    throw new Error(`modules not reachable from main.ts (dead code?): ${unreached.join(", ")}`);
  }
  return order;
}

const EXPORT_KEYWORD_RE = /^export (?=(?:async function|function|class|const|let|var)\b)/gm;
const IMPORT_LINE_RE = /^import\s+[\s\S]*?from\s+["'][^"']+["'];?\s*$/gm;
// tsc emits a bare `export {};` module marker for any file whose exports
// are all type-only (interfaces/types are fully erased at compile time —
// see types/graphData.ts, types/api.ts, types/selection.ts) — this isn't
// covered by EXPORT_KEYWORD_RE (no function/class/const/let/var follows
// "export" here) and, left in, is a syntax error once concatenated into a
// plain (non-module) IIFE. Found by actually executing the built page in
// jsdom after a "nothing loads" report — `tsc`/`node --check` on the
// bundle's syntax alone didn't catch it, since `export {};` is valid syntax
// on its own, just not inside a non-module script.
const EMPTY_EXPORT_RE = /^export \{\s*\};?\s*$/gm;

function stripModuleSyntax(js) {
  return js.replace(IMPORT_LINE_RE, "").replace(EXPORT_KEYWORD_RE, "").replace(EMPTY_EXPORT_RE, "");
}

// tsc type-checks and compiles each file as its own real ES module, with
// its own scope — `const SVG_NS = ...` in two different files is completely
// fine there. But bundleJs() concatenates every module's compiled output
// into ONE flat scope (see the file header), so two modules independently
// declaring a same-named top-level const/let/var/function/class collide at
// *runtime* as a duplicate-declaration SyntaxError that tsc has no way to
// catch (it never sees the flattened result) and that `node --check` on the
// bundle's syntax alone also won't catch reliably (valid syntax in
// isolation; jsdom actually executing the page is what caught this the
// first time). Scanning the stripped output for top-level declarations and
// failing loudly here turns that into an immediate, readable build error
// instead of a silent "nothing loads" in the browser.
const TOP_LEVEL_DECL_RE = /^(?:async function|function|class|const|let|var)\s+([A-Za-z_$][\w$]*)/gm;

function checkForDuplicateTopLevelDeclarations(order, strippedParts) {
  const declaredIn = new Map(); // name -> [moduleKey, ...]
  order.forEach((key, i) => {
    for (const match of strippedParts[i].matchAll(TOP_LEVEL_DECL_RE)) {
      const name = match[1];
      if (!declaredIn.has(name)) declaredIn.set(name, []);
      declaredIn.get(name).push(key);
    }
  });
  const collisions = [...declaredIn.entries()].filter(([, modules]) => modules.length > 1);
  if (collisions.length > 0) {
    const detail = collisions.map(([name, modules]) => `  "${name}" declared in: ${modules.join(", ")}`).join("\n");
    throw new Error(
      `duplicate top-level declaration(s) across modules — rename one, or move the shared value into one file the others import:\n${detail}`
    );
  }
}

function bundleJs() {
  compileTypeScript();
  const order = topoSortFromEntry();
  const strippedParts = order.map((key) => {
    const compiledPath = join(distDir, `${key}.js`);
    const raw = readFileSync(compiledPath, "utf-8");
    return stripModuleSyntax(raw).trim();
  });
  checkForDuplicateTopLevelDeclarations(order, strippedParts);
  const parts = order.map((key, i) => `// ${key}.ts\n${strippedParts[i]}`);
  return `(function () {\n"use strict";\n${parts.join("\n\n")}\n})();\n`;
}

function bundleCss() {
  return CSS_FILES.map((name) => {
    const content = readFileSync(join(stylesDir, name), "utf-8");
    return `/* styles/${name} */\n${content.trim()}`;
  }).join("\n\n");
}

function assemble() {
  const js = bundleJs();
  const css = bundleCss();
  const shell = readFileSync(shellPath, "utf-8");
  const html = shell.replace("/*BUILD:CSS*/", () => css).replace("/*BUILD:JS*/", () => js);
  writeFileSync(outPath, html, "utf-8");
  console.log(`built ${outPath} (${js.length} bytes JS, ${css.length} bytes CSS)`);
}

function tryAssemble() {
  try {
    assemble();
  } catch (err) {
    console.error(err.message ?? err);
  }
}

if (isWatch) {
  tryAssemble();
  let pending = null;
  const trigger = () => {
    clearTimeout(pending);
    pending = setTimeout(tryAssemble, 100);
  };
  watch(srcDir, { recursive: true }, trigger);
  watch(stylesDir, { recursive: true }, trigger);
  watch(shellPath, trigger);
  console.log("watching src/, styles/, index.template.html — Ctrl+C to stop");
} else {
  assemble();
}
