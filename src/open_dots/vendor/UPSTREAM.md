# Open Dots embedded runtime

Source: https://github.com/Anil-matcha/open-dots
Commit: c88141301523ba2e97316239d3a5c8265ba76bc8
License: MIT (LICENSE alongside this file).

`action_gateway.py` is adapted from server/app/services/action_gateway.py.
The action registry, policy checks, audited lifecycle, consume-before-dispatch,
async execution and execution locks come from upstream. Workspace commands,
global storage and singleton construction were removed; Zellu injects its own
domain services and case-bound audit sink. Only ActionRequest/ActionResult are
vendored from schemas/contracts.py. This is an embedded adaptation of the real
runtime, not installation of the upstream workspace UI or an official OpenAI SDK.
