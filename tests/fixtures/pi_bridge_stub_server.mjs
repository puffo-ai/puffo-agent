/**
 * Stub MCP stdio server for the Pi bridge interop tests.
 *
 * Scenario comes from PUFFO_STUB_MODE. Speaks the same newline-delimited
 * JSON-RPC the real puffo_core_server does, so the bridge under test runs its
 * production framing and correlation paths.
 */
const MODE = process.env.PUFFO_STUB_MODE || "ok";

const TOOLS = {
  ok: [
    { name: "send_message", description: "Send", inputSchema: { type: "object" } },
    { name: "read_inbox", description: "Read", inputSchema: { type: "object" } },
  ],
  empty: [],
  duplicate: [
    { name: "send_message", inputSchema: { type: "object" } },
    { name: "send_message", inputSchema: { type: "object" } },
  ],
  blank_name: [{ name: "   ", inputSchema: { type: "object" } }],
  bad_schema: [{ name: "send_message", inputSchema: [1, 2, 3] }],
  // A legal tool result containing the separators that would tear a frame if
  // the client split records with a generic line reader.
  separators: [{ name: "send_message", inputSchema: { type: "object" } }],
};

let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let i = buffer.indexOf("\n");
  while (i !== -1) {
    const line = buffer.slice(0, i);
    buffer = buffer.slice(i + 1);
    if (line.trim()) handle(JSON.parse(line));
    i = buffer.indexOf("\n");
  }
});

function send(frame) {
  process.stdout.write(JSON.stringify(frame) + "\n");
}

function handle(frame) {
  const { id, method } = frame;
  if (method === "initialize") {
    if (MODE === "no_handshake") return; // never answers; client must time out
    send({ jsonrpc: "2.0", id, result: { protocolVersion: "2024-11-05" } });
    return;
  }
  if (method === "notifications/initialized") return;
  if (method === "tools/list") {
    if (MODE === "half_frame_before_list") {
      process.stdout.write('{"jsonrpc":"2.0","id":');
      process.exit(4);
      return;
    }
    if (MODE === "exit_before_list") {
      process.exit(3);
      return;
    }
    send({ jsonrpc: "2.0", id, result: { tools: TOOLS[MODE] ?? TOOLS.ok } });
    return;
  }
  if (method === "tools/call") {
    if (MODE === "tool_error") {
      send({ jsonrpc: "2.0", id, error: { code: -32000, message: "SECRET-PROVIDER-TEXT" } });
      return;
    }
    const text =
      MODE === "separators" ? "before middle afterend" : "sent";
    send({
      jsonrpc: "2.0",
      id,
      result: { content: [{ type: "text", text }], isError: false },
    });
    return;
  }
  send({ jsonrpc: "2.0", id, error: { code: -32601, message: "unknown" } });
}
