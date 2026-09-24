// Shared SVG/XLink namespace URIs for createElementNS/setAttributeNS calls
// across graph/*.ts. Centralized in one file (rather than each file
// declaring its own copy, as the original monolith's single scope allowed)
// because this codebase's build concatenates every module into one flat
// script scope (see build.mjs) — two files independently declaring
// `const SVG_NS = ...` compiles fine under tsc (each file is its own real
// ES module there) but collides as a duplicate top-level declaration once
// flattened, a `SyntaxError` at runtime that no amount of `tsc`/`node
// --check` on the source or the bundle's syntax alone catches. Found by
// actually executing the built page. build.mjs now also fails the build
// loudly on any such duplicate, so this class of bug can't silently recur.
export const SVG_NS = "http://www.w3.org/2000/svg";
export const XLINK_NS = "http://www.w3.org/1999/xlink";
