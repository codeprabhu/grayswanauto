import asyncio
import logging
from agents.challenge_agent import ChallengeAgent
from browser.playwright_manager import PlaywrightManager
from llm.mistral import MistralClient
from core.scope import CHALLENGE_ID, SCENARIO_IDS, ScopeViolation, validate_challenge_scope, validate_scenario_scope

log=logging.getLogger(__name__)

class Scheduler:
    def __init__(self,cfg,db): self.cfg=cfg; self.db=db; self.stop_event=asyncio.Event()
    async def run(self):
        challenges=await self.db.all_challenges()
        if len(challenges)!=1:
            raise RuntimeError('Scoped inventory must contain exactly Hazard Hunt November; refusing worker startup')
        validate_challenge_scope(challenges[0])
        scenarios=await self.db.all_scenarios()
        if len(scenarios)!=len(SCENARIO_IDS):
            raise RuntimeError(f'November inventory has {len(scenarios)} verified scenarios; expected {len(SCENARIO_IDS)}. Refusing worker startup.')
        for scenario in scenarios: validate_scenario_scope(scenario['challenge_id'],scenario['scenario_id'])
        await self.db.recover_running()
        await self.db.resume_paused()
        async with PlaywrightManager(self.cfg) as browser, MistralClient(self.cfg) as llm:
            pages=await browser.open_agent_tabs(self.cfg.concurrency)
            print(f'Opened {len(pages)} headed agent tabs. If login is needed, authenticate in the challenge tab and press Enter here.')
            auth_page=pages[0]
            await auth_page.goto(self.cfg.challenge_entry_url,wait_until='domcontentloaded')
            if 'login' in auth_page.url.lower():
                input('Complete manual authentication in the browser, then press Enter to continue: ')
                await browser.save_auth()
            sem=asyncio.Semaphore(self.cfg.api_concurrency)
            class Limited:
                async def complete(_,messages):
                    async with sem:return await llm.complete(messages)
            limited=Limited()
            auth_required=asyncio.Event()
            async def worker(index,page):
                aid=f'agent-{index+1}'; agent=ChallengeAgent(aid,page,self.cfg,self.db,limited)
                await self.db.set_agent(aid,'idle')
                while not self.stop_event.is_set() and not auth_required.is_set():
                    if browser._context_closed:
                        await self.db.set_agent(aid,'browser_context_closed')
                        return
                    if page.is_closed():
                        try:
                            page=await browser.new_page(); agent.page=page
                            await self.db.event(None,'tab_recovered',{'agent_id':aid,'tab':index+1})
                        except Exception as exc:
                            log.exception('Could not replace closed tab for %s',aid)
                            await self.db.set_agent(aid,'browser_error',last_observation=str(exc)[:300])
                            return
                    control=await self.db.get_control()
                    if control=='shutdown':
                        await self.db.set_agent(aid,'stopped')
                        return
                    if control=='paused':
                        await self.db.set_agent(aid,'paused')
                        await asyncio.sleep(.5)
                        continue
                    task=await self.db.claim_next(aid)
                    if not task: return
                    validate_challenge_scope({'id':task['id'],'name':task['name'],'url':task['url']})
                    validate_scenario_scope(task['id'],task['scenario_id'])
                    await self.db.update_scenario(task['id'],task['scenario_id'],status='running')
                    await self.db.set_agent(aid,'running',task['id'],task.get('iteration',0),task.get('last_action'),task.get('last_observation'),task['scenario_id'])
                    await self.db.event(task['id'],'assigned',{'agent_id':aid,'tab':index+1,'scenario_id':task['scenario_id']})
                    result=await agent.solve(task)
                    if result=='auth_required':
                        auth_required.set(); await self.db.set_control('execution','paused'); await self.db.set_agent(aid,'authentication_required',task['id'],scenario_id=task['scenario_id'])
                    else: await self.db.set_agent(aid,'idle')
                    # Immediately claim next; other workers continue independently.
            try:
                while not self.stop_event.is_set():
                    auth_required.clear()
                    async with asyncio.TaskGroup() as group:
                        for i,p in enumerate(pages): group.create_task(worker(i,p),name=f'agent-{i+1}')
                    if browser._context_closed:
                        log.error('Browser context closed unexpectedly; unfinished work is persisted for --resume')
                        break
                    await browser.save_auth()
                    if not auth_required.is_set(): break
                    input('Authentication expired. Complete login or verification in the browser, then press Enter to resume: ')
                    auth_page=pages[0]
                    await auth_page.goto(self.cfg.challenge_entry_url,wait_until='domcontentloaded',timeout=self.cfg.api_timeout*1000)
                    if 'login' in auth_page.url.lower() or not await browser.authenticated():
                        raise RuntimeError('Arena authentication is still unavailable; execution remains paused and can be resumed later')
                    await browser.save_auth()
                    await self.db.resume_paused()
                    await self.db.set_control('execution','running')
            except* KeyboardInterrupt:
                self.stop_event.set()
            finally:
                if not browser._context_closed: await browser.save_auth()
