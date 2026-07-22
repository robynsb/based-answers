// pi extension: exposes pdf-search.py to the citation-searcher agent.
//
// Loaded by path via `pi -e`, so this file needs no package.json and no
// node_modules — it imports only pi's own types and node builtins.
//
// The pipeline passes the interpreter and script location through the
// environment rather than generating this file, so the agent tooling is
// plain checked-in source.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFileSync } from "node:child_process";

const PYTHON = process.env.BA_PYTHON ?? "python3";
const SKILL_DIR = process.env.BA_SKILL_DIR ?? ".";

function run(args: string[]): string {
  try {
    return execFileSync(PYTHON, [`${SKILL_DIR}/pdf-search.py`, ...args], {
      timeout: 60000,
      encoding: "utf-8",
      // A `get` over several pages of a large datasheet far exceeds the 1 MB default.
      maxBuffer: 64 * 1024 * 1024,
    }).trim();
  } catch (e: any) {
    return e.stdout?.trim() || e.stderr?.trim() || e.message;
  }
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "pdf_search",
    label: "PDF Search",
    description:
      "Search PDFs for text, enumerate every distinct match of a regex, " +
      "retrieve full page content, or get document info",
    parameters: Type.Object({
      action: Type.Union(
        [
          Type.Literal("search"),
          Type.Literal("search_regex"),
          Type.Literal("get"),
          Type.Literal("info"),
        ],
        { description: "Action to perform" },
      ),
      pdf: Type.String({ description: "Path to the PDF file" }),
      query: Type.Optional(
        Type.String({ description: "Text to search for (required for search)" }),
      ),
      pattern: Type.Optional(
        Type.String({
          description:
            "Regex pattern to enumerate (required for search_regex). Returns every " +
            "DISTINCT matching string found anywhere in the document, deduplicated — " +
            "use this instead of guessing a handful of literal names when a claim " +
            "needs to rule out a whole family of possible names.",
        }),
      ),
      pages: Type.Optional(
        Type.Array(Type.Number(), {
          description: "Page numbers to retrieve (required for get)",
        }),
      ),
      limit: Type.Optional(
        Type.Number({ description: "Max search results (default: 10)" }),
      ),
    }),
    async execute(_toolCallId, params) {
      const text = (() => {
        if (params.action === "info") {
          return run([params.pdf, "info"]);
        }
        if (params.action === "search") {
          if (!params.query) return "Error: query is required for search";
          return run([
            params.pdf,
            "search",
            params.query,
            "--limit",
            String(params.limit ?? 10),
          ]);
        }
        if (params.action === "search_regex") {
          if (!params.pattern) return "Error: pattern is required for search_regex";
          return run([params.pdf, "search-regex", params.pattern]);
        }
        if (params.action === "get") {
          if (!params.pages || params.pages.length === 0) {
            return "Error: page numbers required for get";
          }
          return run([params.pdf, "get", ...params.pages.map(String)]);
        }
        return `Error: unknown action ${params.action}`;
      })();

      return { content: [{ type: "text", text }], details: {} };
    },
  });
}
