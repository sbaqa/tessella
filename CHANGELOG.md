# Changelog

## Unreleased

### Added
- Edit a ticket draft in the review queue before filing: every field and the title, marked as edited, with undo. A later triage keeps a reviewer's words.
- A GitHub Action (`uses: sbaqa/tessella@master`) that runs a real local model on the runner, triages the failures, comments on the pull request and uploads the report.
- `tessella ci --minutes`: a time limit for the model, so a slow runner reports what it did not reach.

### Changed
- The CI comment is useful with no model: failures are grouped with their kind, how often each failed before, which ones the board already has, and a link to the run.

## 0.1.0 (2026-11-08)

### Added
- Fixer agent for product bugs: reads the code, tries an edit in a temporary copy, keeps it only
  if the failing test passes every re-run and the suite fails nothing new. `tessella fix list`,
  `tessella fix apply` (a new branch, never pushed).
- Jira duplicate search, a board summary for every run, `tessella doctor --board`.
- `tessella ci --comment`: one summary comment on the GitHub PR or GitLab MR.
- Report: three tabs, key numbers, drag-and-drop panels, Board view with decisions.
- Review queue redesign, with the Allure report served and linked.
- Demo shop rebuilt with test personas, a three-step checkout and a 95-test Playwright suite.

### Fixed
- A decision now belongs to one failure, so a test that fails a new way is a new question.
- An approval that filed nothing is judged again once a board is set up.
- Jira tickets are written in wiki markup, so they no longer show literal asterisks and code fences.
- Screenshots are kept next to the store, so rebuilding a report cannot delete them.
- A measured time in an error message no longer makes a flaky failure look new.

### Earlier work

First working version: report readers, clustering, evidence gathering, verdicts on local models,
ticket drafts, review queue, report, offline mode.
