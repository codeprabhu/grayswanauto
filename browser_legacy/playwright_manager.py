from pathlib import Path
import logging
from playwright.async_api import async_playwright

log=logging.getLogger(__name__)

class PlaywrightManager:
    def __init__(self,cfg):
        self.cfg=cfg; self.pw=None; self.browser=None; self.context=None; self.pages=[]
        self._closed=False; self._context_closed=False; self._page_errors=[]
    async def __aenter__(self):
        try:
            log.info('Starting Playwright engine=%s headed=%s profile=%s',self.cfg.browser,not self.cfg.headless,self.cfg.profile_path)
            self.pw=await async_playwright().start()
            engine=getattr(self.pw,self.cfg.browser)
            Path(self.cfg.profile_path).mkdir(parents=True,exist_ok=True)
            self.context=await engine.launch_persistent_context(user_data_dir=self.cfg.profile_path,headless=self.cfg.headless,viewport=None)
            self.context.on('close',self._on_context_close)
            self.pages=list(self.context.pages)
            for page in self.pages: self._on_page(page)
            self._closed=False
            return self
        except BaseException:
            # __aexit__ is not called when __aenter__ fails. Clean partially-created resources here
            # while preserving the launch exception as the one reported to the caller.
            await self._shutdown(primary_exception=True)
            log.exception('Playwright startup failed')
            raise
    def _on_context_close(self):
        self._context_closed=True
        if not self._closed: log.error('Persistent browser context closed unexpectedly')
    def _on_page(self,page):
        page.on('crash',lambda: self._on_page_crash(page))
        page.on('close',lambda: log.warning('Browser tab closed: %s',page.url) if not self._closed else None)
    def _on_page_crash(self,page):
        message=f'Browser tab crashed: {page.url}'
        self._page_errors.append(message); log.error(message)
    async def new_page(self):
        if self._closed or self.context is None or self._context_closed:
            raise RuntimeError('Browser context is closed; cannot create a tab')
        try:
            page=await self.context.new_page(); self.pages.append(page); self._on_page(page); return page
        except Exception:
            log.exception('Failed to create browser tab')
            raise
    async def open_agent_tabs(self,count):
        while len(self.pages)<count: await self.new_page()
        return self.pages[:count]
    async def save_auth(self):
        if self._closed or self.context is None or self._context_closed: raise RuntimeError('Cannot persist authentication: browser context is closed')
        # Persistent contexts save their own profile state. Do not export cookies or tokens.
        await self.context.cookies()
    async def authenticated(self):
        if self._closed or self.context is None or self._context_closed: return False
        return any('grayswan' in c.get('domain','') for c in await self.context.cookies())
    async def close(self):
        await self._shutdown(primary_exception=False)
    async def _shutdown(self,primary_exception=False,exc_info=None):
        if self._closed: return
        self._closed=True
        errors=[]
        # Attempt every cleanup step. A close failure must never skip playwright.stop().
        for label,resource,closer in (
            ('context',self.context,lambda: self.context.close() if self.context and not self._context_closed else None),
            ('browser',self.browser,lambda: self.browser.close() if self.browser else None),
            ('playwright',self.pw,lambda: self.pw.stop() if self.pw else None),
        ):
            if resource is None: continue
            try: await closer()
            except BaseException as error:
                errors.append(error); log.error('Error closing %s',label,exc_info=(type(error),error,error.__traceback__))
            finally:
                if label=='context': self.context=None
                elif label=='browser': self.browser=None
                else: self.pw=None
        if errors and not primary_exception: raise errors[0]
    async def __aexit__(self,exc_type,exc,tb):
        await self._shutdown(primary_exception=exc is not None,exc_info=(exc_type,exc,tb))
        return False
    async def check_launch(self):
        pages=await self.open_agent_tabs(self.cfg.concurrency)
        for page in pages: await page.goto('about:blank')
        if len(pages)!=self.cfg.concurrency or len(self.context.pages)!=self.cfg.concurrency:
            raise RuntimeError(f'Expected exactly {self.cfg.concurrency} tabs, found tracked={len(pages)} actual={len(self.context.pages)}')
