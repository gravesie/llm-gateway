---
description: Block real external side effects at the choke point before writing test assertions; loaded when a test file is opened.
paths: ["**/test_*.py", "**/*_test.py", "**/conftest.py", "**/tests/**", "**/*.test.ts", "**/*.test.js", "**/*.spec.ts", "**/*.spec.js"]
---

# Tests that touch a real DB or HTTP route

- The moment a test suite starts touching a real DB/HTTP route (not a pure function), before writing the first assertion, check whether that code path (or anything reachable from it) sends a real external side effect — email, SMS, webhook, payment. If yes, add a global autouse fixture (e.g. conftest.py) that blocks/mocks the shared choke point in the same commit, not per-test. Reason: relying on each test to remember to mock the specific call it hits fails silently — a real Resend email went out from 8 of 9 test functions in one session before I caught the pattern. Verify the guard actually raises before trusting it.
