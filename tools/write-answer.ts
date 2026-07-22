// pi extension: the searcher's only write path.
//
// The agent does not write citation text. It names the evidence — a span of
// the numbered lines pdf_search prints, a literal query, a regex pattern —
// and resolve-answer.py materialises it from the same extracted-text cache
// the verifier reads. So a quote is verbatim by construction, and there is
// no way to author a mis-transcribed one.
//
// ANSWER_SLUG and ANSWER_QUESTION are set per run by the pipeline, so the
// agent never sees or handles a slug and a run's answer file cannot sprawl
// across rounds.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFileSync } from "node:child_process";

function required(name: string): string {
  const v = process.env[name];
  if (!v) {
    throw new Error(`${name} is not set — this tool must be run inside the citation-qa pipeline`);
  }
  return v;
}

const Span = Type.Object(
  {
    page: Type.Number({ description: "Page number the lines are on" }),
    from: Type.Number({ description: "First line to quote (1-based, as printed)" }),
    to: Type.Number({ description: "Last line to quote, inclusive" }),
  },
  { description: "A run of numbered lines on one page" },
);

const Citation = Type.Object({
  type: Type.Union(
    [Type.Literal("quote"), Type.Literal("search"), Type.Literal("regex")],
    {
      description:
        "quote: cite a passage by the lines it occupies. " +
        "search: cite what a literal query does or does not find (absence). " +
        "regex: cite every distinct string a pattern matches (exhaustiveness).",
    },
  ),
  source: Type.String({ description: "PDF filename" }),
  spans: Type.Optional(
    Type.Array(Span, {
      description:
        "For type 'quote': the lines to quote, in order, joined into one " +
        "citation. Use several spans to skip lines you do not want to quote " +
        "— a running header or footer interrupting a passage that continues " +
        "onto the next page. Line numbers are the ones pdf_search prints.",
    }),
  ),
  query: Type.Optional(
    Type.String({ description: "For type 'search': the literal text to search for" }),
  ),
  pattern: Type.Optional(
    Type.String({ description: "For type 'regex': the pattern to enumerate" }),
  ),
});

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "write_answer",
    label: "Write Answer",
    description:
      "Write this run's citation-grounded answer, overwriting any previous " +
      "round's attempt. You give claims and point at the evidence for each; " +
      "the citation text is taken from the source, never typed. Returns the " +
      "realised answer so you can check it says what you meant.",
    parameters: Type.Object({
      answers: Type.Array(
        Type.Object({
          claim: Type.String({ description: "The claim, in one sentence" }),
          citations: Type.Array(Citation, {
            description: "The evidence for this claim — at least one",
          }),
        }),
        {
          description:
            "The claims answering the question, in order. An empty list is " +
            "the valid 'unable to answer' output.",
        },
      ),
    }),
    async execute(_toolCallId, params) {
      const python = required("BA_PYTHON");
      const skillDir = required("BA_SKILL_DIR");
      const slug = required("ANSWER_SLUG");

      const args = [
        `${skillDir}/resolve-answer.py`,
        "--slug",
        slug,
        "--question",
        process.env.ANSWER_QUESTION ?? "",
      ];

      try {
        const text = execFileSync(python, args, {
          input: JSON.stringify(params),
          timeout: 120000,
          encoding: "utf-8",
          maxBuffer: 64 * 1024 * 1024,
        }).trim();
        return { content: [{ type: "text", text }], details: {} };
      } catch (e: any) {
        // A non-zero exit means nothing was written and stdout names every
        // citation that would not resolve — that is the result to hand back.
        const text = e.stdout?.trim() || e.stderr?.trim() || e.message;
        return { content: [{ type: "text", text }], details: {} };
      }
    },
  });
}
