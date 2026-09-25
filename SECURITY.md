# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | ✅ Active Support  |

## Reporting a Vulnerability

If you discover a security vulnerability in Savitar Desktop, please report it responsibly:

1. **Do NOT open a public issue** for security vulnerabilities.
2. Email us at **muhammadkamranbashir52@gmail.com** with:
   - A description of the vulnerability
   - Steps to reproduce the issue
   - Expected vs. actual behavior
   - Your environment (Windows version, Savitar version)

We will acknowledge receipt within 48 hours and aim to provide a fix within 7 days for critical issues.

## Integrity Verification

Every official release includes a **SHA-256 checksum** in the release notes. Always verify your download:

```powershell
Get-FileHash -Path "Savitar-Setup-v1.0.0.exe" -Algorithm SHA256
```

## Scope

This policy covers the Savitar Desktop application and its official installer distributed through [GitHub Releases](https://github.com/kamstackbuild/Savitar-Desktop/releases).
