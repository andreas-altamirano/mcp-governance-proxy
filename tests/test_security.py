"""Security regression tests.

These pin down two fixed vulnerabilities:

1. The /admin approval endpoints used to be open, so an agent could list its
   own held calls and approve them. They now require a bearer token.
2. GitHubAdapter used to paste `repo` straight into the request URL. It now
   validates `repo` and `issue_number`.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_governance_proxy import (
    CallStatus,
    EvaluationResult,
    GovernanceProxy,
    ToolCall,
    create_app,
)
from mcp_governance_proxy.adapters import (
    GitHubAdapter,
    _validate_github_repo,
    _validate_issue_number,
)
from tests.conftest import FakeAdapter

TOKEN = "s3cret-admin-token"


class HoldEverything:
    async def evaluate(self, call: ToolCall) -> EvaluationResult:
        return EvaluationResult(allow=False, hold=True, reason="needs human")


def _make_client(admin_token=TOKEN):
    adapter = FakeAdapter("danger")
    proxy = GovernanceProxy(evaluator=HoldEverything(), adapters=[adapter])
    client = TestClient(create_app(proxy, admin_token=admin_token))
    client.proxy = proxy  # handy for assertions
    return client, adapter


def _agent_call(client):
    """What an agent does: a plain MCP tools/call with no admin credentials."""
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "danger", "arguments": {"x": 1}},
    })
    assert r.status_code == 200
    return r.json()


# ----- admin auth -----

def test_agent_cannot_list_holds_without_token():
    client, _ = _make_client()
    _agent_call(client)
    assert client.get("/admin/holds").status_code == 401


def test_agent_cannot_self_approve():
    """The original attack: agent makes a call, finds its hold, approves it."""
    client, adapter = _make_client()
    _agent_call(client)
    # The agent is shown the hold id in the proxy's own response, so assume it
    # knows it. Knowing the id must not be enough to approve.
    hold_id = client.proxy.hold_queue.list_pending()[0].id

    assert client.post(f"/admin/holds/{hold_id}/approve").status_code == 401
    assert client.post(
        f"/admin/holds/{hold_id}/approve", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    assert client.post(f"/admin/holds/{hold_id}/reject").status_code == 401
    assert adapter.calls == []  # nothing executed


def test_admin_with_correct_token_can_approve():
    client, adapter = _make_client()
    _agent_call(client)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    hold_id = client.get("/admin/holds", headers=headers).json()["holds"][0]["id"]
    r = client.post(f"/admin/holds/{hold_id}/approve", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == CallStatus.EXECUTED.value
    assert len(adapter.calls) == 1


def test_admin_with_correct_token_can_reject():
    client, adapter = _make_client()
    _agent_call(client)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    hold_id = client.get("/admin/holds", headers=headers).json()["holds"][0]["id"]
    r = client.post(f"/admin/holds/{hold_id}/reject", headers=headers)
    assert r.status_code == 200
    assert adapter.calls == []


def test_non_bearer_scheme_rejected():
    client, _ = _make_client()
    assert client.get(
        "/admin/holds", headers={"Authorization": f"Basic {TOKEN}"}
    ).status_code == 401


def test_admin_disabled_when_no_token_configured(monkeypatch):
    monkeypatch.delenv("MCP_PROXY_ADMIN_TOKEN", raising=False)
    client, adapter = _make_client(admin_token=None)
    _agent_call(client)
    assert client.get("/admin/holds").status_code == 503
    assert client.post("/admin/holds/hold-abc/approve").status_code == 503
    # Even a bearer token can't work when none is configured.
    assert client.get(
        "/admin/holds", headers={"Authorization": "Bearer anything"}
    ).status_code == 503
    assert adapter.calls == []


def test_admin_token_from_environment(monkeypatch):
    monkeypatch.setenv("MCP_PROXY_ADMIN_TOKEN", "from-env")
    client, _ = _make_client(admin_token=None)
    assert client.get(
        "/admin/holds", headers={"Authorization": "Bearer from-env"}
    ).status_code == 200


def test_mcp_and_health_stay_open_to_agents():
    client, _ = _make_client()
    assert client.get("/health").status_code == 200
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200


def test_malformed_mcp_body_does_not_crash():
    client, _ = _make_client()
    r = client.post("/mcp", content=b"not json", headers={"content-type": "application/json"})
    assert r.json()["error"]["code"] == -32700
    r = client.post("/mcp", json=[1, 2, 3])
    assert r.json()["error"]["code"] == -32600


# ----- GitHub adapter input validation -----

@pytest.mark.parametrize("repo", [
    "owner/name",
    "my-org/my_repo.v2",
    "a/b",
])
def test_valid_repos_accepted(repo):
    assert _validate_github_repo(repo) == repo


@pytest.mark.parametrize("repo", [
    "",
    "owner",
    "owner/name/extra",
    "owner/../../user",
    "../name",
    "owner/..",
    "./name",
    "owner/name?x=1",
    "owner/name#frag",
    "owner/na me",
    "owner/name%2f..",
    "owner\\name",
    "/owner/name",
    None,
    123,
    ["owner", "name"],
])
def test_malformed_repos_rejected(repo):
    with pytest.raises(ValueError):
        _validate_github_repo(repo)


@pytest.mark.parametrize("num", [1, 42, 10**6])
def test_valid_issue_numbers(num):
    assert _validate_issue_number(num) == num


@pytest.mark.parametrize("num", [0, -1, "1", "1/../../x", 1.5, True, None])
def test_invalid_issue_numbers_rejected(num):
    with pytest.raises(ValueError):
        _validate_issue_number(num)


class _CapturingClient:
    """Stand-in for httpx.AsyncClient that records requests and never touches the network."""
    requests = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _respond(self, method, url, **kw):
        _CapturingClient.requests.append((method, url, kw))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    async def post(self, url, **kw):
        return await self._respond("POST", url, **kw)

    async def patch(self, url, **kw):
        return await self._respond("PATCH", url, **kw)


@pytest.mark.asyncio
async def test_github_adapter_never_sends_request_for_bad_repo(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    _CapturingClient.requests = []
    adapter = GitHubAdapter(token="ghp_fake")
    with pytest.raises(ValueError):
        await adapter.execute(ToolCall(
            tool="github_create_issue",
            arguments={"repo": "owner/name/../../../user", "title": "x"},
        ))
    with pytest.raises(ValueError):
        await adapter.execute(ToolCall(
            tool="github_close_issue",
            arguments={"repo": "owner/name", "issue_number": "1/../../x"},
        ))
    assert _CapturingClient.requests == []  # token was never sent anywhere


@pytest.mark.asyncio
async def test_github_adapter_builds_expected_url_for_good_input(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    _CapturingClient.requests = []
    adapter = GitHubAdapter(token="ghp_fake")
    await adapter.execute(ToolCall(
        tool="github_create_issue",
        arguments={"repo": "octo/hello", "title": "Bug", "body": "details"},
    ))
    await adapter.execute(ToolCall(
        tool="github_close_issue",
        arguments={"repo": "octo/hello", "issue_number": 7},
    ))
    urls = [(m, u) for m, u, _ in _CapturingClient.requests]
    assert urls == [
        ("POST", "https://api.github.com/repos/octo/hello/issues"),
        ("PATCH", "https://api.github.com/repos/octo/hello/issues/7"),
    ]
