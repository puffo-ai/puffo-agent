/**
 * Puffo tool bridge for Pi.
 *
 * Pi ships no MCP (README.md: "No MCP."), and Puffo's tools exist only as an
 * MCP server. This extension is therefore the sole way a Pi agent can call
 * `send_message` / `read_inbox` at all: it speaks MCP stdio to
 * `puffo_core_server` and re-registers every tool it advertises through
 * `pi.registerTool()`.
 *
 * The tool list is read from the server, never hardcoded here. A copy of the
 * catalog in this file would be a second source of truth that drifts silently
 * the first time a tool is added.
 *
 * Three rules hold throughout:
 *
 * - **Fail closed, never partially.** Every tool is validated before any tool
 *   is registered. A half-registered surface is worse than none, because it
 *   looks like a working agent that is missing exactly the call it needs.
 * - **Nothing sensitive escapes.** Configuration values, the server
 *   environment, raw stderr, and exception text never reach a Pi tool result.
 *   Callers get a stable code from `BridgeErrorCode`.
 * - **Readiness is attested at runtime.** The installed file proves only that
 *   something was written to disk. This process writes a nonce handed to it by
 *   the driver, so a load failure cannot masquerade as a working bridge.
 */

import { Type } from "typebox";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { renameSync, writeFileSync } from "node:fs";

export const BRIDGE_CONFIG_ENV = "PUFFO_PI_BRIDGE_CONFIG";
export const BRIDGE_READY_FILE_ENV = "PUFFO_PI_BRIDGE_READY_FILE";
export const BRIDGE_NONCE_ENV = "PUFFO_PI_BRIDGE_NONCE";
export const BRIDGE_TIMEOUT_ENV = "PUFFO_BRIDGE_TIMEOUT_MS";
export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;

/** Stable, non-revealing failure vocabulary. */
export const BridgeErrorCode = {
  CONFIG_MISSING: "puffo_bridge_config_missing",
  CONFIG_INVALID: "puffo_bridge_config_invalid",
  UNAVAILABLE: "puffo_bridge_unavailable",
  TIMEOUT: "puffo_bridge_timeout",
  NO_TOOLS: "puffo_bridge_no_tools",
  TOOL_SURFACE_INVALID: "puffo_bridge_tool_surface_invalid",
  TOOL_ERROR: "puffo_tool_error",
} as const;

export type BridgeErrorCodeValue =
  (typeof BridgeErrorCode)[keyof typeof BridgeErrorCode];

/**
 * Carries a code, never a payload.
 *
 * `message` is the code itself: anything richer would be assembled from the
 * server environment or provider text, both of which are exactly what must not
 * reach a tool result or a log line.
 */
export class PuffoBridgeError extends Error {
  readonly code: BridgeErrorCodeValue;

  constructor(code: BridgeErrorCodeValue) {
    super(code);
    this.name = "PuffoBridgeError";
    this.code = code;
  }
}

function errorCode(error: unknown): BridgeErrorCodeValue {
  return error instanceof PuffoBridgeError
    ? error.code
    : BridgeErrorCode.UNAVAILABLE;
}

export interface BridgeConfig {
  command: string;
  args: string[];
  environment: Record<string, string>;
}

/** Bounded per-request timeout; a silent server must never hang the load. */
export function readRequestTimeoutMs(
  env: Record<string, string | undefined>,
): number {
  const raw = Number(env[BRIDGE_TIMEOUT_ENV]);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_REQUEST_TIMEOUT_MS;
}

/** Parse the controlled configuration; never guess a server path. */
export function readBridgeConfig(
  env: Record<string, string | undefined>,
): BridgeConfig {
  const raw = env[BRIDGE_CONFIG_ENV];
  if (!raw) throw new PuffoBridgeError(BridgeErrorCode.CONFIG_MISSING);
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new PuffoBridgeError(BridgeErrorCode.CONFIG_INVALID);
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw new PuffoBridgeError(BridgeErrorCode.CONFIG_INVALID);
  }
  const config = parsed as Partial<BridgeConfig>;
  if (typeof config.command !== "string" || config.command.length === 0) {
    throw new PuffoBridgeError(BridgeErrorCode.CONFIG_INVALID);
  }
  if (
    !Array.isArray(config.args) ||
    config.args.some((value) => typeof value !== "string")
  ) {
    throw new PuffoBridgeError(BridgeErrorCode.CONFIG_INVALID);
  }
  if (
    typeof config.environment !== "object" ||
    config.environment === null ||
    Array.isArray(config.environment) ||
    Object.values(config.environment).some(
      (value) => typeof value !== "string",
    )
  ) {
    throw new PuffoBridgeError(BridgeErrorCode.CONFIG_INVALID);
  }
  return {
    command: config.command,
    args: config.args,
    environment: config.environment as Record<string, string>,
  };
}

export interface McpTool {
  name: string;
  description?: string;
  inputSchema?: unknown;
}

interface JsonRpcResponse {
  id?: number | string;
  result?: unknown;
  error?: { code?: number; message?: string };
}

/**
 * Minimal MCP stdio client.
 *
 * Framing note: MCP stdio is newline-delimited JSON, and a tool result may
 * legally contain U+2028 / U+2029 inside a string. Node's `readline` splits on
 * those, so it would tear a frame in half. Records are split on "\n" only.
 */
export class McpStdioClient {
  private proc: ChildProcessWithoutNullStreams | null = null;
  private buffer = "";
  private nextId = 1;
  private initialized = false;
  private pending = new Map<
    number,
    { resolve: (value: unknown) => void; reject: (error: Error) => void }
  >();

  private readonly config: BridgeConfig;
  private readonly requestTimeoutMs: number;

  // Fields are declared and assigned explicitly rather than via constructor
  // parameter properties: Node's type stripping only erases types, and cannot
  // emit the assignments those imply.
  constructor(config: BridgeConfig, requestTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS) {
    this.config = config;
    this.requestTimeoutMs = requestTimeoutMs;
  }

  async start(): Promise<void> {
    let proc: ChildProcessWithoutNullStreams;
    try {
      proc = spawn(this.config.command, this.config.args, {
        // The whole child environment, not merged into this process's own: the
        // harness already decided what the Puffo server may see.
        env: this.config.environment,
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch {
      throw new PuffoBridgeError(BridgeErrorCode.UNAVAILABLE);
    }
    this.proc = proc;
    proc.stdout.setEncoding("utf8");
    proc.stdout.on("data", (chunk: string) => this.consume(chunk));
    // stderr is drained to keep the pipe from filling, and deliberately
    // discarded: it can carry provider text and configuration values.
    proc.stderr.resume();
    const unavailable = () => {
      this.failAll(BridgeErrorCode.UNAVAILABLE);
      this.proc = null;
      this.initialized = false;
    };
    proc.on("exit", unavailable);
    proc.on("error", unavailable);

    try {
      await this.request("initialize", {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "puffo-pi-bridge", version: "1" },
      });
      this.notify("notifications/initialized", {});
      this.initialized = true;
    } catch (error) {
      // A half-open server must not outlive the failed handshake.
      this.stop();
      throw error;
    }
  }

  private consume(chunk: string): void {
    this.buffer += chunk;
    // Split on LF only. See the framing note on the class.
    let index = this.buffer.indexOf("\n");
    while (index !== -1) {
      const line = this.buffer.slice(0, index).replace(/\r$/, "");
      this.buffer = this.buffer.slice(index + 1);
      if (line.trim().length > 0) this.dispatch(line);
      index = this.buffer.indexOf("\n");
    }
  }

  private dispatch(line: string): void {
    let frame: JsonRpcResponse;
    try {
      frame = JSON.parse(line);
    } catch {
      return; // A malformed frame must not kill the reader.
    }
    // Strict pairing: only an integer id this client issued resolves anything.
    if (typeof frame.id !== "number") return;
    const entry = this.pending.get(frame.id);
    if (!entry) return;
    this.pending.delete(frame.id);
    if (frame.error) {
      entry.reject(new PuffoBridgeError(BridgeErrorCode.TOOL_ERROR));
      return;
    }
    entry.resolve(frame.result);
  }

  private failAll(code: BridgeErrorCodeValue): void {
    for (const { reject } of this.pending.values()) {
      reject(new PuffoBridgeError(code));
    }
    this.pending.clear();
  }

  private write(frame: Record<string, unknown>): void {
    if (!this.proc) throw new PuffoBridgeError(BridgeErrorCode.UNAVAILABLE);
    this.proc.stdin.write(JSON.stringify(frame) + "\n");
  }

  notify(method: string, params: unknown): void {
    this.write({ jsonrpc: "2.0", method, params });
  }

  request(method: string, params: unknown): Promise<unknown> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new PuffoBridgeError(BridgeErrorCode.TIMEOUT));
      }, this.requestTimeoutMs);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
      try {
        this.write({ jsonrpc: "2.0", id, method, params });
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error as Error);
      }
    });
  }

  async listTools(): Promise<McpTool[]> {
    if (!this.initialized) {
      // tools/list before the handshake completes is a protocol violation the
      // server is free to answer with anything at all.
      throw new PuffoBridgeError(BridgeErrorCode.UNAVAILABLE);
    }
    const result = (await this.request("tools/list", {})) as {
      tools?: McpTool[];
    };
    return Array.isArray(result?.tools) ? result.tools : [];
  }

  async callTool(name: string, args: unknown): Promise<unknown> {
    return this.request("tools/call", { name, arguments: args ?? {} });
  }

  stop(): void {
    this.failAll(BridgeErrorCode.UNAVAILABLE);
    this.proc?.kill();
    this.proc = null;
    this.initialized = false;
  }
}

/**
 * Reject a tool surface that cannot be registered whole.
 *
 * Checked before anything is registered: a partially registered surface is an
 * agent that looks healthy and is missing exactly one call.
 */
export function validateToolSurface(tools: McpTool[]): McpTool[] {
  if (tools.length === 0) {
    throw new PuffoBridgeError(BridgeErrorCode.NO_TOOLS);
  }
  const seen = new Set<string>();
  for (const tool of tools) {
    const name = tool?.name;
    if (typeof name !== "string" || name.trim().length === 0) {
      throw new PuffoBridgeError(BridgeErrorCode.TOOL_SURFACE_INVALID);
    }
    if (seen.has(name)) {
      throw new PuffoBridgeError(BridgeErrorCode.TOOL_SURFACE_INVALID);
    }
    seen.add(name);
    const schema = tool.inputSchema;
    if (
      schema !== undefined &&
      (typeof schema !== "object" || schema === null || Array.isArray(schema))
    ) {
      throw new PuffoBridgeError(BridgeErrorCode.TOOL_SURFACE_INVALID);
    }
  }
  return tools;
}

/** Register one Pi tool per MCP tool. Returns the registered names. */
export function registerPuffoTools(
  pi: { registerTool: (definition: Record<string, unknown>) => void },
  client: McpStdioClient,
  tools: McpTool[],
): string[] {
  validateToolSurface(tools);
  const registered: string[] = [];
  for (const tool of tools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description ?? `Puffo tool ${tool.name}`,
      // Type.Unsafe wraps the server's JSON Schema verbatim. Pi passes
      // `parameters` straight through to the provider, so a plain object would
      // also work -- this stays correct if it is ever validated by TypeBox.
      parameters: Type.Unsafe(tool.inputSchema ?? { type: "object" }),
      async execute(_toolCallId: string, params: unknown) {
        try {
          const result = (await client.callTool(tool.name, params)) as {
            content?: unknown;
            isError?: boolean;
          };
          return {
            content: result?.content ?? [{ type: "text", text: "" }],
            details: {},
            isError: Boolean(result?.isError),
          };
        } catch (error) {
          // Reported to the model as a failed tool carrying only a code --
          // never as an empty success it would read as "message sent".
          return {
            content: [{ type: "text", text: errorCode(error) }],
            details: {},
            isError: true,
          };
        }
      },
    });
    registered.push(tool.name);
  }
  return registered;
}

/**
 * Attest that *this* Pi process loaded the bridge.
 *
 * The installed file proves only that an installer ran. The driver clears this
 * path and mints a fresh nonce per spawn, so a stale file, a crashed load, or
 * a disabled extension cannot be mistaken for a live tool surface. Written via
 * rename so a reader never observes a partial nonce.
 */
export function attestReady(
  env: Record<string, string | undefined>,
  registered: string[],
): boolean {
  const path = env[BRIDGE_READY_FILE_ENV];
  const nonce = env[BRIDGE_NONCE_ENV];
  if (!path || !nonce) return false;
  const payload = JSON.stringify({ nonce, tools: registered.length });
  const temp = `${path}.${process.pid}.tmp`;
  writeFileSync(temp, payload, { encoding: "utf8", mode: 0o600 });
  renameSync(temp, path);
  return true;
}

/**
 * Bring up the bridge. Throws on every path that would leave the agent mute.
 */
export async function startBridge(
  pi: { registerTool: (definition: Record<string, unknown>) => void },
  env: Record<string, string | undefined>,
): Promise<{ client: McpStdioClient; registered: string[] }> {
  const client = new McpStdioClient(
    readBridgeConfig(env),
    readRequestTimeoutMs(env),
  );
  await client.start();
  let registered: string[];
  try {
    const tools = await client.listTools();
    registered = registerPuffoTools(pi, client, tools);
    if (!attestReady(env, registered)) {
      throw new PuffoBridgeError(BridgeErrorCode.CONFIG_MISSING);
    }
  } catch (error) {
    // No surviving server behind a failed registration.
    client.stop();
    // Pi may surface an extension-load rejection. Preserve only our stable
    // vocabulary so filesystem paths and runtime exception text cannot leak.
    throw new PuffoBridgeError(errorCode(error));
  }
  return { client, registered };
}

export default function (pi: {
  registerTool: (definition: Record<string, unknown>) => void;
}) {
  // Awaited by Pi's extension loader (`await factory(api)`); a rejection
  // surfaces as extension_error, which the Puffo Pi driver normalizes into a
  // runtime warning. The ready file stays absent, so admission fails closed.
  return startBridge(pi, process.env).then(() => undefined);
}
