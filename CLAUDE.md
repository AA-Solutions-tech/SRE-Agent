# SRE Agent (AA Solutions): releases-only repo

`AA-Solutions-tech/SRE-Agent` (local `D:\SRE-agent-aa`) holds **no source code**. It is the public download page for AA Solutions customers:
- `README.md`: install, configure, upgrade, uninstall.
- `docs/SRE-Agent-Deployment-Guide.html` / `.md`: new-install and upgrade guide (v1.1.0).
- GitHub **Releases**: `.deb` / `.rpm` packages and `SHA256SUMS`, branded **AA Solutions** (vendor, maintainer, homepage).

The code lives only in the private `syberviz-solutions/SRE-agent` (`D:\SRE-agent`). Its CI builds a second, AA-branded package set
(`BRAND=aa`) on every tag `v*` and publishes it here with the secret `AA_RELEASES_TOKEN` (fine-grained PAT, Contents read+write on this repo).
No CI, build or sync happens in this repo. Never add source code here.

Older commits (before the switch on 2026-11-10) still contain the full code in git history (kept on purpose; not wiped).
Released: v1.1.0 (built by the main repo CI; versions were reset on 2026-11-13 and all 2.x releases were deleted).

## Releasing for AA
Tag in the main repo; see `D:\SRE-agent\claude-implementation-docs\Deployment\release-and-aa-sync-runbook.md`. If the token is missing, publish by hand:
`gh run download <run> --repo syberviz-solutions/SRE-agent -p 'packages-aa-*' -D x && gh release create vX.Y.Z x/*/* --repo AA-Solutions-tech/SRE-Agent`
(add a `SHA256SUMS` first). Keep the guide's version strings and settings in step with each release.

## Conventions
Conventional commits with the Co-Authored-By trailer; list changed files after changes; no secrets; git writes only when the owner asks.
