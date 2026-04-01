import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from urllib.request import urlopen

from pyrogram import Client

API_ID = int(os.getenv("VERIFY_API_ID", "0"))
API_HASH = os.getenv("VERIFY_API_HASH", "")
BOT_USERNAME = os.getenv("VERIFY_BOT_USERNAME", "your_bot_username")
SESSIONS_SRC = Path(os.getenv("VERIFY_SESSIONS_SRC", "./sessions"))
SESSIONS_ROOT = Path(os.getenv("VERIFY_SESSIONS_ROOT", "/tmp/ld-tg-verify-sessions"))
VERIFY_SESSION_PREFIX = "verify_user"
WEB_TASKS_URL = os.getenv(
    "VERIFY_WEB_TASKS_URL", "http://127.0.0.1:5000/account/acc_default/tasks/list"
)


def require_verify_env() -> None:
    if API_ID <= 0 or not API_HASH:
        raise SystemExit("set VERIFY_API_ID and VERIFY_API_HASH before running this script")


def copy_session() -> tuple[str, Path]:
    session_name = f"{VERIFY_SESSION_PREFIX}_{uuid.uuid4().hex}"
    session_dir = SESSIONS_ROOT / session_name
    session_dir.mkdir(parents=True, exist_ok=True)
    src = SESSIONS_SRC / "acc_default.session"
    dst = session_dir / f"{session_name}.session"
    if not src.exists():
        raise SystemExit(f"missing source session: {src}")
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return session_name, session_dir


def fetch_tasks():
    with urlopen(WEB_TASKS_URL, timeout=10) as resp:
        return json.loads(resp.read().decode())


async def run_direct(session_name: str, session_dir: Path):
    p = Path("/tmp/ld-tg-verify-direct.bin")
    p.write_bytes(b"PK\x03\x04ld-tg-verify")
    async with Client(session_name, api_id=API_ID, api_hash=API_HASH, workdir=str(session_dir)) as app:
        sent = await app.send_document(BOT_USERNAME, str(p), caption="verify direct file")
        await asyncio.sleep(6)
        history = []
        async for msg in app.get_chat_history(BOT_USERNAME, limit=8):
            txt = (msg.text or msg.caption or "").replace("\n", " ")
            history.append({"id": msg.id, "text": txt[:300]})
        return {"sent_id": sent.id, "history": history, "tasks": fetch_tasks()}


async def run_link(session_name: str, session_dir: Path, link: str):
    async with Client(session_name, api_id=API_ID, api_hash=API_HASH, workdir=str(session_dir)) as app:
        sent = await app.send_message(BOT_USERNAME, f"/download {link}")
        await asyncio.sleep(8)
        history = []
        async for msg in app.get_chat_history(BOT_USERNAME, limit=8):
            txt = (msg.text or msg.caption or "").replace("\n", " ")
            history.append({"id": msg.id, "text": txt[:300]})
        return {"sent_id": sent.id, "history": history, "tasks": fetch_tasks()}


async def main():
    require_verify_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["direct", "link"])
    parser.add_argument("--link", default="https://t.me/example/1")
    args = parser.parse_args()
    session_name, session_dir = copy_session()
    try:
        if args.mode == "direct":
            result = await run_direct(session_name, session_dir)
        else:
            result = await run_link(session_name, session_dir, args.link)
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
