# Working in this repository

Project rules for Gemini agents in Google Antigravity operating on `D:\duanlaylink`. Keep daily operating instructions in `README.md`, and keep this file standalone (no YAML frontmatter) and concise.

## 1. Environment, Scope & Context Budget
- **Target Hardware:** Windows 10, i3-10100F (4C/8T), 8 GB RAM, GTX 1050 Ti 4 GB VRAM. Design concurrency, batching, caching, and memory caps for this hardware envelope.
- **Strict Ignore:** Never scan `storage/projects/`, `venv/`, `__pycache__/`, `.git/`, or raw binary files (`*.sqlite`, `*.wav`, `*.mp4`); only inspect specific files inside ignored paths when explicitly tagged with `@` (or inspect SQLite read-only via approved audit scripts).
- **On-Demand Docs:** Only read `README.md` or `storage/maintenance/BUG_REGRESSIONS.md` when explicitly tagged with `@` or when changing documented workflows, commands, defaults, or logging a confirmed bug.
- **Economical Reading:** Locate symbols/call sites with targeted searches (`rg`) and read bounded line ranges instead of dumping large files.
- **No Obsolete Archives:** The user explicitly requested permanent removal of obsolete audit outputs, one-off scripts, unused experimental models, and previous cleanup backups; do not recreate archives of those files.

## 2. Data, Cache & Pipeline Integrity
- **Preserve User Data:** Preserve existing user changes and `storage/projects`. Always run live-video audits in a separate isolated project/directory.
- **Database Authority:** Treat database cue records, including an intentionally empty transcript, as authoritative. Never hide a database write failure by publishing only JSON.
- **Strict Identity & Completeness:** Reuse only cache/artifacts whose source/trim/settings/code/model/runtime identity matches. A preallocated file, `.part` file, or unfinished scan is not completed work.
- **End-to-End Evidence:** For Bilibili/long-video failures and live audits, verify download byte ranges, source/trim identity, completed-scan manifests, cue IDs, actual voice files, timing, and final encoded audio/video (full decode + QA metrics). Never claim success from labels, paths, or mocks.
- **Do Not Lower QA:** Never lower OCR confidence gates, timing limits, audio/loudness thresholds, or video QA gates to force an audit to pass.

## 3. Debugging, Testing & Experiments
- **Root Cause First:** When a run fails or falls short, stop and diagnose the verified root cause from logs/artifacts/DB before editing code; avoid speculative tweaks.
- **Tests & Bug Log:** Add a focused regression test and a root-cause entry in `storage/maintenance/BUG_REGRESSIONS.md` for each confirmed bug. Run focused tests while fixing, then the complete offline suite at a major milestone.
- **Offline vs. Live Audits:** Keep `pytest` strictly offline and isolated. Run real provider/video/browser audits outside `pytest` through the app's canonical entry points when validating end-to-end behavior; keep measured evidence for active work and remove obsolete runs when requested. (Historical artifact paths in `BUG_REGRESSIONS.md` may no longer exist after authorized cleanup.)
- **Doc Sync:** Whenever code changes a documented algorithm, workflow, command, default, or user-facing result, read `README.md` and update affected sections in the same change, verifying docs against the actual entry point and implementation.
- **Experiment Discipline:** Every optimization experiment must serve `D:\duanlaylink` with a recorded production integration target and measurable acceptance gate. Retire redundant trials after a decision, keep only necessary active evidence, never silently replace main-project work with a separate experimental product, stay within verified experiment paths during cleanup, and report tool/policy blocks honestly.

## 4. Gemini & Antigravity Execution Discipline
- **Complex Architecture & Long Videos:** For deep algorithm design, concurrency/OOM optimization (3–5h videos), or multi-file architecture changes, complete read-only root-cause analysis and produce an explicit implementation/verification plan before editing code; recommend `/plan` (or `/grill-me` / `/boost` when trade-offs need alignment) and prefer **Gemini Pro** for complex design & reasoning.
- **Two-Failure Stop Guard:** If a bug fix fails twice, stop patching immediately, re-verify runtime logs/state/inputs to isolate the true root cause, and recommend switching to **Gemini Pro** (or `/boost`) before attempting a third fix.
- **Model & Subagent Efficiency:** Keep heavy reasoning on **Gemini Pro**; use **Gemini Flash** (such as `flash` for browser subagents or `research` for isolated read-only surveys) for bounded UI/verification work, and use `/compact` when long sessions accumulate stale context.

## 5. Communication & Reporting
- **Concise Output:** After code edits, never print full file contents; report only changed functions/files, measured benchmark metrics, and verification results in at most 5 bullet points (unless a full report/plan was explicitly requested).
- **Strict Factual Accuracy:** State only directly observed paths, symbols, metrics, and post-edit test/audit outcomes; report exact scope, failures, skips, or blocks rather than summarizing partial runs as full passes.
