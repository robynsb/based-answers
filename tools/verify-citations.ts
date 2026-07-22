// pi extension: lets the searcher self-check an answer before handing it back.
// See tools/pdf-search.ts for why this file is plain checked-in source.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFileSync } from "node:child_process";

function required(name: string): string {
  const v = process.env[name];
  if (!v) {
    // A fallback could not work anyway — a bare `python3` has no pymupdf —
    // and would surface as an opaque tool result the agent tries to reason
    // about, rather than as a wiring error.
    throw new Error(`${name} is not set — this tool must be run inside the citation-qa pipeline`);
  }
  return v;
}

const PYTHON = required("BA_PYTHON");
const SKILL_DIR = required("BA_SKILL_DIR");

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "verify_citations",
    label: "Verify Citations",
    description: "Verify citations in a YAML answer file against source PDFs",
    parameters: Type.Object({
      yaml: Type.String({ description: "Path to the YAML answer file" }),
      pdf_dir: Type.Optional(
        Type.String({
          description: "Directory containing PDFs (default: working directory)",
        }),
      ),
    }),
    async execute(_toolCallId, params) {
      let text: string;
      try {
        text = execFileSync(
          PYTHON,
          [
            `${SKILL_DIR}/verify-citations.py`,
            "--pdf-dir",
            params.pdf_dir ?? ".",
            params.yaml,
          ],
          { timeout: 60000, encoding: "utf-8", maxBuffer: 64 * 1024 * 1024 },
        ).trim();
      } catch (e: any) {
        text = e.stdout?.trim() || e.stderr?.trim() || e.message;
      }
      return { content: [{ type: "text", text }], details: {} };
    },
  });
}
