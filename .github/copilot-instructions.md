# Copilot instructions

## Conversation language used
  British English

## Build, test, and lint

- Install the project and its runtime dependencies with `python -m pip install -e .`.
- Run the full test suite with `pytest pyvisa_py/testsuite`.
- Run one test with a pytest node ID, for example:
  `pytest pyvisa_py/testsuite/test_highlevel.py::TestPyVisaLibrary::test_list_backends`.
- Check formatting with `ruff format pyvisa_py --check` and lint with
  `ruff check pyvisa_py`.
- Run the type checker with `mypy pyvisa_py`.
- CI runs these checks and tests the package on Windows, macOS, and Linux.

## Architecture

PyVISA-py is a PyVISA backend: `pyvisa_py.WRAPPER_CLASS` exposes
`PyVisaLibrary`, which implements the library-facing API and dispatches
resource-specific calls to live session objects. `Session` in `sessions.py`
provides common VISA session behavior and a registry keyed by interface type
and resource class. Transport modules register their session classes in that
registry; `PyVisaLibrary` imports them so registration happens when the backend
loads.

Each transport session owns the low-level I/O interface and maps VISA
attributes, operations, and status codes onto it. TCPIP instrument sessions
select a protocol implementation (VXI-11, HiSLIP, or VICP); the protocol
implementations live in `protocols/`. USB sessions use the USBTMC or USB RAW
protocol helpers. Serial, GPIB, TCPIP socket, and Prologix sessions follow the
same session-dispatch model.

Transport libraries are optional. If a required package or system backend is
unavailable, the corresponding session can be registered as unavailable so
the rest of the backend remains usable; debug information reports which
resource implementations are available.

## Repository-specific conventions

- Keep VISA-facing behavior in the session/library layer and wire-level
  framing or transport details in `pyvisa_py/protocols/`.
- Register implementations with `Session.register(interface_type, resource_class)`.
  Use `Session.register_unavailable(...)` when a missing optional dependency
  prevents a resource type from being supported.
- Return PyVISA `StatusCode` values for VISA operations. Raise `OpenError`
  during session creation when opening a resource fails, so `PyVisaLibrary.open`
  can translate it into the appropriate VISA result.
- Session `attrs` provides stored/default attribute values and takes priority
  over `_get_attribute` and `_set_attribute`; don't store attributes there if
  the session's attribute handlers are meant to implement them.
- Tests live in `pyvisa_py/testsuite`. PyVISA API-level tests commonly use
  `pyvisa.testsuite.BaseTestCase`; focused protocol tests often use pytest
  functions and mocked low-level interfaces.
- Use the project's virtual environment from `.venv`. Activate it before
  running any development commands. If no virtual environment exists, ask 
  the user to create one, and to install the requirements from `dev-requirements.txt`, 
  as well as the requirements from `docs/requirements.txt`.
- Ruff is configured for an 88-character line length, import sorting, and a
  McCabe complexity limit of 20.
- Do not run ruff as `.venv/bin/ruff` directly; use the virtual environment.
- Do not perform any git operations other than diff, log, and status. Chained
  commands are permitted only when every Git operation is one of these types.
  Do not perform committing, pushing, or pulling changes.
- For Copilot harness sessions, `.github/hooks/deny-git.json` enforces this
  for Git tools and detected terminal commands.
  It is not an OS-level restriction and cannot prevent indirect Git execution.

## Coding guidelines

- Prefer clarity and maintainability over clever or overly concise code.
- Strive for consistency with existing code patterns and conventions within the project.
- Document any deviations from established patterns and conventions.
- Ensure that all code changes are accompanied by appropriate tests to verify correctness and prevent regressions.

## Comment guidelines

- Comment the reasoning behind non-obvious design decisions and implementation choices.
- Comment all public functions, classes, and modules with docstrings explaining their purpose, parameters, return values, and any exceptions raised.
- Use type hints for function parameters and return values to improve code readability and facilitate static analysis.
- Remove or adapt existing comments only if they are proven to be untrue or redundant.
- In the comments, when applicable, note the references to the relevant rules, recommendations, permissions and observations, as defined in the reference documents.

## Reference documents

- Unless provided in `references/`, use the following documents as standards references:
  - VISA specification: https://www.ivifoundation.org/downloads/VISA/vpp43_2024-01-04.pdf
  - VXI-11 specification: https://web.archive.org/web/20211021230242/http://www.vxibus.org/files/VXI_Specs/VXI-11.zip or https://www.eevblog.com/forum/testgear/vxibus-org-gone-who-maintainspublishes-the-vxi-11-specs/?action=dlattach;attach=2834422
  - HiSLIP specification: https://www.ivifoundation.org/downloads/Protocol%20Specifications/IVI-6.1_HiSLIP-2.0-2020-04-23.pdf

## Ask and plan

- Use Ask mode, or ask a focused question, when requirements leave a meaningful
  design or behavior choice unresolved; don't silently choose between
  materially different approaches.
- Use Plan mode for multi-file or otherwise substantial changes: outline the
  implementation steps before editing, then carry out the plan unless the user
  asks only for a plan.
