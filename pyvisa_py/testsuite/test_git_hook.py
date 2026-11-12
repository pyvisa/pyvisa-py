"""Regression tests for the workspace's Git permission hook."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".github" / "hooks" / "deny-git.py"


def _decision(event: object) -> dict:
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git diff && git log -1 && git status",
        "git diff; git log; git status",
        "git diff || git status",
        "git diff | cat; git status",
        "git diff\ngit log\ngit status",
        "git --no-pager diff; git --no-pager log -1",
        "GIT_PAGER=cat git diff --stat; git status",
        "git -C '/path with spaces' status && git -C . diff",
        "git -C. --no-pager diff && git --git-dir=.git status",
        "git -c color.ui=false diff && git --work-tree . status",
        "git -ccolor.ui=false --no-optional-locks status",
        "bash -c 'git --no-pager diff && git status'",
        '"/usr/bin/git" status',
        "git.exe status",
        "printf '%s\\n' ready && git status",
        "pytest pyvisa_py/testsuite",
    ],
)
def test_hook_allows_permitted_command_chains(command: str) -> None:
    """Permit read-only Git chains without interpreting descriptions as commands."""
    assert (
        _decision(
            {
                "toolName": "functions.bash",
                "toolArgs": {"command": command, "description": "Inspect Git changes"},
            }
        )
        == {}
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git add . && git diff",
        "git diff && git commit -m change",
        "git status; git pull",
        "git status || git reset --hard",
        "git log | git checkout main",
        "git diff\ngit fetch",
        "git --no-pager diff && git --no-pager push",
        "git -C '/path with spaces' commit -m change",
        "git -c alias.publish=push publish",
        "bash -c 'git status && git push'",
        '"/usr/bin/git" push',
        "git.exe push",
        "git -C",
        "git --unknown-option diff",
        "git 'status",
    ],
)
def test_hook_blocks_unpermitted_operations_in_chains(command: str) -> None:
    """Reject the entire tool call if any detected Git operation is not allowed."""
    response = _decision(
        {"toolName": "functions.bash", "toolArgs": {"command": command}}
    )
    assert response["permissionDecision"] == "deny"
    assert response["permissionDecisionReason"]


@pytest.mark.parametrize(
    "tool_name",
    ["GitKraken-git_status", "GitKraken-git_log_or_diff", "git_diff", "git_log"],
)
def test_hook_allows_permitted_git_tools(tool_name: str) -> None:
    """Apply the same allow-list to dedicated Git tools."""
    assert _decision({"toolName": tool_name, "toolArgs": {}}) == {}


@pytest.mark.parametrize(
    "tool_name",
    ["GitKraken-git_commit", "git_push", "git_checkout", "git_status_and_push"],
)
def test_hook_blocks_other_git_tools(tool_name: str) -> None:
    """Do not accept tool names that merely contain an allowed operation."""
    response = _decision({"toolName": tool_name, "toolArgs": {}})
    assert response["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": "git push"},
        {"tool_uses": [{"parameters": {"command": "git push"}}]},
        {"script": ["git status", "git push"]},
    ],
)
def test_hook_checks_nested_command_fields(arguments: dict) -> None:
    """Inspect commands passed through tools that wrap other tool calls."""
    response = _decision({"toolName": "parallel", "toolArgs": arguments})
    assert response["permissionDecision"] == "deny"


@pytest.mark.parametrize("event", [None, [], {}, {"toolName": 1}])
def test_hook_rejects_invalid_events(event: object) -> None:
    """Keep the existing fail-closed behaviour for invalid hook events."""
    response = _decision(event)
    assert response["permissionDecision"] == "deny"
