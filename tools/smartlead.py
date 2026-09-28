#!/usr/bin/env python3
"""Smartlead helper for Flyscale: email-account tags and tag assignment.

Reads the API key from SMARTLEAD_API_KEY. Standard library only.
API reference: https://github.com/Smartlead-Public/docs (api-reference/)

  python3 tools/smartlead.py tags                     # every tag and the accounts on it
  python3 tools/smartlead.py accounts --tag "Set 1 Flyscale" --format domains
  python3 tools/smartlead.py create-tag "Flyscale OS"             # dry run; add --apply
  python3 tools/smartlead.py assign-tag "Flyscale OS" --emails-file picks.txt   # dry run; add --apply
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from difflib import get_close_matches
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = os.environ.get("SMARTLEAD_BASE_URL", "https://server.smartlead.ai/api/v1").rstrip("/")
PAGE_SIZE = 100  # max page size of GET /email-accounts/
TAG_BATCH_SIZE = 25  # max accounts per POST /email-accounts/tag-mapping
MAX_ATTEMPTS = 5
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class SmartleadError(Exception):
    pass


# --- API -------------------------------------------------------------------


def api_key():
    key = os.environ.get("SMARTLEAD_API_KEY", "").strip()
    if not key:
        raise SmartleadError(
            "SMARTLEAD_API_KEY is not set. Add it to the environment "
            "(Claude Code on the web: environment settings, then start a new session)."
        )
    return key


def retry_delay(err, attempt):
    try:
        return min(max(float(err.headers.get("Retry-After")), 1.0), 60.0)
    except (AttributeError, TypeError, ValueError):
        return min(2**attempt, 60)


def call(method, path, params=None, body=None, idempotent=True):
    """Call the API. Retries 429s always; 5xx and network errors only when idempotent."""
    key = api_key()
    url = f"{BASE_URL}{path}?{urlencode({**(params or {}), 'api_key': key})}"
    data = None if body is None else json.dumps(body).encode()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "flyscale-smartlead/1.0",
    }
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urlopen(Request(url, data=data, method=method, headers=headers), timeout=60) as resp:
                raw = resp.read()
            break
        except HTTPError as e:
            retryable = e.code == 429 or (e.code >= 500 and idempotent)
            if retryable and attempt < MAX_ATTEMPTS:
                time.sleep(retry_delay(e, attempt))
                continue
            detail = e.read().decode("utf-8", "replace")[:500].replace(key, "***")
            raise SmartleadError(f"{method} {path} failed with HTTP {e.code}: {detail}") from None
        except OSError as e:  # URLError, timeouts, connection resets
            reason = str(getattr(e, "reason", e)).replace(key, "***")
            if "Tunnel connection failed: 403" in reason:
                raise SmartleadError(
                    f"{method} {path}: the network proxy blocked server.smartlead.ai. "
                    "Allow that domain in the environment's network settings."
                ) from None
            if idempotent and attempt < MAX_ATTEMPTS:
                time.sleep(min(2**attempt, 60))
                continue
            raise SmartleadError(f"{method} {path} failed: {reason}") from None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise SmartleadError(f"{method} {path} returned non-JSON: {raw[:200]!r}") from None


def unwrap(resp):
    """Some endpoints return a bare value, others {"ok": true, "data": ...}."""
    if isinstance(resp, dict) and "data" in resp:
        return resp["data"]
    return resp


def fetch_accounts():
    accounts, offset = {}, 0
    while True:
        page = unwrap(call("GET", "/email-accounts/", {"offset": offset, "limit": PAGE_SIZE}))
        if not isinstance(page, list):
            raise SmartleadError(f"Unexpected response from GET /email-accounts/: {str(page)[:200]}")
        new = [a for a in page if a.get("id") not in accounts]
        accounts.update((a.get("id"), a) for a in new)
        if len(page) < PAGE_SIZE or not new:
            return list(accounts.values())
        offset += PAGE_SIZE


def fetch_tags():
    tags = unwrap(call("GET", "/email-accounts/tags"))
    if not isinstance(tags, list):
        raise SmartleadError(f"Unexpected response from GET /email-accounts/tags: {str(tags)[:200]}")
    return tags


# --- Helpers ---------------------------------------------------------------


def email_of(account):
    return (account.get("from_email") or account.get("username") or "").strip().lower()


def domain_of(account):
    return email_of(account).rpartition("@")[2]


def tag_ids_of(account):
    return {t.get("tag_id") for t in account.get("tags") or []}


def connected(account):
    return bool(account.get("is_smtp_success")) and bool(account.get("is_imap_success"))


def reputation(account):
    rep = str((account.get("warmup_details") or {}).get("warmup_reputation") or "").rstrip("%")
    try:
        return float(rep)
    except ValueError:
        return None


def added(account):
    return (account.get("created_at") or "")[:10]


def newest_first(accounts):
    return sorted(accounts, key=lambda a: (a.get("created_at") or "", email_of(a)), reverse=True)


def tag_index(tags, accounts):
    """All tags by id, including any that only show up on accounts."""
    index = {t.get("id"): {"id": t.get("id"), "name": t.get("name") or ""} for t in tags}
    for account in accounts:
        for t in account.get("tags") or []:
            index.setdefault(t.get("tag_id"), {"id": t.get("tag_id"), "name": t.get("tag_name") or ""})
    return index


def resolve_tag(value, index):
    wanted = value.strip().casefold()
    matches = [t for t in index.values() if t["name"].strip().casefold() == wanted]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(str(t["id"]) for t in matches)
        raise SmartleadError(f'Several tags are named "{value}" (ids {ids}); pass the id instead.')
    if value.strip().isdigit() and int(value) in index:
        return index[int(value)]
    names = [t["name"] for t in index.values()]
    close = get_close_matches(value, names, n=5, cutoff=0.4)
    hint = f" Did you mean: {', '.join(repr(n) for n in close)}?" if close else ""
    raise SmartleadError(f'No tag named "{value}".{hint} Run the "tags" command to see them all.')


def parse_list(text):
    """Comma/whitespace separated tokens, lowercased and de-duplicated; '#' lines are comments."""
    seen, items = set(), []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for token in re.split(r"[,\s]+", line.strip().lower()):
            if token and token not in seen:
                seen.add(token)
                items.append(token)
    return items


def read_text(source):
    if source == "-":
        return sys.stdin.read()
    try:
        with open(source, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise SmartleadError(f"Cannot read {source}: {e.strerror}") from None


def normalize_domain(token):
    return re.sub(r"^(https?://)?@?", "", token).rstrip("/")


def wrap_items(items, width=98):
    """Join items with ', ' into lines of at most `width`, never splitting an item."""
    lines = [""]
    for item in items:
        if lines[-1] and len(lines[-1]) + len(item) + 2 > width:
            lines[-1] += ","
            lines.append(item)
        else:
            lines[-1] += f", {item}" if lines[-1] else item
    return lines


def print_table(headers, rows):
    widths = [max([len(h)] + [len(str(r[i])) for r in rows]) for i, h in enumerate(headers)]
    for row in [headers] + rows:
        print("  ".join(str(cell).ljust(w) for cell, w in zip(row, widths)).rstrip())


def warn(message):
    print(f"warning: {message}", file=sys.stderr)


# --- Commands --------------------------------------------------------------


def cmd_tags(args):
    tags, accounts = fetch_tags(), fetch_accounts()
    index = tag_index(tags, accounts)
    members, untagged = defaultdict(list), []
    for account in accounts:
        ids = tag_ids_of(account)
        for tag_id in ids:
            members[tag_id].append(account)
        if not ids:
            untagged.append(account)

    print(f"{len(index)} tags, {len(accounts)} email accounts ({len(untagged)} untagged)\n")
    groups = [
        (f'{t["name"]} (id {t["id"]})', members.get(t["id"], []))
        for t in sorted(index.values(), key=lambda t: t["name"].casefold())
    ]
    if untagged:
        groups.append(("(untagged)", untagged))
    for label, group in groups:
        domains = Counter(domain_of(a) for a in group)
        print(f"{label}: {len(group)} accounts on {len(domains)} domains")
        if args.emails:
            for email in sorted(email_of(a) for a in group):
                print(f"  {email}")
        elif domains:
            for line in wrap_items([f"{d} ({n})" for d, n in sorted(domains.items())]):
                print(f"  {line}")
        print()


def cmd_accounts(args):
    tags, accounts = fetch_tags(), fetch_accounts()
    index = tag_index(tags, accounts)
    label = "All accounts"
    if args.tag:
        tag = resolve_tag(args.tag, index)
        label = tag["name"]
        accounts = [a for a in accounts if tag["id"] in tag_ids_of(a)]

    wanted = []
    if args.domains:
        wanted += parse_list(args.domains)
    if args.domains_file:
        wanted += parse_list(read_text(args.domains_file))
    if wanted:
        wanted = {normalize_domain(d) for d in wanted}
        missing = sorted(wanted - {domain_of(a) for a in accounts})
        if missing:
            warn(f"not found in {label}: {', '.join(missing)}")
        accounts = [a for a in accounts if domain_of(a) in wanted]

    accounts = newest_first(accounts)
    if args.newest is not None:
        if args.newest < 1:
            raise SmartleadError("--newest must be at least 1")
        accounts = accounts[: args.newest]

    if args.format == "emails":
        for account in accounts:
            print(email_of(account))
    elif args.format == "json":
        names = {t["id"]: t["name"] for t in index.values()}
        slim = [
            {
                "id": a.get("id"),
                "email": email_of(a),
                "domain": domain_of(a),
                "created_at": a.get("created_at"),
                "connected": connected(a),
                "warmup_status": (a.get("warmup_details") or {}).get("status"),
                "warmup_reputation": reputation(a),
                "campaign_count": a.get("campaign_count"),
                "tags": sorted(names.get(i, str(i)) for i in tag_ids_of(a)),
            }
            for a in accounts
        ]
        print(json.dumps(slim, indent=2))
    elif args.format == "domains":
        by_domain = defaultdict(list)
        for account in accounts:
            by_domain[domain_of(account)].append(account)
        rows = []
        for domain, group in by_domain.items():
            reps = [r for r in map(reputation, group) if r is not None]
            rows.append([
                domain,
                len(group),
                f"{sum(map(connected, group))}/{len(group)}",
                f"{min(reps):g}%" if reps else "-",
                max(added(a) for a in group) or "-",
                "yes" if args.brand and args.brand.lower() in domain else "",
            ])
        rows.sort(key=lambda r: r[0])
        rows.sort(key=lambda r: r[4], reverse=True)  # newest first, then by domain
        print(f"{label}: {len(accounts)} mailboxes on {len(rows)} domains\n")
        print_table(["DOMAIN", "BOXES", "CONNECTED", "MIN REP", "NEWEST ADDED", "BRANDED"], rows)
    else:
        names = {t["id"]: t["name"] for t in index.values()}
        rows = [
            [
                a.get("id"),
                email_of(a),
                added(a) or "-",
                "yes" if connected(a) else "NO",
                (a.get("warmup_details") or {}).get("status") or "-",
                f"{reputation(a):g}%" if reputation(a) is not None else "-",
                a.get("campaign_count", "-"),
                ", ".join(sorted(names.get(i, str(i)) for i in tag_ids_of(a))),
            ]
            for a in accounts
        ]
        print(f"{label}: {len(accounts)} accounts\n")
        print_table(["ID", "EMAIL", "ADDED", "CONNECTED", "WARMUP", "REP", "CAMPAIGNS", "TAGS"], rows)


def cmd_create_tag(args):
    name = args.name.strip()
    if not name:
        raise SmartleadError("Tag name is empty.")
    if args.color and not HEX_COLOR.match(args.color):
        raise SmartleadError("--color must look like #RRGGBB")
    existing = [t for t in fetch_tags() if (t.get("name") or "").strip().casefold() == name.casefold()]
    if existing:
        t = existing[0]
        raise SmartleadError(f'A tag named "{t.get("name")}" already exists (id {t.get("id")}). Nothing created.')
    color = f" with color {args.color}" if args.color else ""
    if not args.apply:
        print(f'Dry run: would create tag "{name}"{color}. Re-run with --apply to create it.')
        return
    body = {"name": name, **({"color": args.color} if args.color else {})}
    try:
        created = unwrap(call("POST", "/tags", body=body, idempotent=False)) or {}
    except SmartleadError as e:
        raise SmartleadError(f'{e}\nRun "tags" to check whether "{name}" was created before retrying.') from None
    print(f'Created tag "{created.get("name", name)}" (id {created.get("id")}){color}.')


def cmd_assign_tag(args):
    emails = parse_list(read_text(args.emails_file))
    if not emails:
        raise SmartleadError("No email addresses in the input.")
    tags, accounts = fetch_tags(), fetch_accounts()
    tag = resolve_tag(args.name, tag_index(tags, accounts))

    by_email = defaultdict(list)
    for account in accounts:
        by_email[email_of(account)].append(account)
    unknown = [e for e in emails if e not in by_email]
    if unknown:
        raise SmartleadError(
            f"{len(unknown)} address(es) are not Smartlead email accounts; nothing changed:\n  "
            + "\n  ".join(unknown)
        )
    targets = [a for e in emails for a in by_email[e]]
    already = [a for a in targets if tag["id"] in tag_ids_of(a)]
    todo = [a for a in targets if tag["id"] not in tag_ids_of(a)]
    current = sum(tag["id"] in tag_ids_of(a) for a in accounts)

    print(f'Tag "{tag["name"]}" (id {tag["id"]}) has {current} accounts now.')
    print(f"{len(todo)} to add, {len(already)} already tagged -> {current + len(todo)} accounts after.\n")
    for account in todo:
        print(f"  + {email_of(account)}")
    for account in already:
        print(f"  = {email_of(account)} (already tagged)")
    if not todo:
        return
    if not args.apply:
        print("\nDry run: nothing changed. Re-run with --apply to assign the tag.")
        return

    ids = [a["id"] for a in todo]
    for start in range(0, len(ids), TAG_BATCH_SIZE):
        batch = ids[start : start + TAG_BATCH_SIZE]
        call("POST", "/email-accounts/tag-mapping", body={"email_account_ids": batch, "tag_ids": [tag["id"]]})
        print(f"Tagged {start + len(batch)}/{len(ids)}")
    final = sum(tag["id"] in tag_ids_of(a) for a in fetch_accounts())
    print(f'Done. "{tag["name"]}" now has {final} accounts.')
    if final != current + len(todo):
        warn(f"expected {current + len(todo)} accounts on the tag; Smartlead reports {final}.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[4:]),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("tags", help="list every email-account tag and the accounts on it")
    p.add_argument("--emails", action="store_true", help="list full addresses instead of domains")
    p.set_defaults(func=cmd_tags)

    p = sub.add_parser("accounts", help="list email accounts, optionally only those on one tag")
    p.add_argument("--tag", help="tag name (case-insensitive) or id")
    p.add_argument("--domains", help="only these sending domains, comma-separated")
    p.add_argument("--domains-file", help="only the domains listed in this file ('-' for stdin)")
    p.add_argument("--newest", type=int, metavar="N", help="only the N most recently added accounts")
    p.add_argument("--format", choices=["table", "domains", "emails", "json"], default="table")
    p.add_argument("--brand", default="flyscale", help="word marking a branded domain (default: flyscale)")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("create-tag", help="create a new email-account tag (dry run unless --apply)")
    p.add_argument("name")
    p.add_argument("--color", help="hex color, e.g. #4CAF50")
    p.add_argument("--apply", action="store_true", help="actually create it")
    p.set_defaults(func=cmd_create_tag)

    p = sub.add_parser("assign-tag", help="add an existing tag to email accounts (dry run unless --apply)")
    p.add_argument("name", help="tag name (case-insensitive) or id")
    p.add_argument("--emails-file", required=True, help="one address per line ('-' for stdin)")
    p.add_argument("--apply", action="store_true", help="actually assign it")
    p.set_defaults(func=cmd_assign_tag)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except SmartleadError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
