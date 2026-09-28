# Flyscale ops

## Smartlead

`tools/smartlead.py` talks to the Smartlead API (Python standard library, nothing to install). It needs:

- `SMARTLEAD_API_KEY` in the environment. Never print, log, or commit it.
- Network access to `server.smartlead.ai`.

Commands (`python3 tools/smartlead.py <command> --help` for all options):

- `tags`: every email-account tag with the accounts on it (`--emails` for full addresses).
- `accounts --tag NAME --format domains|table|emails|json`: accounts on a tag. Narrow with `--domains a.com,b.com`, `--domains-file FILE`, `--newest N`.
- `create-tag NAME`: create an email-account tag.
- `assign-tag NAME --emails-file FILE`: add an existing tag to accounts (25 per API call).

Rules:

- `create-tag` and `assign-tag` are dry runs unless `--apply`. Show the user the dry-run output and get a yes before applying.
- Don't remove, rename, or re-tag accounts on existing tags unless the user names that tag.
- Tag names match exactly (case-insensitive). Run `tags` first when unsure of a name.
- When the user asks for domains, list domains, not full addresses.

API reference: https://github.com/Smartlead-Public/docs (`api-reference/`).
