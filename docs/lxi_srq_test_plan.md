# LXI SRQ Test Plan (Tests-Only Scope)

This plan intentionally covers only adding tests in pyvisa-py.

## Scope for this PR series

- Add LXI-assisted SRQ test modules mirroring queue/handler expectations used in pyvisa.
- Mark VXI-11 and HiSLIP SRQ tests as expected-fail where backend support is not yet implemented.
- Keep raw TCPIP SOCKET SRQ out of scope.
- Wire CI jobs to execute and report these tests without requiring them to pass yet.
- Use only the `pyvisa-tester` released fake instrument binary in CI test runs.

## Explicitly out of scope

- Implementing SRQ event support in pyvisa-py for VXI-11.
- Implementing SRQ event support in pyvisa-py for HiSLIP.

Those protocol support changes will be delivered in follow-up PRs.

## Fake instrument source policy

- CI must run tests against the `pyvisa-tester` prebuilt binary (`pyvisa-lxi-fake`).
- CI must not start lxi-rs example servers directly for SRQ tests.

## Planned test categories

1. Queue mechanism SRQ tests (wait_on_event flow)
2. Handler mechanism SRQ tests (install/uninstall callback flow)
3. Timeout behavior tests for queue waiting
4. Session cleanup tests for handlers

## CI posture

- Linux-only SRQ test job in GitHub Actions.
- Non-blocking status while backend implementation is pending.
