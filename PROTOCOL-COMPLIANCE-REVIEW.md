# VXI-11 and HiSLIP compliance review

## Scope

This is an implementation-side review of PyVISA-py's TCPIP instrument client
against the supplied VXI-11 Revision 1.0, IVI HiSLIP 2.0, and VPP-4.3
Revision 7.2.1 documents. The review does not use `docs/source/compliance.rst`
or `docs/source/faq.rst`. Findings limited to shared or nested locking are
excluded.

The code is a VISA client, not a VXI-11 instrument server or HiSLIP server.
Accordingly, server-only requirements are not attributed to this code.
Finding identifiers are retained below, including resolved findings. Qualified
gaps are listed separately where a feature is optional, the standard uses
recommendation language, or applicability depends on deployment.

## Reassessment (2026-10-26)

Statuses reflect the current worktree, including the H4 and H6 fixes. The
initial reassessment was documentation-only; the subsequent H4 implementation
and regression coverage are recorded below. The later DataEND/count/termination-
character revalidation adds H7-H10 without changing Python code.

| Finding | Status | Current assessment |
|---------|--------|--------------------|
| H1 | Open | Overlap mode and its VISA attribute are not wired to protocol behaviour. |
| H2 | Resolved | Async control transactions use the correct initial/reset MessageID. |
| H3 | Partially resolved | Channel draining and final feature handling are fixed; pending async requests can survive device clear. |
| H4 | Resolved | Buffered and in-flight responses are invalidated; both notification orders and resumable payload discard are covered. |
| H5 | Open | Receive errors do not perform the required Error/FatalError transactions. |
| H6 | Resolved | Complete synchronous operations, read status, and abort recovery are serialised. |
| H7 | Open | DataEND incorrectly accepts the wildcard MessageID reserved for Data. |
| H8 | Open | A MessageID mismatch discards the offending frame but not previously buffered read data. |
| H9 | Open | Ordinary HiSLIP read timeouts lose already-consumed bytes and report an empty result. |
| H10 | Open | A pending AsyncStatusQuery can lose or duplicate an RMT-delivered indication. |
| V1 | Resolved | VXI-11 RPC read timeouts now map to `VI_ERROR_TMO`. |
| V2 | Open | Unforced TCPIP INSTR opens do not fall back from VXI-11 to HiSLIP. |
| Q1 | Qualified gap | Secure HiSLIP is unsupported; conformance obligations depend on platform and deployment. |
| Q2 | Unverified | The default vendor ID remains `xx`; its registration has not been established. |
| Q3 | Qualified gap | VXI-11 cancellation remains unsupported, but its abort channel is optional for clients. |

## HiSLIP DataEND/count/termination-character revalidation

**Revalidated on:** 2026-10-26, against VPP-4.3 section 6.1.1 and Rules
6.1.1-6.1.5, and HiSLIP sections 3.1.2, 6.4, and 6.14. The checks exercise the
currently implemented synchronised mode; they do not establish overlapped-mode
compliance, which remains H1.

For valid MessageIDs, the ordinary read-termination paths pass the tested cases:

| Behaviour | Verified result |
|-----------|-----------------|
| DataEND payload completion | Non-empty DataEND reports END with its last payload byte; empty DataEND is also handled. |
| END, termination character and count coincide | Unsuppressed END wins and returns `VI_SUCCESS`. |
| Termination character and count coincide without an eligible END | Enabled TERMCHAR wins and returns `VI_SUCCESS_TERM_CHAR`. |
| Count before END or an enabled termination character | Returns exactly the requested count with `VI_SUCCESS_MAX_CNT`. |
| `VI_ATTR_TERMCHAR_EN = VI_FALSE` | Matching bytes are ordinary data; no `VI_SUCCESS_TERM_CHAR` is reported. |
| `VI_ATTR_SUPPRESS_END_EN = VI_TRUE` | END does not stop the read or produce `VI_SUCCESS`; character/count termination remains active. |
| Partial payload after count or character termination | A subsequent read returns the unconsumed suffix, without loss or duplication. |
| Empty messages and zero/negative counts | Empty END is handled; zero/negative counts do not consume queued data. |
| Fragmented writes and `VI_ATTR_SEND_END_EN` | Byte count, payload and MessageIDs are correct; only the final fragment is DataEND when enabled. |

A socket-pair matrix passed **312 read combinations**, each followed by a
remainder-preservation read. It varied packet splits, empty/non-empty messages,
count positions, termination bytes `0x00`, `0x0a`, and `0xff`, and both values
of TERMCHAR_EN and SUPPRESS_END_EN. The matrix used matching DataEND MessageIDs,
rather than relying on the wildcard accepted by some existing test fixtures.
Two raw-wire write cases also passed with SEND_END_EN enabled and disabled.

One observed boundary is that a count stop does not read ahead into a separate
empty DataEND frame: a read of the preceding Data payload returns
`VI_SUCCESS_MAX_CNT`, and the next read consumes the empty END and returns
`b""` with `VI_SUCCESS`.

These normal-path results are not a full compliance result. H7-H10 below are
separately reproduced failures in MessageID processing, error-result counts,
and concurrent RMT accounting.

## Findings

### H1. [OPEN] HiSLIP overlap mode is not implemented and the VISA attribute does not reflect or change the mode

**Standards:** HiSLIP 2.0 §3, especially §§3.1.2 and 3.2.2; VPP-4.3 Rule
5.1.17 and the description of `VI_ATTR_TCPIP_HISLIP_OVERLAP_EN` (page 5-66).

HiSLIP requires clients to support both synchronized and overlapped modes.
This implementation advertises protocol version 1.0, parses the server's
overlap indication as `bool(control_code)`, and initialises the VISA overlap
attribute to `VI_FALSE` unconditionally. Because the attribute is stored as a
plain scalar in `Session.attrs`, `Session.set_attribute()` updates that cached
value in place and returns `VI_SUCCESS` without calling the session's
`_set_attribute()` handler. No mode switch is wired to that attribute: the
read/write path remains synchronised. Thus, an instrument that suggests overlap
is not reflected in the default attribute, and setting the attribute to
`VI_TRUE` neither enables overlap behavior nor performs the required device
clear to change modes. A focused check confirmed that setting the attribute
returns `VI_SUCCESS` and changes its cached value without any protocol calls.
The final overlap bit is now copied into the attribute after device clear, but
that does not implement overlapped-mode I/O.

**Code:** [`InitializeResponse`](./pyvisa_py/protocols/hislip.py#L333),
[`initialize`](./pyvisa_py/protocols/hislip.py#L1281), HiSLIP session
attribute setup in [`tcpip.py`](./pyvisa_py/tcpip.py#L231), and attribute
dispatch in [`Session.set_attribute`](./pyvisa_py/sessions.py#L844) and
[`_set_attribute`](./pyvisa_py/tcpip.py#L641).

### H2. [RESOLVED] HiSLIP initial/reset MessageID in lock release and remote/local control transactions

**Standards:** HiSLIP 2.0 §6.5 (pages 39-40) and §6.7 (pages 44-45).

Both transactions now use `_message_id_for_async_request()`. It returns the
last transmitted MessageID when present, including a valid ID of zero, and
otherwise returns `(next_message_id - 2) & 0xffffffff`. After initialisation
or device clear, that produces the required `0xfffffefe`. Regression tests
cover both transactions, the initial/reset state, wraparound, and zero.

**Code:** [`_message_id_for_async_request`](./pyvisa_py/protocols/hislip.py#L903),
[`async_lock_release`](./pyvisa_py/protocols/hislip.py#L1369),
[`async_remote_local_control`](./pyvisa_py/protocols/hislip.py#L1386), and
[`test_hislip_async_control.py`](./pyvisa_py/testsuite/test_hislip_async_control.py).

### H3. [PARTIALLY RESOLVED] HiSLIP device clear does not abandon all pending async transactions

**Standard:** HiSLIP 2.0 §6.12 and §6.12.1 (pages 51-53).

The fixed 100 ms delay and discarded final feature bitmap are no longer
present. Device clear drains synchronous messages and partial payloads,
preserves FatalError handling, retains the final feature bitmap, and updates
the corresponding VISA attribute. The existing device-clear regressions pass.
The missing overlapped-mode implementation remains H1, not a lost-bitmap issue.

The remaining gap concerns pending asynchronous transactions. Section 6.12
requires an unacknowledged lock operation to be treated as incomplete if
`DeviceClearAcknowledge` arrives first. `begin_device_clear()` suppresses
service-request delivery, but `end_device_clear()` does not retire pending
requests; ordinary response dispatch can still complete them after the clear.

A socket-pair reproduction completed a full device-clear handshake with a
final feature bitmap of `1` while an exclusive-lock request was outstanding.
The old lock request remained pending after `DeviceClearAcknowledge`, and a
subsequent `AsyncLockResponse` still completed it with `success`.

**Code:** [`begin_device_clear`](./pyvisa_py/protocols/hislip.py#L494),
[`end_device_clear`](./pyvisa_py/protocols/hislip.py#L506),
[`_complete_pending`](./pyvisa_py/protocols/hislip.py#L644),
[`_perform_device_clear`](./pyvisa_py/protocols/hislip.py#L1179),
[`device_clear_complete`](./pyvisa_py/protocols/hislip.py#L1432), and
[`_update_feature_attributes`](./pyvisa_py/tcpip.py#L457).

### H4. [RESOLVED] HiSLIP interruption handling and response invalidation

**Standard:** HiSLIP 2.0 §3.1.2 and §6.11 (pages 22 and 50).

An async-first interruption now invalidates the current read's accumulated
bytes as well as any partially read Data/DataEND payload. The client drains
that payload without losing frame alignment, discards subsequent data messages
until `Interrupted`, and reports the abort rather than returning stale bytes.
A sync-first interruption clears response-completion state and gates further
sends until `AsyncInterrupted` arrives.

Read completion on byte count, termination character, or END is committed
under the same condition lock used to record async interruptions. An
interruption detected during payload receipt therefore cannot be bypassed by
an otherwise successful read-completion condition. The async dispatcher
records the interruption and fails pre-existing requests atomically with
async send registration, so newly unblocked requests are not accidentally
aborted. It still preserves a pending device-clear acknowledgement.

Payload receipt and discard retain progress after each socket read. If a
payload read or discard times out, a later read can finish discarding the
remaining payload and consume `Interrupted` without treating payload bytes
as a new header. Cancellation and H6 synchronous-operation locking remain
independent of async notification delivery.

Regressions cover partial payloads after count and termination-character
stops, interruptions during active reads and accumulated multi-packet
responses, both notification orders, actual async-reader delivery, missing
notifications, payload timeout/resume, short socket reads, and empty messages.
The previously reproduced `b"prefix-stale"` case now discards `b"stale"` and
raises `HiSLIPInterruptedError` after consuming the synchronous notification.

**Code:** [`AsyncChannel._run`](./pyvisa_py/protocols/hislip.py#L675),
[`Instrument._handle_async_interrupted`](./pyvisa_py/protocols/hislip.py#L909),
[`Instrument.receive`](./pyvisa_py/protocols/hislip.py#L1006),
[`_next_data_header`](./pyvisa_py/protocols/hislip.py#L1123),
[`_discard_pending_sync_payload`](./pyvisa_py/protocols/hislip.py#L1446), and
[`interruption regressions`](./pyvisa_py/testsuite/test_hislip_terminate.py).

### H5. [OPEN] HiSLIP protocol errors are raised locally instead of following the required Error/FatalError transactions

**Standard:** HiSLIP 2.0 §§6.2-6.3 (pages 35-36).

For a non-recoverable synchronization error, the client must send FatalError
on both channels and close the connection. For an unprocessable message that
does not lose synchronization, it must discard the message and reply with
Error on the channel where the message arrived. On the receive paths, malformed
prologues, unknown message types, and unexpected message types instead raise
`RuntimeError`; the ordinary message reader does not send the required error
message or reliably close/re-establish the connection. The protocol's
`fatal_error()` and `error()` message constructors are not wired into these
error paths (and `fatal_error()` only writes to the synchronous socket).

A malformed-prologue socket-pair check raised
`RuntimeError("protocol synchronization error")`, sent no error reply, and
left the synchronous socket open.

**Code:** [`RxHeader`](./pyvisa_py/protocols/hislip.py#L281),
[`AsyncChannel._read_message`](./pyvisa_py/protocols/hislip.py#L588),
[`AsyncChannel._run`](./pyvisa_py/protocols/hislip.py#L675),
[`fatal_error`](./pyvisa_py/protocols/hislip.py#L1517), and
[`error`](./pyvisa_py/protocols/hislip.py#L1521).

### H6. [RESOLVED] HiSLIP synchronous I/O concurrency and buffered-payload handling

**Standards:** VPP-4.3 Rule 3.6.1 and §6.1.4; HiSLIP 2.0 §3.1.2.

Synchronous reads, complete multi-packet writes, triggers, and device-clear
transactions now share a per-instrument reentrant lock. The lock covers
payload discard, parser state, and MessageID updates. The VISA read handler
holds the same lock until it has captured its completion status or finished
abort recovery, so another call cannot overwrite read flags or use the channel
before recovery completes. Cancellation and the async reader remain independent
of this lock, so `viTerminate` and interruption notifications can still run
while synchronous I/O is blocked. Response invalidation is covered separately
under H4.

The earlier unread-payload bug had already been fixed by
`_discard_pending_sync_payload()`. That drain is now protected by the same
operation lock. It clears the remaining payload of the already-validated
message, including after count or termination-character stops; it does not
blindly flush unparsed TCP bytes. Section 3.1.2 rule 3 applies to messages
already validated under rules 1 and 2.

Regression tests cover concurrent reads, writes, triggers and clears,
multi-packet write ordering and MessageIDs, read-status and abort-recovery
races, cancellation, lock release on failure, and payload discard before
sends and triggers. Individual operations are atomic; applications must still
coordinate multi-call write/read query sequences.

**Code:** [`Instrument.send`](./pyvisa_py/protocols/hislip.py#L976),
[`Instrument.receive`](./pyvisa_py/protocols/hislip.py#L1006),
[`device_clear`](./pyvisa_py/protocols/hislip.py#L1172),
[`_discard_pending_sync_payload`](./pyvisa_py/protocols/hislip.py#L1446),
[`TCPIPInstrHiSLIP.read`](./pyvisa_py/tcpip.py#L332), and
[`concurrency tests`](./pyvisa_py/testsuite/test_hislip_concurrency.py).

### H7. [OPEN] DataEND incorrectly accepts the wildcard MessageID

**Standards:** HiSLIP 2.0 section 3.1.2 rules 1-2 (pages 21-22) and section
6.4, Table 18 (page 38).

In synchronised mode, DataEND must carry the MessageID of the most recently
sent Data, DataEND, or Trigger. The `0xffffffff` exception applies to Data,
not DataEND. `_next_data_header()` applies that exception to both message types.

With the last transmitted MessageID set to `0x1234`, a queued DataEND with
ID `0xffffffff` and payload `b"stale"` was accepted and returned with
`VI_SUCCESS`, even though a valid `0x1234` DataEND containing `b"fresh"` followed.
The invalid DataEND should have been discarded rather than supplying data or
an END indication to the read.

Some existing socket-pair fixtures use wildcard DataEND IDs, so their passing
results do not establish this validation requirement.

**Code:** [`_next_data_header`](./pyvisa_py/protocols/hislip.py#L1123) and
[`existing receive fixtures`](./pyvisa_py/testsuite/test_hislip_terminate.py).

### H8. [OPEN] A MessageID mismatch does not clear accumulated read data

**Standard:** HiSLIP 2.0 section 3.1.2 rules 1-2 (pages 21-22).

On a Data or DataEND MessageID mismatch, the client must clear already-buffered
Data responses as well as discard the offending message. `_next_data_header()`
flushes the offending payload and continues, but does not invalidate the bytes
already accumulated in `receive()`'s local buffer.

Both variants were reproduced with last transmitted ID `0x1234`: a valid Data
frame supplies `b"prefix-"`, a Data or DataEND frame with ID `0x2222` supplies
`b"wrong"`, and a valid DataEND supplies `b"fresh"`. The read returns
`b"prefix-fresh"` with `VI_SUCCESS`, contrary to the requirement to clear the
prefix when the mismatch is detected. If the read continues to the following
valid message, its data must not include that invalidated prefix.

This is distinct from H4: the invalidation trigger here is a MessageID mismatch,
not an Interrupted/AsyncInterrupted notification.

**Code:** [`_next_data_header`](./pyvisa_py/protocols/hislip.py#L1123) and
[`Instrument.receive`](./pyvisa_py/protocols/hislip.py#L1006).

### H9. [OPEN] HiSLIP read timeouts lose already-consumed data and its count

**Standard:** VPP-4.3 section 6.1.1, the `buf` and `retCount` descriptions and
the synchronous-read description (pages 6-2 and 6-4).

The read buffer must contain the data read, and retCount describes the actual
number of bytes transferred. At the Python backend boundary these are
represented by the returned bytes. If `receive()` times out after accumulating
data, that local buffer is lost; `TCPIPInstrHiSLIP.read()` returns
`b"", VI_ERROR_TMO` unconditionally.

Two ordinary, non-interrupted reads reproduced this: a complete Data frame
containing `b"abc"` followed by a timeout waiting for DataEND, and a partially
received DataEND whose first three payload bytes are `b"abc"`. Both return an
empty result rather than those three bytes with `VI_ERROR_TMO`. After the server
supplies `b"def"`, the next read returns only `b"def"`; the consumed prefix is
not retained for a later call.

The timeout status itself is correct. This is a HiSLIP data/count loss, separate
from the resolved VXI-11 timeout-status finding V1 and from H4's intentional
discard of invalidated data during interruption.

**Code:** [`Instrument.receive`](./pyvisa_py/protocols/hislip.py#L1006),
[`TCPIPInstrHiSLIP.read`](./pyvisa_py/tcpip.py#L332), and
[`PyVisaLibrary.read`](./pyvisa_py/highlevel.py#L563).

### H10. [OPEN] A pending status query can lose or duplicate RMT-delivered

**Standards:** HiSLIP 2.0 sections 6.4 and 6.14 (pages 37-38 and 55-56), and
section 3.1.1 rule 2 (page 21).

RMT-delivered must be set only on the first flag-carrying message after RMT is
delivered to the application. `async_status_query()` clears `_rmt` only after
waiting for AsyncStatusResponse. The status-query lock serialises queries but
does not protect that delayed reset from concurrent synchronous I/O.

Two wire-level counterexamples were reproduced:

- **Lost RMT:** AsyncStatusQuery is sent with RMT `0`; a concurrent read then
  completes DataEND and sets `_rmt = 1`. The status reply resets it to `0`, so
  the next Trigger carries `0` instead of the required `1`.
- **Duplicate RMT:** AsyncStatusQuery is sent with RMT `1`; before its response,
  a Trigger also carries `1`, although that RMT was already reported and the
  Trigger should carry `0`.

These can leave MAV accounting incorrect or provoke a spurious interrupted
error at the server. This is an asynchronous/synchronous metadata race, separate
from H6's serialisation of complete synchronous operations.

**Code:** [`async_status_query`](./pyvisa_py/protocols/hislip.py#L1401),
[`Instrument.receive`](./pyvisa_py/protocols/hislip.py#L1006), and
[`trigger`](./pyvisa_py/protocols/hislip.py#L1482).

### V1. [RESOLVED] VXI-11 RPC-level read timeout status

**Standard:** VPP-4.3 §6.1.1 (page 6-3).

`CoreClient.device_read()` now catches `socket.timeout` and returns
`ErrorCodes.io_timeout, 0, b""`, which the session maps to `VI_ERROR_TMO`.
Regression tests verify the core result and the VISA read result, both before
any data is received and after a previously received prefix. Genuine
`io_error` results remain `VI_ERROR_IO`.

**Code:** [`CoreClient.device_read`](./pyvisa_py/protocols/vxi11.py#L248) and
[`test_tcpip_vxi11_read.py`](./pyvisa_py/testsuite/test_tcpip_vxi11_read.py).

### V2. [OPEN] Default `TCPIP::...::INSTR` opens do not fall back from VXI-11 to HiSLIP

**Standard:** VPP-4.3 Rule 5.1.31 (page 5-69).

If a TCPIP `INSTR` resource descriptor contains no device name forcing the
protocol choice, `viOpen()` must attempt a VXI-11 connection first and, if that
fails, attempt a HiSLIP connection. Because `rname.parse_resource_name()`
supplies the default `lan_device_name` of `inst0` and
`TCPIPInstrSession.__new__()` dispatches solely on
`parsed.lan_device_name.lower().startswith("hislip")`, opening
`TCPIP::<host>::INSTR` never falls back to HiSLIP when VXI-11 fails.
`PyVisaLibrary.open()` translates the failed open to a VISA result without a
second protocol attempt. A mocked-open check observed one VXI-11 attempt and
zero HiSLIP attempts for an unforced descriptor after a VXI-11 `OpenError`.

**Code:** [`TCPIPInstrSession.__new__`](./pyvisa_py/tcpip.py#L94) and
[`PyVisaLibrary.open`](./pyvisa_py/highlevel.py#L143).

## Qualified gaps and risks

### Q1. [QUALIFIED] HiSLIP 2.0 secure connections are not supported

**Standards:** HiSLIP 2.0 §§4.1 and 6.15 (pages 25 and 58-60); VPP-4.3
§2.8 and Rules 5.1.17-a/5.1.17-b.

The client advertises HiSLIP 1.0, parses but does not use the asynchronous
server-capability bits, and has no TLS/SASL secure-connection transaction.
It cannot establish a secure HiSLIP connection, so it cannot use an instrument
that requires secure communication unless that instrument separately permits
a legacy unencrypted connection. This is a capability/coverage gap rather
than a false claim to support HiSLIP 2.0: the implementation advertises 1.0.
VPP-4.3 §2.8 makes its deadline-bound security requirement specifically for
Windows VISA implementations released on or after January 1, 2025; Rules
5.1.17-a and 5.1.17-b also apply conditionally to secure TCPIP/HiSLIP systems.

**Code:** [`initialize`](./pyvisa_py/protocols/hislip.py#L1281),
[`AsyncInitializeResponse`](./pyvisa_py/protocols/hislip.py#L341), and HiSLIP
attribute setup in [`tcpip.py`](./pyvisa_py/tcpip.py#L232).

### Q2. [UNVERIFIED] Registration of the default HiSLIP client vendor ID `xx`

**Standard:** HiSLIP 2.0 §6.1 (pages 32-34).

The Initialize message uses the default `vendor_id=b"xx"`. The standard
describes this field as the client's two-character VPP-9 vendor abbreviation.
The repository does not establish that `xx` is an assigned abbreviation, but
the literal value alone does not prove that it is unregistered. This remains
an external registration check, not a confirmed wire-protocol violation. If
it is not assigned to this implementation's vendor, it is not the required
vendor ID.

**Code:** [`initialize`](./pyvisa_py/protocols/hislip.py#L1281).

### Q3. [QUALIFIED] VXI-11 cancellation is not implemented

**Standards:** VXI-11 §§B.2.2 and B.6.16; VPP-4.3 §3.5.1.1.

The VXI-11 session does not override the base `Session.terminate()`, which
returns `VI_ERROR_NSUP_OPER`, and the `abortPort` returned by `create_link` is
discarded. Thus `viTerminate()` cannot cancel a blocked VXI-11 operation.
However, VXI-11 permits a client connection to omit the optional abort
channel, and VPP's relevant cancellation description uses recommendation
language; treat this as an unsupported capability, not an unconditional
VXI-11 wire-protocol violation.

**Code:** base [`Session.terminate`](./pyvisa_py/sessions.py#L560),
VXI-11 `create_link` response handling in [`tcpip.py`](./pyvisa_py/tcpip.py#L844),
and the `DEVICE_ABORT` procedure definition in
[`vxi11.py`](./pyvisa_py/protocols/vxi11.py#L31).

## Reassessment evidence

The following existing tests were run in the repository's virtual environment
during the initial documentation-only reassessment, before the H4 follow-up:

```text
pytest pyvisa_py/testsuite/test_hislip_async_control.py pyvisa_py/testsuite/test_hislip_device_clear.py pyvisa_py/testsuite/test_tcpip_vxi11_read.py -q --tb=short
Result: 56 passed

pytest pyvisa_py/testsuite/test_hislip_terminate.py pyvisa_py/testsuite/test_hislip_concurrency.py -q --tb=short
Result: 72 passed
```

Separate mocked and socket-pair checks verified H1, H3, H5, V2, and Q3 and
identified the then-outstanding H4 partial-payload case without changing Python
files. Those passing tests did not cover the outstanding H3 and H4 cases.

### H4 follow-up validation

```text
pytest pyvisa_py/testsuite/test_hislip*.py pyvisa_py/testsuite/test_attributes.py pyvisa_py/testsuite/test_locks.py pyvisa_py/testsuite/test_open_timeout.py pyvisa_py/testsuite/test_remote_local.py pyvisa_py/testsuite/test_stb.py pyvisa_py/testsuite/test_trigger.py -q --tb=short
Result: 249 passed, 51 skipped
```

Ruff formatting and lint checks passed for both changed Python files. Targeted
mypy checks passed for the HiSLIP protocol and TCPIP session implementations.
The H4 follow-up adds coverage for the previously reproduced failure and the
related framing and notification-order races. H3's pending-async-request gap is
unchanged. No physical-instrument testing was performed.

### DataEND/count/termination-character revalidation evidence

```text
pytest pyvisa_py/testsuite/test_hislip_terminate.py pyvisa_py/testsuite/test_hislip_send.py pyvisa_py/testsuite/test_hislip_status_query.py pyvisa_py/testsuite/test_hislip_concurrency.py -q --tb=short
Result: 102 passed
```

Separate in-memory socket-pair checks passed the 312-case read matrix and its
follow-up reads, zero/negative count checks, and two raw-wire fragmented-write
cases. Further probes reproduced H7, both H8 mismatch types, both H9 timeout
positions, and both lost/duplicate RMT variants of H10. No Python source or
test files were changed during this revalidation, and no physical instruments
were used.

## Standards reviewed

- [IVI-6.1 HiSLIP 2.0 (2020-04-23)](./IVI-6.1_HiSLIP-2.0-2020-04-23.pdf)
- [VPP-4.3 Revision 7.2.1 (2024-01-04)](./vpp43_2024-01-04.pdf)
- [VXI-11 Revision 1.0](./vxi-11.pdf)
