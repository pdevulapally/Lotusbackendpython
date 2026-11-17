# Session continuity

When starting or resuming work in this backend, read the current checkpoint in
[the session handoff](SESSION_HANDOFF_2026-11-17.md) and confirm it against the
relevant source. Use the user's latest request to choose the work.

Update that checkpoint after each meaningful change, verification result, or
blocker, including before pausing mid-task. Record what is complete, what is
partially edited, which checks actually ran, and the next actionable step.
Keep credentials, tokens, and user data out of the handoff.
