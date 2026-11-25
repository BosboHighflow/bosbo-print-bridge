# BOSBO Print Bridge update channel

Current installed Windows version: V0.8.1. Target first online update: V0.8.2.

## Packaging
Each update ZIP must contain exactly:
- manifest.json
- app/bridge.py

Manifest product must be BOSBO_PRINT_BRIDGE. Version must match both source VERSION and GitHub tag vX.Y.Z. The manifest includes the SHA-256 of app/bridge.py.

## Publishing
Workflow .github/workflows/publish-update.yml packages the checked-in release/v0.8.2/bridge.py and creates the public v0.8.2 GitHub Release. The source file must be added before this workflow can run. Do not mark the Release as published until that source and workflow run are verified.

## Client
Windows client configured to BosboHighflow/bosbo-print-bridge checks GitHub API /releases/latest and expects asset BOSBO_Print_Bridge_GUNCELLEME_VX.Y.Z.zip, SHA-256 digest, and exact file size. Client validates archive and delegates installation to the external updater.

## Safety
- Real printing remains disabled in the V0.8 series.
- Never commit user data, jobs.sqlite3, settings.json, api_token.txt, GitHub tokens, or personal PDFs.
- Keep rollback backups, version consistency, and Windows file-lock regression tests.
- SHA-256 confirms integrity; a malicious release editor could change both file and digest, so restrict repository release permissions.
