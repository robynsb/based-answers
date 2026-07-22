// pi extension: the searcher's only write path.
//
// ANSWER_SLUG is set per run by the pipeline, so the agent never sees or
// handles a slug and a run's answer file cannot sprawl across rounds.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import * as fs from "node:fs";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "write_answer",
    label: "Write Answer",
    description:
      "Write this run's citation-grounded answer YAML file, overwriting any " +
      "previous round's attempt.",
    parameters: Type.Object({
      yaml_content: Type.String({ description: "Full YAML content to write" }),
    }),
    async execute(_toolCallId, params) {
      const slug = process.env.ANSWER_SLUG;
      if (!slug) {
        throw new Error(
          "ANSWER_SLUG is not set — this tool must be run inside the citation-qa pipeline",
        );
      }
      fs.mkdirSync("answers", { recursive: true });
      const filename = `answers/${slug}.yml`;
      fs.writeFileSync(filename, params.yaml_content, "utf-8");
      return { content: [{ type: "text", text: filename }], details: { filename } };
    },
  });
}
