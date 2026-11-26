import asyncio
import importlib.util
import json
import sys
import types
from types import SimpleNamespace

import pytest

from core.database import Database
from discovery.challenge_parser import parse_candidate
from llm.schemas import parse_plan
from core.evaluator import CompletionEvaluator
from browser.challenge_page import ChallengePage
from core.scope import CHALLENGE_ID, CHALLENGE_NAME, CHALLENGE_URL, SCENARIO_IDS

def scoped_inventory():
    return {'id':CHALLENGE_ID,'name':CHALLENGE_NAME,'url':CHALLENGE_URL,'description':'Rendered November description',
      'scenario_details':[{'id':sid,'name':sid,'description':'Rendered description','criteria':'Official criteria'} for sid in sorted(SCENARIO_IDS)]}

def test_parse_candidate_stable_id():
    a=parse_candidate('https://example.test/arena/challenge/foo',' Foo ')
    b=parse_candidate('https://example.test/arena/challenge/foo','Foo')
    assert a['id']==b['id'] and a['name']=='Foo' and a['slug']=='foo'

def test_action_schema_rejects_unbounded_actions():
    assert parse_plan('{"action":"WAIT","seconds":100}')['seconds']==15
    with pytest.raises(ValueError): parse_plan('{"action":"NAVIGATE","url":"https://elsewhere"}')

def test_only_official_indicators_authorize_completion():
    class Locator:
        def __init__(self,visible):self.visible=visible;self.first=self
        async def count(self):return 1
        async def is_visible(self,**kwargs):return self.visible
    class Page:
        def __init__(self,success,submitted):self.values={'success':success,'submitted':submitted};self.url=CHALLENGE_URL
        def locator(self,selector):return Locator(self.values['success'] if 'challenge-status' in selector else self.values['submitted'])
    async def run():
        evaluator=CompletionEvaluator()
        model_claim_only=ChallengePage(Page(False,False),type('C',(),{'success_selector':'[data-challenge-status="success"]','submission_confirmation_selector':'[data-testid="submission-confirmation"]'})(),CHALLENGE_ID); model_claim_only.assigned_path='/arena/challenge/hazard-hunt-november-26'
        assert not await evaluator.success_verified(model_claim_only)
        actual=ChallengePage(Page(True,True),type('C',(),{'success_selector':'[data-challenge-status="success"]','submission_confirmation_selector':'[data-testid="submission-confirmation"]'})(),CHALLENGE_ID); actual.assigned_path='/arena/challenge/hazard-hunt-november-26'
        assert await evaluator.success_verified(actual)
        assert await evaluator.submission_confirmed(actual)
    asyncio.run(run())

def test_sqlite_claims_unique_and_persists(tmp_path):
    async def run():
        db=Database(tmp_path/'state.sqlite3'); await db.initialize()
        await db.upsert_inventory([scoped_inventory()])
        claimed=await asyncio.gather(*(db.claim_next(f'a{i}') for i in range(8)))
        got=[c['id'] for c in claimed if c]
        scenario_ids=[c['scenario_id'] for c in claimed if c]
        assert len(got)==8 and set(got)=={CHALLENGE_ID} and len(scenario_ids)==len(set(scenario_ids))==8
        await db.update_scenario(CHALLENGE_ID,scenario_ids[0],status='completed',success_verified=1,submission_state='confirmed')
        rows=await db.all_scenarios()
        assert next(r for r in rows if r['scenario_id']==scenario_ids[0])['submission_state']=='confirmed'
        await db.update_scenario(CHALLENGE_ID,scenario_ids[1],status='paused')
        await db.recover_running(); await db.resume_paused()
        resumed=await db.all_scenarios()
        assert next(r for r in resumed if r['scenario_id']==scenario_ids[1])['status']=='pending'
    asyncio.run(run())

def test_worker_pool_replenishes_completed_agent(monkeypatch):
    if importlib.util.find_spec('playwright') is None:
        playwright=types.ModuleType('playwright'); api=types.ModuleType('playwright.async_api')
        api.async_playwright=lambda:None; sys.modules['playwright']=playwright; sys.modules['playwright.async_api']=api
    import core.scheduler as scheduler
    assignments=[]; finished=set(); snapshots=[]; lock=asyncio.Lock()
    class FakeDB:
        async def all_challenges(self): return [{'id':CHALLENGE_ID,'name':CHALLENGE_NAME,'url':CHALLENGE_URL}]
        async def all_scenarios(self): return [{'challenge_id':CHALLENGE_ID,'scenario_id':sid} for sid in sorted(SCENARIO_IDS)]
        async def recover_running(self): pass
        async def resume_paused(self): pass
        async def set_agent(self,*args,**kwargs): pass
        async def update_scenario(self,*args,**kwargs): pass
        async def get_control(self): return 'running'
        async def set_control(self,*args,**kwargs): pass
        async def claim_next(self,aid):
            async with lock:
                if len(assignments)>=22:return None
                if len(assignments)==9:snapshots.append(set(finished))
                item=sorted(SCENARIO_IDS)[len(assignments)]; assignments.append((aid,item)); return {'id':CHALLENGE_ID,'name':CHALLENGE_NAME,'url':CHALLENGE_URL,'scenario_id':item,'iteration':0}
        async def event(self,*a): pass
    class Manager:
        def __init__(self,cfg):self._context_closed=False
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def open_agent_tabs(self,n):return [FakePage() for _ in range(n)]
        async def save_auth(self):pass
    class FakePage:
        url='about:blank'
        def is_closed(self): return False
        async def goto(self,url,**kwargs):self.url=url
    class LLM:
        def __init__(self,cfg):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
    class Agent:
        def __init__(self,aid,*a):self.aid=aid
        async def solve(self,task):
            if task['scenario_id']==sorted(SCENARIO_IDS)[2]: await asyncio.sleep(.005)
            else: await asyncio.sleep(.08)
            finished.add(task['scenario_id'])
    monkeypatch.setattr(scheduler,'PlaywrightManager',Manager)
    monkeypatch.setattr(scheduler,'MistralClient',LLM)
    monkeypatch.setattr(scheduler,'ChallengeAgent',Agent)
    cfg=SimpleNamespace(expected_challenges=1,concurrency=8,api_concurrency=4,challenge_entry_url='https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26',api_timeout=1)
    asyncio.run(scheduler.Scheduler(cfg,FakeDB()).run())
    ninth=assignments[8]
    assert ninth[0]=='agent-3'
    assert len({cid for _,cid in assignments})==len(assignments)==22
    # Agent 3's next claim happened before agents 1/2 and 4-8 finished their first task.
    assert not set(sorted(SCENARIO_IDS)[0:8]) <= snapshots[0]
