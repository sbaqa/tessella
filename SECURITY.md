# Security policy

Tessella is built for teams whose logs and code may not leave their network, so security reports
are very welcome.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private reporting instead:
[report a vulnerability](https://github.com/sbaqa/tessella/security/advisories/new).

Say what the problem is, how to reproduce it and what an attacker could do. You will get a reply
as soon as possible; Tessella is an early-stage project with no guaranteed response time yet.

## Supported versions

Only the latest commit on `master` is supported during early development.

## What Tessella promises

- Local models see your data; cloud models only see what your data policy allows.
- Every outbound request goes through one allowlisted client, and every model call is audited.
- Nothing is filed to a board without a person's approval, and fixes reach your code only through
  `tessella fix apply`, on a new branch.
- Secrets are read from environment variables and masked as data is read.

A way around any of these is a vulnerability. Known gaps are listed in the README under
"Security and privacy".
