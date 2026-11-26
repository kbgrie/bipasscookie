import asyncio
import io
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# Import nguyên bản từ source gốc, không chỉnh sửa cookie_refresher.py
from cookie_refresher import mask, normalize_proxy, parse_account, refresh_one

app = FastAPI(title="Cookie Refresher Web")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

JOBS: dict[str, dict] = {}
MAX_JOBS = 30


class RefreshRequest(BaseModel):
    accounts_text: str = Field(..., min_length=1)
    proxies_text: str = ""
    threads: int = Field(default=10, ge=1, le=100)
    keep_failed: bool = False
    webhook_url: str = ""


def _clean_lines(raw: str) -> list[str]:
    return [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _prune_jobs() -> None:
    while len(JOBS) > MAX_JOBS:
        oldest = next(iter(JOBS))
        JOBS.pop(oldest, None)


def _send_webhook_file_bg(job_id: str, webhook_url: str, lines: list[str], ok: int, fail: int, total: int) -> None:
    """Chạy ngầm 100% trên daemon thread: gửi file .txt qua multipart/form-data."""
    url = webhook_url.strip()
    if not url:
        return

    content = ("\n".join(lines) + "\n") if lines else ""
    filename = f"output_refreshed_{job_id}.txt"
    summary = f"Refreshed done ({job_id}): {ok}/{total} OK, {fail} FAIL"

    def _run():
        try:
            buf = io.BytesIO(content.encode("utf-8"))
            files = {"file": (filename, buf, "text/plain; charset=utf-8")}
            data = {
                "content": summary,
                "job_id": job_id,
                "ok": str(ok),
                "fail": str(fail),
                "total": str(total),
            }
            requests.post(url, data=data, files=files, timeout=30)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name=f"wh-{job_id}").start()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/refresh/stream")
async def refresh_stream(payload: RefreshRequest):
    raw_accounts = _clean_lines(payload.accounts_text)
    accounts = [a for a in (parse_account(ln) for ln in raw_accounts) if a]
    if not accounts:
        raise HTTPException(status_code=400, detail="Không tìm thấy account hợp lệ (user:pass:cookie)")

    proxies = [normalize_proxy(p) for p in _clean_lines(payload.proxies_text)]
    proxies = [p for p in proxies if p]
    webhook_url = payload.webhook_url.strip()

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"ok_lines": [], "all_lines": [], "done": False}
    _prune_jobs()

    async def event_generator():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def emit(event: str, data: dict):
            msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            loop.call_soon_threadsafe(queue.put_nowait, msg)

        def worker():
            ok = 0
            fail = 0
            total = len(accounts)
            emit("start", {
                "job_id": job_id,
                "total": total,
                "proxies": len(proxies),
                "threads": payload.threads,
            })

            with ThreadPoolExecutor(max_workers=payload.threads) as pool:
                futures = [
                    pool.submit(refresh_one, u, p, c, proxies)
                    for (u, p, c) in accounts
                ]
                for fut in as_completed(futures):
                    user, password, cookie, err = fut.result()
                    if err is None:
                        ok += 1
                        line = f"{user}:{password}:{cookie}"
                        JOBS[job_id]["ok_lines"].append(line)
                        JOBS[job_id]["all_lines"].append(line)
                        emit("progress", {
                            "status": "OK",
                            "user": user,
                            "detail": mask(cookie),
                            "line": line,
                            "ok": ok,
                            "fail": fail,
                            "total": total,
                        })
                    else:
                        fail += 1
                        line = f"{user}:{password}:{cookie}"
                        if payload.keep_failed:
                            JOBS[job_id]["all_lines"].append(line)
                        emit("progress", {
                            "status": "FAIL",
                            "user": user,
                            "detail": err,
                            "line": line if payload.keep_failed else "",
                            "ok": ok,
                            "fail": fail,
                            "total": total,
                        })

            final_lines = list(JOBS[job_id]["all_lines"])
            JOBS[job_id]["done"] = True

            if webhook_url:
                _send_webhook_file_bg(job_id, webhook_url, final_lines, ok, fail, total)

            emit("done", {
                "job_id": job_id,
                "ok": ok,
                "fail": fail,
                "total": total,
            })
            loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, worker)

        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/download/{job_id}", response_class=PlainTextResponse)
async def download_result(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job không tồn tại hoặc đã hết hạn")
    content = "\n".join(job["all_lines"])
    if content:
        content += "\n"
    return PlainTextResponse(
        content=content,
        headers={"Content-Disposition": f'attachment; filename="output_refreshed_{job_id}.txt"'},
    )
