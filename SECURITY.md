# Security Policy

## Reporting a Vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities or suspected credential exposure.

Instead, report vulnerabilities privately via GitHub Security Advisories (`Security > Advisories > Report a vulnerability`) or contact the repository maintainer directly.

## Operational Security Notes

- Always run live mail observation in a **Private Repository**. Never make a repository public if it contains a `state` branch or retained GitHub Actions logs from live runs.
- Never commit `.env` files, app passwords, webhook URLs, or personal database IDs.
- Use `--show-details` only in trusted local terminals; default CLI output omits email subjects, companies, and summaries so CI/Actions logs stay sanitized.
