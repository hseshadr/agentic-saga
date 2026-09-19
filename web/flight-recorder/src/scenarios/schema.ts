import { z } from "zod";

const safeTraceRefSchema = z.string().regex(/^[a-z0-9][a-z0-9_-]{0,119}\.json$/);
const digestSchema = z.string().regex(/^[a-f0-9]{64}$/);

export const scenarioIndexEntrySchema = z
  .object({
    id: z
      .string()
      .min(1)
      .max(80)
      .regex(/^[a-z0-9][a-z0-9-]*$/),
    name: z.string().min(1).max(120),
    summary: z.string().min(1).max(500),
    mode: z.enum(["scripted", "live"]),
    presentation: z.enum(["ecommerce"]).optional(),
    trace_ref: safeTraceRefSchema,
    trace_sha256: digestSchema,
  })
  .strict();

export const scenarioIndexSchema = z
  .object({
    schema_version: z.literal("1.0"),
    runs: z.array(scenarioIndexEntrySchema).min(1).max(100),
  })
  .strict()
  .superRefine((value, context) => {
    const ids = value.runs.map((run) => run.id);
    if (new Set(ids).size !== ids.length) {
      context.addIssue({ code: "custom", message: "run IDs must be unique", path: ["runs"] });
    }
  });

export type ScenarioIndex = z.infer<typeof scenarioIndexSchema>;
export type ScenarioIndexEntry = z.infer<typeof scenarioIndexEntrySchema>;
