"""Restrict detected Git operations to diff, log, and status."""

import json
import re
import shlex
import sys
from typing import Iterator

GIT_TOOL = re.compile(r"(?:^|[^a-z0-9])git(?:$|[^a-z0-9])", re.IGNORECASE)
ALLOWED_GIT_TOOL = re.compile(
    r"(?:^|[^a-z0-9])git[-_.](?:diff|log|status|log[-_.]or[-_.]diff)$",
    re.IGNORECASE,
)
GIT_COMMAND = re.compile(r"\bgit(?:\.exe)?(?:[\"']?\s|$)", re.IGNORECASE)
ALLOWED_GIT_COMMANDS = {"diff", "log", "status"}
GIT_FLAGS = {
    "-p",
    "-P",
    "--paginate",
    "--no-pager",
    "--bare",
    "--no-replace-objects",
    "--literal-pathspecs",
    "--glob-pathspecs",
    "--noglob-pathspecs",
    "--icase-pathspecs",
    "--no-optional-locks",
}
GIT_VALUE_OPTIONS = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
}
TERMINAL_TOOL = re.compile(
    r"terminal|shell|bash|powershell|command|task", re.IGNORECASE
)
COMMAND_KEYS = {
    "bash",
    "cmd",
    "command",
    "powershell",
    "script",
    "shell",
    "terminalcommand",
}


def strings_in(value: object) -> Iterator[str]:
    """Yield strings contained in a tool argument or nested argument collection."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


def command_strings(value: object, terminal_tool: bool = False) -> Iterator[str]:
    """Extract commands, excluding terminal metadata when command fields exist."""
    if terminal_tool:
        commands = list(command_strings(value))
        yield from commands if commands else strings_in(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in COMMAND_KEYS:
                yield from strings_in(item)
            else:
                yield from command_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from command_strings(item)


def git_subcommands(command: str) -> Iterator[str]:
    """Yield Git operations across command chains and nested shell arguments.

    Global Git options are skipped before checking the operation. This detects
    direct invocations, not arbitrary indirect execution through scripts or aliases.
    Malformed shell quoting raises ValueError.
    """
    if not GIT_COMMAND.search(command):
        return

    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    tokens = list(lexer)
    for index, token in enumerate(tokens):
        executable = token.strip("`").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if executable in {"git", "git.exe"}:
            arguments = iter(tokens[index + 1 :])
            operation = next(arguments, "")
            while operation:
                if operation in GIT_FLAGS:
                    pass
                elif operation in GIT_VALUE_OPTIONS:
                    next(arguments, "")
                elif operation.partition("=")[
                    0
                ] in GIT_VALUE_OPTIONS or operation.startswith(("-C", "-c")):
                    pass
                else:
                    break
                operation = next(arguments, "")
            yield operation.strip("`").lower()
        elif token != command and GIT_COMMAND.search(token):
            yield from git_subcommands(token)


def deny(reason: str) -> None:
    """Emit the hook's denial response with the supplied reason."""
    print(
        json.dumps(
            {
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        )
    )


def main() -> None:
    """Validate a hook event from stdin and print its permission decision."""
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        deny("Could not validate this tool call; Git operations are restricted.")
        return

    if not isinstance(event, dict):
        deny("Could not validate this tool call; Git operations are restricted.")
        return

    tool_name = event.get("toolName")
    if not isinstance(tool_name, str):
        deny("Could not identify this tool call; Git operations are restricted.")
        return

    if GIT_TOOL.search(tool_name):
        if not ALLOWED_GIT_TOOL.search(tool_name):
            deny(
                "Only Git diff, log, and status operations are permitted in this workspace."
            )
            return

    terminal_tool = bool(TERMINAL_TOOL.search(tool_name))
    try:
        for command in command_strings(event.get("toolArgs"), terminal_tool):
            if any(
                subcommand not in ALLOWED_GIT_COMMANDS
                for subcommand in git_subcommands(command)
            ):
                deny(
                    "Only Git diff, log, and status operations are permitted in this workspace."
                )
                return
    except ValueError:
        deny("Could not parse this command; Git operations are restricted.")
        return

    print("{}")


if __name__ == "__main__":
    main()
