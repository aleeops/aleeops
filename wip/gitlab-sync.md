# Quick Pre-Flight Checklist on Medium & High
No MRS will be created, commits are just pushed.
1. Repos must already exist on the destination GitLab under `--group` (Git cannot auto-create projects on push).
1. Allow force-push on `env-main`: Under Settings → Repository → Protected branches, enable Allowed to force push for your sync user/Maintainers on env-main (otherwise the drift-clobbering push will be rejected).
1. Internal CA certificates: If Medium or High uses an internal PKI/CA that `git` or `curl` doesn't trust by default, export `GIT_SSL_CAINFO=/path/to/ca-bundle.crt` and `CURL_CA_BUNDLE=/path/to/ca-bundle.crt` before running either script.
1. Run `--dry-run` first on the import script to verify every repo path, auth token, and branch update before clobbering.