"""Async OpenAI-compatible client for NVIDIA NIM using aiohttp."""
import asyncio
import json
import logging
import random
import aiohttp

log = logging.getLogger(__name__)
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class MistralClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._session = None

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=self.cfg.api_timeout, connect=30, sock_read=self.cfg.api_timeout)
        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300, force_close=True)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={'Authorization': f'Bearer {self.cfg.api_key}', 'Content-Type': 'application/json', 'Connection': 'close'}
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session:
            await self._session.close()
            self._session = None
        return False

    async def complete(self, messages):
        if not self.cfg.api_key:
            raise RuntimeError('Set MISTRAL_API_KEY in .env before API requests')
        if not self.cfg.mistral_model:
            raise RuntimeError('Set MISTRAL_MODEL to a model identifier enabled for your NVIDIA account')
        if self._session is None:
            raise RuntimeError('Use MistralClient as an async context manager')

        payload = {
            'model': self.cfg.mistral_model,
            'messages': messages,
            'temperature': self.cfg.temperature,
            'max_tokens': self.cfg.max_tokens,
            'response_format': {'type': 'json_object'},
            'reasoning_effort': 'low'
        }
        endpoint = self.cfg.mistral_base_url.rstrip('/') + '/chat/completions'

        for attempt in range(self.cfg.retries + 1):
            try:
                async with self._session.post(endpoint, json=payload) as response:
                    if response.status in TRANSIENT_STATUS:
                        body = (await response.text())[:1000]
                        if attempt >= self.cfg.retries:
                            raise RuntimeError(f'NVIDIA NIM chat completion failed with HTTP {response.status}: {body}')
                        retry_after = response.headers.get('retry-after')
                        try:
                            provider_wait = float(retry_after)
                        except (TypeError, ValueError):
                            provider_wait = 0
                        wait = min(60, max(provider_wait, 2 ** attempt + random.random()))
                        log.warning('NVIDIA NIM transient HTTP %d; retrying in %.1fs', response.status, wait)
                        await asyncio.sleep(wait)
                        continue
                    if not response.ok:
                        raise RuntimeError(f'NVIDIA NIM chat completion failed with HTTP {response.status}')
                    result = await response.json()
                    choices = result.get('choices') or []
                    if not choices or not isinstance(choices[0].get('message'), dict):
                        raise ValueError('NVIDIA chat completion response had no assistant choice')
                    content = choices[0]['message'].get('content')
                    if isinstance(content, list):
                        content = ''.join(part.get('text', '') for part in content if isinstance(part, dict))
                    if not isinstance(content, str) or not content.strip():
                        # Some responses have content in a different location
                        message = choices[0].get('message', {})
                        if isinstance(message, dict):
                            parts = message.get('content') or []
                            if isinstance(parts, list):
                                content = ''.join(str(p.get('text', '')) for p in parts if isinstance(p, dict))
                        if not isinstance(content, str) or not content.strip():
                            if attempt >= self.cfg.retries:
                                raise ValueError(f'NVIDIA chat completion response had empty assistant content (finish_reason={choices[0].get("finish_reason")})')
                            # Retry on empty content
                            wait = min(10, 1 + random.random())
                            log.warning('NVIDIA NIM empty content; retrying in %.1fs', wait)
                            await asyncio.sleep(wait)
                            continue
                    return content, result.get('usage') or {}
            except aiohttp.ClientError as exc:
                if attempt >= self.cfg.retries:
                    raise RuntimeError(f'NVIDIA NIM request failed: {type(exc).__name__}: {exc}') from exc
                wait = min(30, 2 ** attempt + random.random())
                log.warning('NVIDIA NIM transient request error %s; retrying in %.1fs', type(exc).__name__, wait)
                await asyncio.sleep(wait)
            except asyncio.TimeoutError as exc:
                if attempt >= self.cfg.retries:
                    raise RuntimeError(f'NVIDIA NIM request timed out after {self.cfg.api_timeout}s') from exc
                wait = min(30, 2 ** attempt + random.random())
                log.warning('NVIDIA NIM request timeout; retrying in %.1fs', wait)
                await asyncio.sleep(wait)

    async def check(self):
        result, usage = await self.complete([
            {'role': 'system', 'content': 'Return one JSON object. Do not include markdown.'},
            {'role': 'user', 'content': 'Reply with {"ok": true}.'}
        ])
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError('Configured model failed JSON response-format smoke check') from exc
        if not isinstance(parsed, dict):
            raise ValueError('Configured model returned a non-object structured response')
        return {'provider': 'NVIDIA NIM', 'model': self.cfg.mistral_model, 'structured_response': parsed, 'usage': usage}