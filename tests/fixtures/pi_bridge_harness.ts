/**
 * Drives the Pi tool bridge under plain node, with no Pi present.
 *
 * Records every registerTool call, runs the requested scenario, and prints a
 * single JSON line so pytest can assert on it. Exercises the real bridge
 * module -- not a reimplementation of it.
 */
import { startBridge, BridgeErrorCode } from "./puffo-tools.ts";

const registered: Array<Record<string, unknown>> = [];
const pi = {
  registerTool(definition: Record<string, unknown>) {
    registered.push(definition);
  },
};

const scenario = process.argv[2] ?? "start";

function emit(payload: Record<string, unknown>) {
  process.stdout.write(JSON.stringify({ ...payload, registered: registered.map((t) => t.name) }) + "\n");
}

try {
  const { client, registered: names } = await startBridge(pi, process.env);
  if (scenario === "call") {
    const tool = registered.find((t) => t.name === "send_message") as
      | { execute: (id: string, params: unknown) => Promise<unknown> }
      | undefined;
    const result = await tool!.execute("call-1", { text: "hi" });
    client.stop();
    emit({ ok: true, names, result });
  } else {
    client.stop();
    emit({ ok: true, names });
  }
} catch (error) {
  const code = (error as { code?: string }).code ?? "unknown";
  emit({ ok: false, code, known: Object.values(BridgeErrorCode).includes(code as never) });
}
