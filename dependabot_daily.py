#!/usr/bin/env python3
"""
dependabot_daily.py
===================
A once-a-day status report for Dependabot across ALL of your GitHub repositories.

It answers the question you're tired of clicking through notifications to answer:
"Is anything ready for me to merge yet?"

For every repo you own it finds open Dependabot pull requests and sorts them into:
  - READY TO MERGE   (GitHub reports the PR as cleanly mergeable)
  - NEEDS ATTENTION  (conflicts, failing checks, behind base, etc.)
and it also summarizes open Dependabot security ALERTS, so you can see issues
that don't yet have a fix PR.

By default the script is READ-ONLY (it just reports). Pass --merge to also
merge the pull requests that are cleanly mergeable.

------------------------------------------------------------------------------
SETUP
------------------------------------------------------------------------------
1. Create a Personal Access Token (PAT):  https://github.com/settings/tokens
     - Classic token: tick the "repo" scope (covers private repos + the
       security-alert data this script reads).
     - Fine-grained token: grant these repository permissions --
         Metadata (read), Pull requests (read; read+write if you want --merge),
         Contents (read; read+write if you want --merge),
         Dependabot alerts (read).

2. Make the token available to the script (recommended: environment variable):

     macOS / Linux:
         export GITHUB_TOKEN='ghp_your_token_here'

     Windows (PowerShell):
         $env:GITHUB_TOKEN = 'ghp_your_token_here'

   (Or paste it into the GITHUB_TOKEN variable below -- less safe; don't commit it.)

3. Install the one dependency and run:
     pip install requests
     python dependabot_daily.py            # report only
     python dependabot_daily.py --merge    # report, then merge the clean ones

------------------------------------------------------------------------------
RUN IT AUTOMATICALLY ONCE A DAY
------------------------------------------------------------------------------
  macOS/Linux (cron -- 9am daily, logs to a file):
      0 9 * * * GITHUB_TOKEN='ghp_...' /usr/bin/python3 /path/dependabot_daily.py >> ~/dependabot.log 2>&1
  Windows: use Task Scheduler to run "python dependabot_daily.py" daily.
"""

import os
import sys
import time
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' library. Install it with:  pip install requests")


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Preferred: set the GITHUB_TOKEN environment variable (see SETUP above).
# Fallback: paste your token between the quotes here (avoid committing it).
GITHUB_TOKEN = ''
if not GITHUB_TOKEN:
    GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '').strip()

# Leave as None to auto-detect your username from the token. Or hardcode it.
GITHUB_USER = None

BASE_URL = 'https://api.github.com'
DEPENDABOT_LOGINS = {'dependabot[bot]', 'dependabot-preview[bot]'}

# Human-readable explanations of GitHub's PR "mergeable_state" values.
STATE_HELP = {
    'clean':     'ready to merge',
    'dirty':     'merge conflicts -- needs a manual rebase/fix',
    'blocked':   'blocked by a required status check or review',
    'behind':    'branch is behind base -- needs an update first',
    'unstable':  'mergeable, but a non-required check is failing',
    'has_hooks': 'mergeable (commit hooks must pass)',
    'draft':     'PR is still a draft',
    'unknown':   'GitHub is still computing mergeability -- re-run shortly',
}

# Order severities worst-first for the alert summary.
SEVERITY_ORDER = {'critical': 0, 'high': 1, 'medium': 2, 'moderate': 2, 'low': 3}

# Glyphs kept as named constants (NOT inline \u escapes). Before Python 3.12 a
# backslash can't appear inside an f-string's {...} part, and macOS system
# Python is older than that -- so we reference these names instead.
DASH = '\u2500'   # horizontal line, for section dividers
CHECK = '\u2713'  # check mark
DOT = '\u2022'    # bullet


# ----------------------------------------------------------------------------
# Terminal colors (auto-disabled when output is piped to a file)
# ----------------------------------------------------------------------------
class C:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'


def disable_colors():
    for name in ('GREEN', 'YELLOW', 'RED', 'BLUE', 'BOLD', 'DIM', 'RESET'):
        setattr(C, name, '')


def divider(label, color=''):
    """A section header: the label padded out to 64 columns with box-drawing dashes."""
    text = (DASH + ' ' + label + ' ').ljust(64, DASH)
    return f"{color}{text}{C.RESET}" if color else text


# ----------------------------------------------------------------------------
# HTTP plumbing (rate-limit handling + retries + pagination)
# ----------------------------------------------------------------------------
session = requests.Session()


def build_headers(token):
    headers = {
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def handle_rate_limit(resp):
    """Return True if we hit a rate limit and slept (caller should retry)."""
    if resp.status_code in (403, 429):
        # Primary rate limit: wait until the reset time.
        if resp.headers.get('X-RateLimit-Remaining') == '0':
            reset = int(resp.headers.get('X-RateLimit-Reset', '0'))
            sleep_for = max(reset - int(time.time()), 0) + 1
            print(f"\n[rate limit] primary limit hit -- sleeping {sleep_for}s...", file=sys.stderr)
            time.sleep(sleep_for)
            return True
        # Secondary rate limit: honor Retry-After.
        retry_after = resp.headers.get('Retry-After')
        if retry_after:
            wait = int(retry_after) + 1
            print(f"\n[rate limit] secondary limit -- sleeping {wait}s...", file=sys.stderr)
            time.sleep(wait)
            return True
    return False


def api(method, url, **kwargs):
    """HTTP request with rate-limit handling and a few transient-error retries."""
    attempts = 0
    while True:
        try:
            resp = session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException:
            attempts += 1
            if attempts >= 3:
                raise
            time.sleep(2 * attempts)
            continue
        if handle_rate_limit(resp):
            continue
        return resp


def get_all_pages(url, params=None):
    """Follow GitHub's Link-header pagination and return a flat list."""
    out = []
    while url:
        resp = api('GET', url, params=params)
        params = None  # subsequent pages arrive fully-formed via the Link header
        if resp.status_code != 200:
            break
        out.extend(resp.json())
        url = resp.links.get('next', {}).get('url')
    return out


# ----------------------------------------------------------------------------
# GitHub API calls
# ----------------------------------------------------------------------------
def detect_username():
    resp = api('GET', f'{BASE_URL}/user')
    return resp.json().get('login') if resp.status_code == 200 else None


def get_repos(affiliation):
    return get_all_pages(
        f'{BASE_URL}/user/repos',
        params={'per_page': 100, 'affiliation': affiliation, 'sort': 'full_name'},
    )


def get_open_prs(full_name):
    return get_all_pages(
        f'{BASE_URL}/repos/{full_name}/pulls',
        params={'state': 'open', 'per_page': 100},
    )


def is_dependabot(pr):
    return (pr.get('user') or {}).get('login', '') in DEPENDABOT_LOGINS


def get_pr_mergeability(full_name, number, polls=3, delay=2.0):
    """
    GitHub computes 'mergeable' asynchronously, so the first read is often null.
    Poll a few times until it settles, then return the PR detail.
    """
    url = f'{BASE_URL}/repos/{full_name}/pulls/{number}'
    data = {}
    for i in range(polls):
        resp = api('GET', url)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        if data.get('mergeable') is not None:
            break
        if i < polls - 1:
            time.sleep(delay)
    return data


def get_open_alerts(full_name):
    """Returns (alerts, error_code_or_None)."""
    url = f'{BASE_URL}/repos/{full_name}/dependabot/alerts'
    params = {'state': 'open', 'per_page': 100}
    alerts = []
    while url:
        resp = api('GET', url, params=params)
        params = None
        if resp.status_code == 403:
            return [], 'forbidden'   # alerts disabled org-wide, or token missing scope
        if resp.status_code == 404:
            return [], 'disabled'    # Dependabot alerts not enabled on this repo
        if resp.status_code != 200:
            return alerts, f'http {resp.status_code}'
        alerts.extend(resp.json())
        url = resp.links.get('next', {}).get('url')
    return alerts, None


def merge_pr(full_name, number):
    resp = api('PUT', f'{BASE_URL}/repos/{full_name}/pulls/{number}/merge')
    if resp.status_code == 200:
        return True, ''
    try:
        msg = resp.json().get('message', resp.text)
    except ValueError:
        msg = resp.text
    return False, f'{resp.status_code}: {msg}'


# ----------------------------------------------------------------------------
# Per-repo analysis
# ----------------------------------------------------------------------------
def alert_severity(alert):
    adv = alert.get('security_advisory') or {}
    return (adv.get('severity') or 'unknown').lower()


def analyze_repo(repo, check_alerts):
    full_name = repo['full_name']
    result = {
        'full_name': full_name,
        'ready': [],
        'attention': [],
        'alerts': [],
        'alerts_error': None,
    }
    for pr in get_open_prs(full_name):
        if not is_dependabot(pr):
            continue
        detail = get_pr_mergeability(full_name, pr['number'])
        state = (detail or {}).get('mergeable_state', 'unknown')
        entry = {
            'number': pr['number'],
            'title': pr['title'],
            'url': pr['html_url'],
            'state': state,
        }
        (result['ready'] if state == 'clean' else result['attention']).append(entry)

    if check_alerts:
        result['alerts'], result['alerts_error'] = get_open_alerts(full_name)
    return result


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------
def print_report(results, username, repos_scanned, check_alerts):
    ready = [(r['full_name'], pr) for r in results for pr in r['ready']]
    attention = [(r['full_name'], pr) for r in results for pr in r['attention']]
    repos_with_alerts = [r for r in results if r['alerts']]
    total_alerts = sum(len(r['alerts']) for r in results)
    forbidden = any(r['alerts_error'] == 'forbidden' for r in results)

    bar = '=' * 64
    print()
    print(bar)
    print(f"{C.BOLD} Dependabot daily report{C.RESET}  --  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f" user: {username}    repositories scanned: {repos_scanned}")
    print(bar)
    print()

    # ---- one-line summary ----
    line = (f"  {C.GREEN}{C.BOLD}{CHECK} {len(ready)} ready to merge{C.RESET}"
            f"     {C.YELLOW}! {len(attention)} need attention{C.RESET}")
    if check_alerts:
        line += f"     {C.BLUE}{DOT} {total_alerts} open alert(s) in {len(repos_with_alerts)} repo(s){C.RESET}"
    print(line)
    print()

    # ---- ready to merge ----
    if ready:
        print(divider('READY TO MERGE', C.GREEN))
        for full_name, pr in sorted(ready):
            print(f"  {C.BOLD}{full_name}{C.RESET}")
            print(f"    #{pr['number']}  {pr['title']}")
            print(f"    {C.DIM}{pr['url']}{C.RESET}")
        script = os.path.basename(sys.argv[0]) or 'dependabot_daily.py'
        print(f"\n  Merge them all with:  {C.BOLD}python {script} --merge{C.RESET}")
        print()

    # ---- needs attention ----
    if attention:
        print(divider('NEEDS ATTENTION', C.YELLOW))
        for full_name, pr in sorted(attention):
            why = STATE_HELP.get(pr['state'], pr['state'])
            print(f"  {C.BOLD}{full_name}{C.RESET}")
            print(f"    #{pr['number']}  {pr['title']}")
            print(f"    {C.YELLOW}{pr['state']}{C.RESET} -- {why}")
            print(f"    {C.DIM}{pr['url']}{C.RESET}")
        print()

    # ---- security alerts ----
    if check_alerts and forbidden and total_alerts == 0:
        print(divider('SECURITY ALERTS', C.BLUE))
        print("  Couldn't read Dependabot alerts -- your token likely lacks the required")
        print("  scope. Classic token: add the 'repo' scope. Fine-grained token: grant")
        print("  the 'Dependabot alerts' repository permission (read).")
        print()
    elif check_alerts and repos_with_alerts:
        print(divider('SECURITY ALERTS', C.BLUE))
        for r in sorted(repos_with_alerts, key=lambda x: -len(x['alerts'])):
            counts = {}
            for a in r['alerts']:
                sev = alert_severity(a)
                counts[sev] = counts.get(sev, 0) + 1
            order = sorted(counts.items(), key=lambda kv: SEVERITY_ORDER.get(kv[0], 9))
            summary = ', '.join(f"{n} {sev}" for sev, n in order)
            print(f"  {C.BOLD}{r['full_name']}{C.RESET}  ({len(r['alerts'])}: {summary})")
        print()

    # ---- the key insight: alerts exist but no fix PRs ----
    if check_alerts and total_alerts > 0 and not ready and not attention:
        print(f"{C.YELLOW}Heads up:{C.RESET} you have open security alerts but NO Dependabot pull")
        print("requests. That usually means \"Dependabot security updates\" isn't turned on,")
        print("so no fix PRs are being opened for you to merge. Enable it per repo under")
        print("  Settings -> Code security -> Dependabot security updates,  or org-wide in")
        print("your GitHub security settings. After that, fix PRs will show up here as")
        print("\"ready to merge.\"")
        print()

    if not ready and not attention and total_alerts == 0 and not forbidden:
        print(f"  {C.GREEN}All clear -- nothing open right now.{C.RESET}\n")


def do_merges(results):
    ready = [(r['full_name'], pr) for r in results for pr in r['ready']]
    if not ready:
        print("Nothing to merge (no cleanly-mergeable PRs).")
        return
    print(divider('MERGING'))
    merged = 0
    for full_name, pr in sorted(ready):
        ok, err = merge_pr(full_name, pr['number'])
        if ok:
            merged += 1
            print(f"  {C.GREEN}merged{C.RESET}  {full_name} #{pr['number']}")
        else:
            print(f"  {C.RED}failed{C.RESET}  {full_name} #{pr['number']} -- {err}")
    print(f"\nMerged {merged} of {len(ready)} pull request(s).")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='Daily Dependabot status report across all your GitHub repos.'
    )
    parser.add_argument('--merge', action='store_true',
                        help='Also merge the pull requests that are cleanly mergeable.')
    parser.add_argument('--no-alerts', action='store_true',
                        help='Skip the security-alert summary (a bit faster).')
    parser.add_argument('--affiliation', default='owner',
                        help="Which repos to scan. Default 'owner'. "
                             "e.g. 'owner,collaborator,organization_member'.")
    parser.add_argument('--workers', type=int, default=0,
                        help='Number of parallel workers (default: auto).')
    parser.add_argument('--no-color', action='store_true',
                        help='Disable colored output.')
    args = parser.parse_args()

    if args.no_color or not sys.stdout.isatty():
        disable_colors()

    if not GITHUB_TOKEN:
        sys.exit(
            "ERROR: No GitHub token found.\n"
            "  Set one with:  export GITHUB_TOKEN='ghp_your_token_here'\n"
            "  (or paste it into the GITHUB_TOKEN variable near the top of this script).\n"
            "  Create a token at https://github.com/settings/tokens (scope: repo)."
        )

    session.headers.update(build_headers(GITHUB_TOKEN))

    username = GITHUB_USER or detect_username()
    if not username:
        sys.exit("ERROR: Could not verify your token (GET /user failed). "
                 "Is it valid and not expired?")

    workers = args.workers if args.workers > 0 else min(16, (os.cpu_count() or 4) * 2)
    check_alerts = not args.no_alerts

    print(f"Fetching your repositories (affiliation: {args.affiliation})...", file=sys.stderr)
    repos = get_repos(args.affiliation)
    if not repos:
        sys.exit("No repositories found for this token.")

    total = len(repos)
    done = 0
    results = []
    print(f"Scanning {total} repositories for Dependabot activity...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(analyze_repo, r, check_alerts): r['full_name'] for r in repos}
        for fut in as_completed(futures):
            done += 1
            print(f"\r  scanned {done}/{total}", end='', file=sys.stderr, flush=True)
            try:
                results.append(fut.result())
            except Exception as e:
                print(f"\n[warn] {futures[fut]}: {e}", file=sys.stderr)
    print('', file=sys.stderr)  # newline after the progress counter

    print_report(results, username, total, check_alerts)

    if args.merge:
        do_merges(results)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
