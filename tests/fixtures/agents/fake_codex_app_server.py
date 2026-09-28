#!/usr/bin/env python3
"""Deterministic Codex app-server stand-in used by conversation integration tests."""

import json
import os
import sys
from pathlib import Path


class _FakeCodexAppServer:
    def __init__(self) -> None:
        executable = Path(sys.argv[0])
        self.config = json.loads(executable.with_suffix(".json").read_text())
        self.log_path = executable.with_suffix(".log")
        self.model_calls = 0
        self.turns = 0
        if self.log_path.is_file():
            self.turns = sum(
                json.loads(line).get("method") == "turn/start"
                for line in self.log_path.read_text().splitlines()
            )
        self.approval_responses = 0
        executable.with_suffix(".pid").write_text(str(os.getpid()))

        if sys.argv[1:] != ["app-server", "--listen", "stdio://"]:
            raise SystemExit("unexpected app-server transport arguments")

    def run(self) -> None:
        try:
            for line in sys.stdin:
                message = json.loads(line)
                if "jsonrpc" in message:
                    raise SystemExit("Codex app-server frames omit the JSON-RPC header")
                self._record(message)
                if "method" in message:
                    self._request(message)
                elif message.get("id") in self._approval_ids():
                    self.approval_responses += 1
                    if self.approval_responses == len(self._approval_ids()):
                        self._complete_after_approval()
        finally:
            Path(sys.argv[0]).with_suffix(".exit").write_text("closed\n")

    def _record(self, message: dict) -> None:
        record = {"id": message.get("id"), "method": message.get("method")}
        params = message.get("params")
        method = message.get("method")
        if method in ("initialize", "account/read", "model/list", "turn/start"):
            record["params"] = params
        if method == "thread/start" and isinstance(params, dict):
            record["params"] = {
                key: params.get(key)
                for key in (
                    "model", "cwd", "runtimeWorkspaceRoots", "sandbox", "approvalPolicy",
                    "approvalsReviewer", "allowProviderModelFallback",
                )
            }
            if "developerInstructions" in params:
                record["params"]["developerInstructions"] = params["developerInstructions"]
        if message.get("method") == "thread/resume" and isinstance(params, dict):
            record["params"] = {"threadId": params.get("threadId")}
        if message.get("method") == "turn/interrupt" and isinstance(params, dict):
            record["params"] = {
                "threadId": params.get("threadId"), "turnId": params.get("turnId")
            }
        if "result" in message:
            record["result"] = message["result"]
        if "error" in message:
            record["error"] = message["error"]
        with self.log_path.open("a") as stream:
            stream.write(json.dumps(record) + "\n")

    def _request(self, message: dict) -> None:
        method = message["method"]
        request_id = message.get("id")
        params = message.get("params")
        if not self._valid(method, params):
            self._write({"id": request_id, "error": {
                "code": -32602, "message": "invalid synthetic request shape",
            }})
            return
        if method == "initialize":
            if self.config.get("mode") == "incompatible":
                self._write({"id": request_id, "error": {"code": -32601, "message": "unsupported"}})
                return
            self._write({
                "id": request_id,
                "result": {
                    "codexHome": "/synthetic/codex",
                    "platformFamily": "unix",
                    "platformOs": "linux",
                    "userAgent": "fake-codex-app-server/1",
                },
            })
            self._write({
                "method": "configWarning",
                "params": {"summary": "synthetic warning", "details": None},
                "emittedAtMs": 1_790_092_139_983,
            })
            self._write({
                "method": "remoteControl/status/changed",
                "params": {"status": "disabled", "serverName": "synthetic"},
                "emittedAtMs": 1_790_092_139_983,
            })
            return
        assert isinstance(params, dict)
        if method == "initialized":
            return
        if method == "account/read":
            auth = self.config.get("mode") != "auth_required"
            self._write({
                "id": request_id,
                "result": {
                    "account": {
                        "type": "chatgpt",
                        "email": None,
                        "planType": "plus",
                    } if auth else None,
                    # What codex 0.155.1 answers on the default OpenAI provider, signed in or not.
                    "requiresOpenaiAuth": True,
                },
            })
            return
        if method == "model/list":
            paged = self.config.get("paged_models")
            if paged:
                page_index = int(params.get("cursor") or 0)
                models = paged[page_index]
                next_cursor = str(page_index + 1) if page_index + 1 < len(paged) else None
                is_first_page = page_index == 0
            else:
                pages = self.config.get("model_pages") or [
                    self.config.get("models") or ["openai/example-model"]
                ]
                models = pages[min(self.model_calls, len(pages) - 1)]
                self.model_calls += 1
                next_cursor = None
                is_first_page = True
            self._write({
                "id": request_id,
                "result": {
                    "data": [
                        self._model(model, is_first_page and index == 0)
                        for index, model in enumerate(models)
                    ],
                    "nextCursor": next_cursor,
                },
            })
            return
        if method == "thread/start":
            thread_id = self.config.get("thread_id", "thread-root")
            self._write({
                "id": request_id,
                "result": self._thread_result(thread_id, params["model"]),
            })
            return
        if method == "thread/resume":
            if self.config.get("mode") == "resume_failure":
                self._write({
                    "id": request_id,
                    "error": {"code": -32000, "message": "synthetic resume failure"},
                })
                return
            thread_id = message["params"]["threadId"]
            self._write({
                "id": request_id,
                "result": self._thread_result(thread_id, params.get("model") or "openai/example-model"),
            })
            return
        if method == "turn/start":
            self.turns += 1
            turn_id = f"turn-{self.turns}"
            self._write({
                "id": request_id,
                "result": {"turn": {"id": turn_id, "status": "inProgress", "items": [], "error": None}},
            })
            self._write({"method": "turn/started", "params": {
                "threadId": message["params"]["threadId"],
                "turn": {"id": turn_id, "status": "inProgress", "items": [], "error": None},
            }})
            self._after_turn_start(message["params"]["threadId"], turn_id)
            return
        if method == "turn/interrupt":
            if self.config.get("mode") == "captured_schema_invalid":
                self._write({
                    "id": "invalid-provider-request",
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "threadId": message["params"]["threadId"],
                        "turnId": message["params"]["turnId"],
                        "itemId": "invalid-question",
                        "isBlocking": True,
                    },
                })
                return
            self._write({"id": request_id, "result": {}})
            self._write({"method": "turn/completed", "params": {
                "threadId": message["params"]["threadId"],
                "turn": {
                    "id": message["params"]["turnId"],
                    "status": "interrupted",
                    "items": [],
                    "error": None,
                },
            }})

    @staticmethod
    def _valid(method: str, params: object) -> bool:
        if not isinstance(params, dict):
            return False
        if method == "initialize":
            client = params.get("clientInfo")
            capabilities = params.get("capabilities")
            return bool(
                isinstance(client, dict)
                and client.get("name") == "interact"
                and isinstance(client.get("version"), str)
                and client["version"]
                and capabilities == {"experimentalApi": False}
            )
        if method == "initialized":
            return params == {}
        if method == "account/read":
            return params == {"refreshToken": False}
        if method == "model/list":
            return bool(
                params.get("cursor") is None or isinstance(params.get("cursor"), str)
            ) and params.get("includeHidden") is False and params.get("limit") == 100
        if method == "thread/start":
            return bool(
                isinstance(params.get("model"), str)
                and isinstance(params.get("cwd"), str)
                and params.get("runtimeWorkspaceRoots") == [params.get("cwd")]
                and params.get("sandbox") == "read-only"
                and params.get("approvalPolicy") == "on-request"
                and params.get("approvalsReviewer") == "user"
                and params.get("allowProviderModelFallback") is False
            )
        if method == "thread/resume":
            return bool(
                isinstance(params.get("threadId"), str)
                and isinstance(params.get("cwd"), str)
                and params.get("runtimeWorkspaceRoots") == [params.get("cwd")]
                and params.get("sandbox") == "read-only"
                and params.get("approvalPolicy") == "on-request"
                and params.get("approvalsReviewer") == "user"
            )
        if method == "turn/start":
            input_items = params.get("input")
            return bool(
                isinstance(params.get("threadId"), str)
                and isinstance(input_items, list)
                and len(input_items) == 1
                and isinstance(input_items[0], dict)
                and input_items[0].get("type") == "text"
                and isinstance(input_items[0].get("text"), str)
            )
        if method == "turn/interrupt":
            return bool(
                isinstance(params.get("threadId"), str)
                and isinstance(params.get("turnId"), str)
            )
        return False

    def _after_turn_start(self, thread_id: str, turn_id: str) -> None:
        mode = self.config.get("mode", "success")
        if mode in ("hold", "captured_schema_invalid"):
            return
        if mode == "eof":
            raise SystemExit(0)
        if mode == "malformed":
            sys.stdout.write("{malformed\n")
            sys.stdout.flush()
            return
        if mode == "known_malformed":
            self._write({"method": "item/agentMessage/delta", "params": {
                "threadId": thread_id, "turnId": turn_id,
            }})
            return
        if mode == "oversized":
            self._write({"method": "item/agentMessage/delta", "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "message-1",
                "delta": "x" * 1_100_000,
            }})
            return
        if mode == "approval":
            for index, approval_id in enumerate(self._approval_ids(), start=1):
                self._write({
                    "id": approval_id,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": f"command-{index}",
                        "startedAtMs": index,
                        "command": "synthetic-command --flag",
                        "cwd": "/synthetic/workspace",
                        "availableDecisions": ["accept", "decline", "cancel"],
                    },
                })
            return
        if mode == "heterogeneous_interactions":
            requests = [
                ("item/commandExecution/requestApproval", {
                    "threadId": thread_id, "turnId": turn_id, "itemId": "command-1",
                    "startedAtMs": 1,
                    "command": "synthetic-command --safe", "cwd": "/synthetic/workspace",
                    "availableDecisions": ["accept", "decline"],
                }),
                ("item/fileChange/requestApproval", {
                    "threadId": thread_id, "turnId": turn_id, "itemId": "file-1",
                    "startedAtMs": 1,
                    "grantRoot": "/synthetic/workspace", "reason": "edit the fixture",
                }),
                ("item/tool/requestUserInput", {
                    "threadId": thread_id, "turnId": turn_id, "itemId": "question-1",
                    "isBlocking": True,
                    "questions": [
                        {"id": "choice", "header": "Choice",
                         "question": "Choose one", "options": [
                             {"label": "A", "description": "first"},
                             {"label": "B", "description": "second"},
                         ]},
                        {"id": "notes", "header": "Notes",
                         "question": "Explain the choice"},
                    ],
                }),
                ("item/permissions/requestApproval", {
                    "threadId": thread_id, "turnId": turn_id, "itemId": "permission-1",
                    "startedAtMs": 1, "cwd": "/synthetic/workspace",
                    "permissions": {"network": {"enabled": True}},
                }),
            ]
            for index, (method, params) in enumerate(requests, start=1):
                self._write({"id": f"interaction-{index}", "method": method, "params": params})
            return
        if mode == "fragmented_usage":
            for delta in ("synthetic ", "fragmented ", "answer"):
                self._write({"method": "item/agentMessage/delta", "params": {
                    "threadId": thread_id, "turnId": turn_id,
                    "itemId": f"message-{self.turns}", "delta": delta,
                }})
            usage = {
                "inputTokens": self.turns * 10, "cachedInputTokens": self.turns * 2,
                "outputTokens": self.turns * 5, "reasoningOutputTokens": 0,
                "totalTokens": self.turns * 15, "cacheWriteInputTokens": self.turns,
            }
            for _ in range(2):
                self._write({"method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread_id, "turnId": turn_id,
                    "tokenUsage": {"total": usage, "last": usage,
                                   "modelContextWindow": 128_000},
                }})
            self._write({"method": "item/completed", "params": {
                "threadId": thread_id, "turnId": turn_id,
                "item": {"type": "agentMessage", "id": f"message-{self.turns}",
                         "text": "synthetic fragmented answer"},
                "completedAtMs": self.turns * 1_000,
            }})
            self._write({"method": "turn/completed", "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "completed", "items": [], "error": None},
            }})
            return
        if mode == "failed_turn":
            self._write({"method": "turn/completed", "params": {
                "threadId": thread_id,
                "turn": {
                    "id": turn_id, "status": "failed", "items": [],
                    "error": {"message": "synthetic provider failure"},
                },
            }})
            return
        if mode == "late_after_terminal":
            self._answer(thread_id, turn_id)
            self._write({"method": "item/agentMessage/delta", "params": {
                "threadId": thread_id, "turnId": turn_id,
                "itemId": "late-message", "delta": "late stale frame",
            }})
            self._write({"method": "turn/started", "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "inProgress", "items": [], "error": None},
            }})
            return
        if mode == "collaboration":
            self._collaboration(thread_id, turn_id)
            self._write({"method": "thread/syntheticFuture", "params": {
                "threadId": thread_id, "turnId": turn_id, "value": "bounded future fact",
            }})
            self._write({"method": "item/started", "params": {
                "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1_600,
                "item": {"type": "syntheticFutureItem", "id": "future-item"},
            }})
        if mode == "collaboration_eof":
            self._collaboration(thread_id, turn_id, complete=False)
            raise SystemExit(0)
        self._answer(thread_id, turn_id)

    def _collaboration(self, thread_id: str, turn_id: str, *, complete: bool = True) -> None:
        child_thread = "thread-child"
        child_turn = "turn-child"
        item = {
            "type": "collabAgentToolCall",
            "id": "collab-1",
            "tool": "spawnAgent",
            "status": "inProgress",
            "senderThreadId": thread_id,
            "receiverThreadIds": [child_thread],
            "prompt": None,
            "model": None,
            "reasoningEffort": None,
            "agentsStates": {child_thread: {"status": "running", "message": None}},
        }
        if not complete:
            self._write({"method": "item/started", "params": {
                "threadId": thread_id, "turnId": turn_id, "item": item,
                "startedAtMs": 1_000,
            }})
            return
        # Child activity deliberately precedes the collaboration metadata that explains it.
        self._write({"method": "item/agentMessage/delta", "params": {
            "threadId": child_thread, "turnId": child_turn,
            "itemId": "child-message", "delta": "synthetic child answer",
        }})
        self._write({"method": "item/completed", "params": {
            "threadId": child_thread, "turnId": child_turn,
            "completedAtMs": 1_250,
            "item": {"type": "agentMessage", "id": "child-message",
                     "text": "synthetic child answer"},
        }})
        child_tool = {
            "type": "commandExecution", "id": "child-command",
            "command": self.config.get("child_command", "printf synthetic"), "commandActions": [],
            "cwd": "/synthetic/workspace", "status": "inProgress",
        }
        self._write({"method": "item/started", "params": {
            "threadId": child_thread, "turnId": child_turn,
            "item": child_tool, "startedAtMs": 1_200,
        }})
        self._write({"method": "item/completed", "params": {
            "threadId": child_thread, "turnId": child_turn,
            "item": {**child_tool, "status": "completed", "exitCode": 0,
                     "aggregatedOutput": "synthetic tool output"},
            "completedAtMs": 1_500,
        }})
        usage = {
            "inputTokens": 11, "cachedInputTokens": 0, "outputTokens": 7,
            "reasoningOutputTokens": 0, "totalTokens": 18, "cacheWriteInputTokens": 0,
        }
        self._write({"method": "thread/tokenUsage/updated", "params": {
            "threadId": child_thread, "turnId": child_turn,
            "tokenUsage": {"total": usage, "last": usage, "modelContextWindow": 128_000},
        }})
        completed = {**item, "status": "completed", "prompt": "inspect the synthetic result",
                     "model": "openai/example-child"}
        # Completion deliberately arrives first, then is replayed, then richer start metadata.
        self._write({"method": "item/completed", "params": {
            "threadId": thread_id, "turnId": turn_id, "item": completed,
            "completedAtMs": 2_000,
        }})
        self._write({"method": "item/completed", "params": {
            "threadId": thread_id, "turnId": turn_id, "item": completed,
            "completedAtMs": 2_000,
        }})
        self._write({"method": "item/started", "params": {
            "threadId": thread_id, "turnId": turn_id, "item": item,
            "startedAtMs": 1_000,
        }})

    def _answer(self, thread_id: str, turn_id: str) -> None:
        self._write({"method": "item/agentMessage/delta", "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "itemId": "message-1",
            "delta": "synthetic answer",
        }})
        self._write({"method": "item/completed", "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": {"type": "agentMessage", "id": "message-1", "text": "synthetic answer"},
            "completedAtMs": 3_000,
        }})
        self._write({"method": "turn/completed", "params": {
            "threadId": thread_id,
            "turn": {"id": turn_id, "status": "completed", "items": [], "error": None},
        }})

    def _complete_after_approval(self) -> None:
        self._answer("thread-root", f"turn-{self.turns}")

    def _approval_ids(self) -> list[int | str]:
        configured = self.config.get("approval_ids")
        if isinstance(configured, list):
            return configured
        if self.config.get("mode") == "heterogeneous_interactions":
            return ["interaction-1", "interaction-2", "interaction-3"]
        return ["approval-1"]

    @staticmethod
    def _model(model: str, default: bool) -> dict:
        return {
            "id": model,
            "model": model,
            "displayName": model,
            "description": "synthetic model",
            "hidden": False,
            "isDefault": default,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [],
            "multiAgentVersion": "v1",
        }

    def _thread_result(self, thread_id: str, model: str) -> dict:
        return {
            "thread": {
                "id": thread_id,
                "sessionId": self.config.get("session_id", thread_id),
                "status": {"type": "idle"},
                "modelProvider": "openai",
                "turns": [],
            },
            "model": model,
            "modelProvider": "openai",
            "cwd": "/synthetic/workspace",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "sandbox": {"type": "readOnly"},
            "runtimeWorkspaceRoots": [],
        }

    @staticmethod
    def _write(message: dict) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    _FakeCodexAppServer().run()
