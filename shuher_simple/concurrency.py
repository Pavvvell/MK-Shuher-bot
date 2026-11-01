"""Per-user ordering and a single Sheets worker for one application process.

Deployment must keep one instance and one Uvicorn worker. These locks are
not distributed. Telegram retries unacknowledged requests after a restart.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextvars import copy_context


class KeyedQueues:
    def __init__(self):
        self.entries={}

    @asynccontextmanager
    async def hold(self,key):
        entry=self.entries.setdefault(key,[asyncio.Lock(),0])
        entry[1]+=1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1]-=1
            if not entry[1]:
                del self.entries[key]


class SheetsWorker:
    """Run whole read/check/write operations serially on one OS thread.

    A cancelled HTTP request cannot release a thread lock prematurely:
    the executor finishes its running operation before starting another.
    Context propagation retains the calling request's Sheets read cache.
    """
    def __init__(self,thread_name="shuher-sheets"):
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix=thread_name)

    async def run(self,method,*args):
        context=copy_context()
        return await asyncio.get_running_loop().run_in_executor(
            self.executor,context.run,method,*args)

    def close(self):
        self.executor.shutdown(wait=True,cancel_futures=True)


def user_queue_key(body):
    event=body.get("callback_query") or body.get("message") or {}
    uid=event.get("from",{}).get("id")
    # Dialogue and pending confirmations are keyed by user, including callbacks.
    if type(uid) is int:
        return ("user",uid)
    return ("unsupported",body["update_id"])
