# cuvm 0.2.0

Codex CLI 0.167.0 now runs in disposable macOS VMs with shell/file tools and
optional Playwright browser tools, alongside the existing Claude workflow.

- Scripted runs and interactive terminal sessions share the proxy, probes,
  artifact collection and teardown. Failed terminal attachment now fails the run;
  startup logs survive failures, and the reach check precedes interactive startup.
- `experiments/codex/web-30m.json` provides 30 minutes from terminal attachment.
  Build the documented web-dev input snapshot first. VM startup and collection
  take additional time. Enter your own prompts; no task script is injected.
- Project edits, added directories and home changes are collected. Known Codex
  reasoning events are accepted; unknown action types still fail the tool check.
- Credential setup uses an operator-owned Keychain item containing compact JSON
  without a refresh token. Credentials stay out of command arguments and the guest.

A completed operator web-development session reached its configured 1800-second
window (observed 1800.2 seconds), returned source/build artifacts and screenshots,
and passed containment, collection and cleanup checks. The agent completed its
Easter eggs request but ran out of time during the later sharks/whales request.
PASS describes harness checks, not task completion, design quality or fidelity to
a comparison experiment's prompt timing.

Native Codex desktop computer use and its private runtime are outside this
release. The public Codex route exposes shell/file and Playwright browser tools.
Network access is allowlisted, and backend tool checks are evidence after the
fact, not independent enforcement of server-side actions. See docs/design.md.

## Verification

Fresh public-clone verification runs with an empty HOME and no private imports:
538 discovered tests, 537 pass and one expected module skip for the private creep
analysis experiment. Python 3.14.7, OpenSSL 3.6.3.

A fresh public-clone scripted browser smoke, `codex-20261111T220238Z-6744`, passes:
31 pre-run and 30 post-run probes, exact page-heading and returned-file checks,
Playwright navigation/snapshot, collected edited HTML verified byte-for-byte at
the heading. No forbidden tool attempts or uses. Clone and proxy removed,
input archive and golden image unchanged; zero VMs running afterward.
