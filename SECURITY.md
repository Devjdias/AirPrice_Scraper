# Security Policy

This repository is intended to contain only source code, public documentation,
installation instructions, the open-source license, and safe example
configuration files.

Do not publish:

- `config/config.ini` with real local settings;
- service-account JSON files or any other credentials;
- Google Sheets IDs or private spreadsheet links;
- generated Excel files from `DADOS_BRUTOS/`;
- screenshots, saved HTML pages, or browser profiles from `LOGS/`;
- proxy lists, passwords, tokens, keys, or local `.env` files.

Use `config/config.example.ini` as the public configuration template.

If any credential, spreadsheet ID, token, or private file is accidentally
published, revoke or rotate it in the provider console before using the project
again. Then remove the exposed file from the repository and regenerate a clean
submission package.

To report security concerns, use the public project issue tracker without
including secrets, credentials, private URLs, or generated data in the report.
