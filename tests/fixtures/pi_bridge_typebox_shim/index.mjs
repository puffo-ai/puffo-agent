/**
 * Minimal `typebox` stand-in for the bridge interop tests.
 *
 * The bridge uses exactly one TypeBox call, `Type.Unsafe(schema)`, whose job is
 * to hand an arbitrary JSON Schema through unchanged. Pi consumes it the same
 * way: `getJsonSchemaToolParameters(tool, strict)` in the 0.84.3 bundle returns
 * `tool.parameters` directly. Identity therefore matches both sides, and keeps
 * these tests hermetic instead of depending on an npm install.
 */
export const Type = { Unsafe: (schema) => schema };
