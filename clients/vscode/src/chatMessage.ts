/** What a chat surface accepts from its webview.
 *
 *  There are two chat surfaces now — the sidebar view and the full editor-area page — rendering
 *  the same document. These rules used to be inline in the sidebar's provider, which is exactly
 *  how a second surface grows a weaker copy of a security check: the panel renders AGENT OUTPUT,
 *  so every message arriving from it is untrusted, and a command executed because a message named
 *  it would be a real hole.
 *
 *  Deliberately free of vscode, so it can be unit-tested without an extension host.
 */

export interface ChatCommandLike {
  command: string;
}

export type ChatAction =
  | { kind: "send"; text: string }
  | { kind: "command"; command: string }
  | { kind: "open"; path: string }
  | { kind: "pickFile" };

/** The action a message asks for, or null if it asks for nothing we offer.
 *
 *  A command is checked against the list THIS panel declares — not against "is it one of ours",
 *  which would let a message reach any interact command the panel never showed.
 */
export function chatAction(
  message: unknown,
  commands: readonly ChatCommandLike[],
): ChatAction | null {
  if (!message || typeof message !== "object") return null;
  const msg = message as { type?: unknown; text?: unknown; command?: unknown; path?: unknown };

  if (msg.type === "send" && typeof msg.text === "string") {
    const text = msg.text.trim();
    // An empty reply wakes an agent to read nothing: it costs a turn and says nothing.
    return text ? { kind: "send", text } : null;
  }
  if (msg.type === "command" && typeof msg.command === "string") {
    const offered = commands.some((c) => c.command === msg.command);
    return offered ? { kind: "command", command: msg.command } : null;
  }
  if (msg.type === "open" && typeof msg.path === "string" && msg.path) {
    return { kind: "open", path: msg.path };
  }
  if (msg.type === "pickFile") return { kind: "pickFile" };
  return null;
}
