# Working in this repository

Read `README.md` and `storage/maintenance/BUG_REGRESSIONS.md` before changing the pipeline. Keep daily operating instructions in README.md. The user explicitly requested permanent removal of obsolete audit outputs, one-off scripts, unused experimental models and previous cleanup backups; do not recreate archives of those files.

- Preserve existing user changes and `storage/projects`. Use a separate project for live-video audits.
- Treat database cue records, including an intentionally empty transcript, as authoritative. Never hide a database write failure by publishing only JSON.
- For Bilibili/long-video failures, check download byte ranges, source/trim identity, completed-scan manifests, cue IDs, actual voice files, timing and final encoded audio/video. Do not claim success from labels, paths or mocks.
- Add a focused regression test and a root-cause entry for each confirmed bug. Run focused tests while fixing, then the complete suite at a major milestone.
- Keep pytest offline and isolated. Run real provider/video/browser audits outside pytest; keep measured evidence for active work, and remove obsolete runs when requested by the user. Historical artifact paths in the regression log may no longer exist after the authorized cleanup. Do not lower QA thresholds to pass an audit.
- Reuse only cache whose source/settings/version identity matches. A preallocated file or an unfinished scan is not completed work.
