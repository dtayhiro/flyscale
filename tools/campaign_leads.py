#!/usr/bin/env python3
"""Smartlead helper for Flyscale: find and remove leads from campaigns.

Reads the API key from SMARTLEAD_API_KEY. Standard library only.
API reference: https://github.com/Smartlead-Public/docs (api-reference/)

  # Which campaigns hold these leads? (read-only)
  python3 tools/campaign_leads.py scan --emails-file data/SEP30_flyscale_upload_emails.txt

  # Remove them from prior campaigns (dry run; add --apply after a yes from the user)
  python3 tools/campaign_leads.py remove --emails-file data/SEP30_flyscale_upload_emails.txt
  python3 tools/campaign_leads.py remove --emails-file ... --skip-campaign 12345 --apply

The emails file is one address per line ('#' lines are comments). A CSV works
too: the address is taken from the first comma-separated field of each line
that looks like an email, so a full lead export can be passed as-is.
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from smartlead import SmartleadError, call, print_table, read_text, unwrap, warn

PAGE_SIZE = 100  # max page size of GET /campaigns/{id}/leads

EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def parse_emails(text):
    """One address per line, or the email column of a CSV; lowercased, de-duplicated."""
    seen, emails = set(), []
    for row in csv.reader(text.splitlines()):
        for cell in row:
            cell = cell.strip().lower()
            if cell.startswith("#"):
                break
            if EMAIL.match(cell):
                if cell not in seen:
                    seen.add(cell)
                    emails.append(cell)
                break  # at most one address per line
    return emails


def fetch_campaigns():
    campaigns = unwrap(call("GET", "/campaigns/"))
    if not isinstance(campaigns, list):
        raise SmartleadError(f"Unexpected response from GET /campaigns/: {str(campaigns)[:200]}")
    return campaigns


def fetch_campaign_leads(campaign_id):
    """Yield (lead_id, email, lead_status) for every lead in the campaign."""
    offset = 0
    while True:
        resp = call("GET", f"/campaigns/{campaign_id}/leads", {"offset": offset, "limit": PAGE_SIZE})
        page = resp.get("data") if isinstance(resp, dict) else resp
        if not isinstance(page, list):
            raise SmartleadError(
                f"Unexpected response from GET /campaigns/{campaign_id}/leads: {str(resp)[:200]}"
            )
        for entry in page:
            lead = entry.get("lead") or entry
            email = (lead.get("email") or "").strip().lower()
            if lead.get("id") and email:
                yield lead["id"], email, entry.get("status") or "-"
        if len(page) < PAGE_SIZE:
            return
        offset += PAGE_SIZE


def campaign_label(c):
    return f'{c.get("name") or "(unnamed)"} (id {c.get("id")}, {c.get("status") or "?"})'


def wanted_campaigns(args, campaigns):
    skip = {s.strip().casefold() for s in args.skip_campaign or []}
    kept, skipped = [], []
    for c in campaigns:
        key_id, key_name = str(c.get("id")), (c.get("name") or "").strip().casefold()
        (skipped if key_id in skip or key_name in skip else kept).append(c)
    unmatched = skip - {str(c.get("id")) for c in campaigns} - {
        (c.get("name") or "").strip().casefold() for c in campaigns
    }
    if unmatched:
        raise SmartleadError(f"--skip-campaign matched nothing: {', '.join(sorted(unmatched))}")
    return kept, skipped


def scan(args):
    """Match the emails against every campaign's leads. Returns per-campaign matches."""
    emails = set(parse_emails(read_text(args.emails_file)))
    if not emails:
        raise SmartleadError("No email addresses in the input.")
    campaigns, skipped = wanted_campaigns(args, fetch_campaigns())
    print(f"{len(emails)} uploaded addresses; {len(campaigns)} campaigns to scan", file=sys.stderr)
    for c in skipped:
        print(f"  skipping {campaign_label(c)}", file=sys.stderr)

    matches = defaultdict(list)  # campaign id -> [(lead_id, email, lead_status)]
    for c in campaigns:
        found = [(lid, e, st) for lid, e, st in fetch_campaign_leads(c["id"]) if e in emails]
        if found:
            matches[c["id"]] = found
        print(f"  scanned {campaign_label(c)}: {len(found)} matches", file=sys.stderr)
    return emails, campaigns, matches


def report(emails, campaigns, matches):
    by_id = {c["id"]: c for c in campaigns}
    rows = [
        [
            cid,
            by_id[cid].get("name") or "(unnamed)",
            by_id[cid].get("status") or "?",
            len(found),
        ]
        for cid, found in sorted(matches.items(), key=lambda kv: -len(kv[1]))
    ]
    matched_emails = {e for found in matches.values() for _, e, _ in found}
    print(f"\n{len(matched_emails)} of {len(emails)} uploaded leads found in {len(matches)} campaigns\n")
    if rows:
        print_table(["CAMPAIGN ID", "NAME", "STATUS", "MATCHED LEADS"], rows)


def cmd_scan(args):
    emails, campaigns, matches = scan(args)
    report(emails, campaigns, matches)
    if args.out:
        payload = {
            str(cid): [{"lead_id": lid, "email": e, "status": st} for lid, e, st in found]
            for cid, found in matches.items()
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nMatches written to {args.out}")


def cmd_remove(args):
    emails, campaigns, matches = scan(args)
    report(emails, campaigns, matches)
    total = sum(len(found) for found in matches.values())
    if not total:
        print("\nNothing to remove.")
        return
    if not args.apply:
        print(
            f"\nDry run: nothing changed. {total} lead-campaign pairs would be removed."
            "\nShow this to the user and re-run with --apply after a yes."
        )
        return

    by_id = {c["id"]: c for c in campaigns}
    done = failed = 0
    for cid, found in matches.items():
        for lid, email, _ in found:
            try:
                call("DELETE", f"/campaigns/{cid}/leads/{lid}", idempotent=False)
                done += 1
            except SmartleadError as e:
                failed += 1
                warn(f"{email} in campaign {cid}: {e}")
            if done and done % 100 == 0:
                print(f"  removed {done}/{total}", file=sys.stderr)
        print(f"Removed {len(found) - failed} leads from {campaign_label(by_id[cid])}")
    print(f"\nDone: {done} removed, {failed} failed.")
    if failed:
        print("Re-run the same command to retry the failures; already-removed leads no longer match.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[4:]),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="read-only: which campaigns hold these leads")
    p.add_argument("--emails-file", required=True, help="addresses, one per line or a CSV ('-' for stdin)")
    p.add_argument("--skip-campaign", action="append", metavar="ID_OR_NAME", help="leave this campaign alone")
    p.add_argument("--out", help="also write the matches to this JSON file")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("remove", help="remove matched leads from campaigns (dry run unless --apply)")
    p.add_argument("--emails-file", required=True, help="addresses, one per line or a CSV ('-' for stdin)")
    p.add_argument("--skip-campaign", action="append", metavar="ID_OR_NAME", help="leave this campaign alone")
    p.add_argument("--apply", action="store_true", help="actually remove them")
    p.set_defaults(func=cmd_remove)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except SmartleadError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
