"""Reference tool adapters.

Three real adapters that show the pattern. Each adapter declares the tool
names it handles, their MCP input schemas, and an `execute` method that
calls the actual API. Credentials live here, never in the agent.

You can write your own adapter by implementing the same shape (see types.py
for the ToolAdapter protocol).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import httpx

from .types import ToolCall


# -----------------------------
# Slack adapter
# -----------------------------

class SlackAdapter:
    """Sends messages to Slack channels using a bot token.

    Configure via:
      - constructor token argument, or
      - SLACK_BOT_TOKEN env var

    Tools:
      slack_send_message(channel: str, text: str)
    """

    def __init__(self, token: Optional[str] = None) -> None:
        self._token = token or os.environ.get("SLACK_BOT_TOKEN", "")
        if not self._token:
            # Don't raise; allow demo/test runs without a real token.
            # The execute() call will fail explicitly if used.
            pass

    @property
    def tool_names(self) -> List[str]:
        return ["slack_send_message"]

    @property
    def tool_schemas(self) -> Dict[str, Dict[str, Any]]:
        return {
            "slack_send_message": {
                "type": "object",
                "description": "Send a message to a Slack channel.",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID or name (e.g. '#general')."},
                    "text": {"type": "string", "description": "Message text."},
                },
                "required": ["channel", "text"],
            }
        }

    async def execute(self, call: ToolCall) -> Any:
        if not self._token:
            raise RuntimeError("SlackAdapter is not configured: set SLACK_BOT_TOKEN")
        args = call.arguments or {}
        channel = args.get("channel")
        text = args.get("text")
        if not channel or not text:
            raise ValueError("slack_send_message requires 'channel' and 'text'")
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"channel": channel, "text": text},
            )
        r.raise_for_status()
        return r.json()


# -----------------------------
# GitHub adapter
# -----------------------------

class GitHubAdapter:
    """Performs a small number of GitHub actions using a PAT.

    Configure via:
      - constructor token argument, or
      - GITHUB_TOKEN env var

    Tools:
      github_create_issue(repo: str, title: str, body: str)
      github_close_issue(repo: str, issue_number: int)
    """

    def __init__(self, token: Optional[str] = None) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN", "")

    @property
    def tool_names(self) -> List[str]:
        return ["github_create_issue", "github_close_issue"]

    @property
    def tool_schemas(self) -> Dict[str, Dict[str, Any]]:
        return {
            "github_create_issue": {
                "type": "object",
                "description": "Create a new issue on a GitHub repository.",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in 'owner/name' form."},
                    "title": {"type": "string", "description": "Issue title."},
                    "body": {"type": "string", "description": "Issue body (markdown)."},
                },
                "required": ["repo", "title"],
            },
            "github_close_issue": {
                "type": "object",
                "description": "Close an existing GitHub issue.",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in 'owner/name' form."},
                    "issue_number": {"type": "integer", "description": "Issue number to close."},
                },
                "required": ["repo", "issue_number"],
            },
        }

    async def execute(self, call: ToolCall) -> Any:
        if not self._token:
            raise RuntimeError("GitHubAdapter is not configured: set GITHUB_TOKEN")
        args = call.arguments or {}
        repo = args["repo"]
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            if call.tool == "github_create_issue":
                r = await client.post(
                    f"https://api.github.com/repos/{repo}/issues",
                    headers=headers,
                    json={"title": args["title"], "body": args.get("body", "")},
                )
            elif call.tool == "github_close_issue":
                num = args["issue_number"]
                r = await client.patch(
                    f"https://api.github.com/repos/{repo}/issues/{num}",
                    headers=headers,
                    json={"state": "closed"},
                )
            else:
                raise ValueError(f"GitHubAdapter does not handle {call.tool}")
        r.raise_for_status()
        return r.json()


# -----------------------------
# Generic webhook adapter
# -----------------------------

class WebhookAdapter:
    """A generic outbound webhook tool. Sends a JSON payload to a configured URL.

    Configure with:
        WebhookAdapter(tool_name="notify_oncall", url="https://...")

    The tool takes a single `payload` argument (any JSON object). Useful as a
    catch-all for things that don't yet have a dedicated adapter.
    """

    def __init__(self, tool_name: str, url: str, description: str = "Send a generic webhook.") -> None:
        self._tool_name = tool_name
        self._url = url
        self._description = description

    @property
    def tool_names(self) -> List[str]:
        return [self._tool_name]

    @property
    def tool_schemas(self) -> Dict[str, Dict[str, Any]]:
        return {
            self._tool_name: {
                "type": "object",
                "description": self._description,
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": "JSON object to POST to the webhook URL.",
                    },
                },
                "required": ["payload"],
            }
        }

    async def execute(self, call: ToolCall) -> Any:
        args = call.arguments or {}
        payload = args.get("payload", {})
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(self._url, json=payload)
        r.raise_for_status()
        try:
            return r.json()
        except json.JSONDecodeError:
            return {"status_code": r.status_code, "text": r.text[:1000]}
