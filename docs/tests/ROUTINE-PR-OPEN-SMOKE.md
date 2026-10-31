# Claude Code Routine — PR opened event smoke test

- Purpose: Verify that the configured GitHub `pull_request.opened` trigger starts a Claude Code Routine automatically for a new PR targeting `main`.
- Repository: `MinYongUm/my-netops-platform`
- Related design: Issue #9
- Scope: documentation-only trigger marker. No executable code, production configuration, API key, secret, or device access.
- Expected verification: inspect the Claude Code Routine execution history and confirm that this newly opened PR was discovered and, if supported, its PR activity subscription was established.
- Do not merge this test PR. Do not modify files or push changes as part of the subscription test.
