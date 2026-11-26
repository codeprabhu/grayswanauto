"""Communication adapter: bounded, cancellable, deduplicated dispatch to the external model endpoint."""
import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass

from core.interaction import utcnow


class AdapterError(RuntimeError): pass
class EndpointUnavailableError(AdapterError): pass
class RequestCancelledError(AdapterError): pass
class DuplicateRequestError(AdapterError): pass

_TRANSIENT_MARKERS=('timeout','timed out','connection','network','temporarily','unavailable',
                    '408','409','425','429','500','502','503','504')

def _is_transient(exc):
    if isinstance(exc,(TimeoutError,ConnectionError,OSError)): return True
    return any(marker in str(exc).lower() for marker in _TRANSIENT_MARKERS)

@dataclass
class AdapterResponse:
    content: str
    usage: dict
    model: str
    attempts: int
    request_at: str
    response_at: str
    latency_ms: float

class CommunicationAdapter:
    """Sends structured requests to the configured external endpoint and returns parsed responses.

    Adds request timeouts, bounded retries with exponential backoff and jitter, cancellation
    support, and duplicate-request protection on top of the underlying LLM client.
    """
    def __init__(self, client, cfg, sleeper=asyncio.sleep, jitter=random.uniform):
        self.client=client; self.cfg=cfg
        self._inflight={}; self._completed=set()
        self._sleep=sleeper; self._jitter=jitter

    def request_key(self, messages, *, session_id, iteration):
        """Stable fingerprint of one dispatched request for duplicate protection."""
        body=json.dumps(messages,ensure_ascii=False,sort_keys=True)
        digest=hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]
        return f'{session_id}/{iteration}/{digest}'

    async def send(self, messages, *, request_key, timeout=None, cancel_event=None, on_retry=None):
        if request_key in self._inflight:
            raise DuplicateRequestError(f'request {request_key} is already in flight; refusing a concurrent duplicate')
        if request_key in self._completed:
            raise DuplicateRequestError(f'request {request_key} already completed; refusing to dispatch it again')
        self._inflight[request_key]=True
        request_at=utcnow(); started=time.monotonic()
        try:
            content,usage,attempts=await self._send_with_retries(messages,timeout,cancel_event,on_retry,request_key)
        finally:
            self._inflight.pop(request_key,None)
        self._completed.add(request_key)
        response_at=utcnow()
        return AdapterResponse(content=content,usage=usage or {},
                               model=getattr(self.cfg,'mistral_model',''),attempts=attempts,
                               request_at=request_at,response_at=response_at,
                               latency_ms=round((time.monotonic()-started)*1000,1))

    async def _send_with_retries(self, messages, timeout, cancel_event, on_retry, request_key):
        total=int(self.cfg.retries)+1
        for attempt in range(1,total+1):
            if cancel_event is not None and cancel_event.is_set():
                raise RequestCancelledError(f'request {request_key} cancelled before dispatch')
            try:
                content,usage=await self._dispatch(messages,timeout,cancel_event)
                return content,usage,attempt
            except (RequestCancelledError,DuplicateRequestError,asyncio.CancelledError):
                raise
            except Exception as exc:
                if not _is_transient(exc): raise
                if attempt>=total:
                    raise EndpointUnavailableError(
                        f'external endpoint unavailable after {attempt} attempts: {type(exc).__name__}') from exc
                base=max(0.0,float(getattr(self.cfg,'interaction_backoff_base',1.0)))
                wait=min(30.0,base*(2**(attempt-1))+self._jitter(0,0.5))
                if on_retry is not None:
                    result = on_retry(attempt, exc, round(wait, 2))
                    if asyncio.iscoroutine(result):
                        await result
                await self._sleep(wait)
        raise EndpointUnavailableError('external endpoint dispatch could not be completed')

    async def _dispatch(self, messages, timeout, cancel_event):
        """One dispatch: bounded by timeout and interruptible by cancellation."""
        task=asyncio.ensure_future(self.client.complete(messages))
        cancel=asyncio.ensure_future(cancel_event.wait()) if cancel_event is not None else None
        try:
            waiters={task}|({cancel} if cancel is not None else set())
            done,_=await asyncio.wait(waiters,timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
            if cancel is not None and cancel in done and not task.done():
                raise RequestCancelledError('cancelled while the external endpoint was generating a response')
            if not done:
                raise TimeoutError(f'external endpoint did not respond within {timeout}s')
            return task.result()
        finally:
            task.cancel()
            if cancel is not None: cancel.cancel()