export const PROMPT_ACTIONS = [
  { id: "status", remote: false }, { id: "log", remote: false },
  { id: "commit", remote: false }, { id: "compile", remote: false },
  { id: "install", remote: false }, { id: "pull", remote: true },
  { id: "push", remote: true }, { id: "publish", remote: true },
  { id: "sync", remote: true },
] as const;
export type PromptAction = typeof PROMPT_ACTIONS[number]["id"];
