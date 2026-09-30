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

`tools/campaign_leads.py` finds and removes leads from Smartlead campaigns (same key and network needs):

- `scan --emails-file FILE`: read-only; which campaigns hold these leads. FILE is one address per line or a CSV.
- `remove --emails-file FILE`: remove matched leads from campaigns. Dry run unless `--apply`; `--skip-campaign ID_OR_NAME` protects a campaign.
- `data/SEP30_flyscale_upload_emails.txt` holds the SEP30 upload's addresses, pending removal from prior campaigns (asked 30 Sep; blocked then on the missing API key).

Rules:

- `remove` is a dry run unless `--apply`. Show the user the dry-run output (which campaigns, how many leads each) and get a yes before applying.

- `create-tag` and `assign-tag` are dry runs unless `--apply`. Show the user the dry-run output and get a yes before applying.
- Don't remove, rename, or re-tag accounts on existing tags unless the user names that tag.
- Tag names match exactly (case-insensitive). Run `tags` first when unsure of a name.
- When the user asks for domains, list domains, not full addresses.

API reference: https://github.com/Smartlead-Public/docs (`api-reference/`).
