import asyncio
import random
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from core.scope import CHALLENGE_ID, CHALLENGE_NAME, CHALLENGE_URL, validate_challenge_scope, validate_challenge_url

class ChallengePage:
    def __init__(self,page,cfg,cid,adapter=None):
        validate_challenge_scope({'id':cid,'name':CHALLENGE_NAME,'url':CHALLENGE_URL})
        self.page=page; self.cfg=cfg; self.cid=cid; self.adapter=adapter or {}; self.assigned_path=None; self._observed_elements={}
    def selector(self,key): return self.adapter.get(key,getattr(self.cfg,key))
    def _assert_assigned_location(self):
        if self.assigned_path is None: raise RuntimeError('Challenge page has not been opened through the scoped navigation guard')
        parsed=urlparse(self.page.url)
        if parsed.scheme!='https' or parsed.netloc!='app.grayswan.ai' or parsed.path!=self.assigned_path or parsed.path!='/arena/challenge/hazard-hunt-november-26':
            raise RuntimeError(f'Challenge page left its assigned Arena route: {parsed.hostname or "unknown"}{parsed.path or "/"}')
    async def open(self,url):
        validate_challenge_url(self.cid,url)
        parsed=urlparse(url)
        self.assigned_path=parsed.path
        for attempt in range(self.cfg.retries+1):
            try:
                await self.page.goto(url,wait_until='domcontentloaded',timeout=self.cfg.api_timeout*1000)
                self._assert_assigned_location()
                return
            except Exception:
                if attempt>=self.cfg.retries: raise
                await asyncio.sleep(min(8,2**attempt+random.random()))
    async def text(self):
        self._assert_assigned_location()
        return await self.page.locator('body').inner_text(timeout=10000)
    async def observe(self):
        """Return compact rendered text plus visible, semantically named controls."""
        self._assert_assigned_location()
        state=await self.page.evaluate(r"""() => {
          const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.visibility!=='hidden'&&s.display!=='none'&&r.width>0&&r.height>0};
            const clean=s=>(s||'').replace(/\s+/g,' ').trim().slice(0,120);
          const nodes=Array.from(document.querySelectorAll('button,a,input,textarea,select,[role="button"],[role="option"],[role="radio"],[role="checkbox"]')).filter(visible);
          return {text:(document.body?.innerText||'').slice(-6000),elements:nodes.slice(0,50).map((e,i)=>{
            const tag=e.tagName.toLowerCase(),role=e.getAttribute('role')||(tag==='a'?'link':tag==='button'?'button':tag==='select'?'combobox':tag==='textarea'?'textbox':tag==='input'?({checkbox:'checkbox',radio:'radio',submit:'button'}[e.type]||'textbox'):'');
            const label=clean(e.getAttribute('aria-label')||e.getAttribute('title')||e.labels?.[0]?.innerText||e.innerText||e.value||'');
            return {ref:String(i+1),tag,role,name:label,placeholder:clean(e.getAttribute('placeholder')),type:e.getAttribute('type')||'',disabled:!!e.disabled,href:e.tagName==='A'?e.getAttribute('href'):null,options:tag==='select'?Array.from(e.options).map(o=>clean(o.label)).slice(0,30):undefined};
          })};
        }""")
        self._observed_elements={item['ref']:item for item in state['elements']}
        return state
    async def act(self,action):
        """Execute only a uniquely resolved semantic control from the last observation."""
        self._assert_assigned_location()
        ref=str(action.get('ref','')); previously_observed=self._observed_elements.get(ref)
        observed=await self.observe()
        element=next((e for e in observed['elements'] if e['ref']==ref),None)
        if not element or previously_observed is None: raise ValueError('Action target is absent from the current rendered observation')
        identity=('tag','role','name','placeholder','type','href','disabled','options')
        if any(previously_observed.get(key)!=element.get(key) for key in identity): raise ValueError('Action target changed after observation; refusing a stale element action')
        role=element.get('role'); name=element.get('name',''); tag=element.get('tag'); placeholder=element.get('placeholder','')
        label=(name+' '+str(element.get('href') or '')).lower()
        if element.get('disabled'): raise ValueError('Disabled controls cannot be acted on')
        if role in ('button','link') and any(word in label for word in ('submit','complete','finish challenge','claim reward')):
            raise ValueError('Arena completion and submission controls are reserved for the official evaluator')
        if tag=='select' and action['action']=='SELECT':
            if not isinstance(action.get('value'),str) or not action['value']: raise ValueError('Select target requires a rendered option label')
            locator=self.page.get_by_label(name,exact=True) if name else self.page.locator('select')
            if await locator.count()!=1: raise ValueError('Select target is ambiguous')
            await locator.select_option(label=action['value'],timeout=8000)
        elif tag=='select': raise ValueError('A native select control requires SELECT with a rendered option label')
        elif action['action']=='FILL':
            if role not in ('textbox','combobox') or not (name or placeholder): raise ValueError('Fill target needs a rendered accessible name or placeholder')
            locator=self.page.get_by_label(name,exact=True) if name else self.page.get_by_placeholder(placeholder,exact=True)
            if await locator.count()!=1: raise ValueError('Fill target is ambiguous')
            await locator.fill(action['value'],timeout=8000)
        elif action['action']=='CLICK':
            if not name or role not in ('button','link','radio','checkbox','option'):
                raise ValueError('Click target must have a rendered semantic role and accessible name')
            locator=self.page.get_by_role(role,name=name,exact=True)
            if await locator.count()!=1: raise ValueError('Click target is ambiguous')
            await locator.click(timeout=8000)
        elif action['action']=='SELECT':
            if role not in ('radio','checkbox','option','button') or not name: raise ValueError('Selection target is not an accessible option')
            locator=self.page.get_by_role(role,name=name,exact=True)
            if await locator.count()!=1: raise ValueError('Selection target is ambiguous')
            await locator.click(timeout=8000)
        elif action['action']=='PRESS':
            if role not in ('textbox','combobox') or not (name or placeholder): raise ValueError('Press target must be a named text control')
            locator=self.page.get_by_label(name,exact=True) if name else self.page.get_by_placeholder(placeholder,exact=True)
            if await locator.count()!=1: raise ValueError('Press target is ambiguous')
            await locator.press(action['key'],timeout=8000)
        else: raise ValueError('Unsupported browser action')
        try: await self.page.wait_for_load_state('domcontentloaded',timeout=3000)
        except Exception: await self.page.wait_for_timeout(300)
        self._assert_assigned_location()
        return await self.observe()
    async def details(self):
        self._assert_assigned_location()
        body=await self.text()
        async def take(selector):
            try: return await self.page.locator(selector).first.inner_text(timeout=1000)
            except Exception: return ''
        desc=await take(self.selector('description_selector'))
        criteria=await take(self.selector('criteria_selector'))
        return {'description':desc or body[:5000], 'criteria':criteria or '', 'state':body[-8000:]}
    async def success(self):
        self._assert_assigned_location()
        loc=self.page.locator(self.selector('success_selector'))
        try:
            if await loc.count()!=1: return False
            return await loc.first.is_visible(timeout=700)
        except Exception:return False
    async def submitted(self):
        self._assert_assigned_location()
        try:
            status=self.page.locator(self.selector('submission_confirmation_selector'))
            if await status.count()!=1: return False
            return await status.first.is_visible(timeout=500)
        except Exception:return False
    async def response_count(self):
        self._assert_assigned_location()
        return await self.page.locator(self.selector('response_selector')).count()
    async def send(self,text):
        self._assert_assigned_location()
        if not isinstance(text,str) or not text.strip() or len(text)>8000:
            raise ValueError('Challenge interaction must be 1 to 8000 characters of plain text')
        before=await self.response_count()
        inputs=self.page.locator(self.selector('input_selector'))
        sends=self.page.locator(self.selector('send_selector'))
        if await inputs.count()!=1 or await sends.count()!=1:
            raise RuntimeError('Expected exactly one challenge input and one chat-send control')
        field=inputs.first
        send=sends.first
        if not await field.is_visible() or not await send.is_visible() or not await send.is_enabled():
            raise RuntimeError('Challenge input or chat-send control is not visibly available')
        await field.fill(text,timeout=10000)
        await send.click(timeout=10000)
        try: await self.page.wait_for_function('(arg) => document.querySelectorAll(arg.selector).length > arg.count',arg={'selector':self.selector('response_selector'),'count':before},timeout=60000)
        except Exception: await self.page.wait_for_timeout(2000)
        self._assert_assigned_location()
        return (await self.text())[-5000:]
    async def submit(self):
        self._assert_assigned_location()
        if not await self.success(): return False
        if await self.submitted(): return True
        buttons=self.page.locator(self.selector('submit_selector'))
        if await buttons.count()!=1: raise RuntimeError('Expected exactly one Arena submission control')
        button=buttons.first
        if not await button.is_visible() or not await button.is_enabled():
            raise RuntimeError('Arena submission control is not visibly available')
        await button.click(timeout=10000)
        for _ in range(20):
            self._assert_assigned_location()
            if await self.submitted(): return True
            await asyncio.sleep(.5)
        return False
    async def screenshot_error(self):
        await self.screenshot('error')
    async def screenshot(self,label):
        Path('logs/screenshots').mkdir(parents=True,exist_ok=True)
        try: await self.page.screenshot(path=f'logs/screenshots/{self.cid}-{label}-{datetime.now():%Y%m%d%H%M%S}.png',full_page=True)
        except Exception: pass
