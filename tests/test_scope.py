import asyncio
import json
from types import SimpleNamespace

import pytest

from core.scope import (CHALLENGE_ID, CHALLENGE_NAME, CHALLENGE_URL, CHALLENGE_PATH,
                        SCENARIO_IDS, ScopeViolation)

def inventory():
    return {'id':CHALLENGE_ID,'name':CHALLENGE_NAME,'url':CHALLENGE_URL,'description':'November description',
        'scenario_details':[{'id':sid,'name':sid,'description':'Rendered scenario description','criteria':'Rendered official criteria'} for sid in sorted(SCENARIO_IDS)]}

def test_scope_allowlist_and_q3_hazard_hunt_exclusion():
    from core.scope import validate_challenge_scope, validate_scenario_inventory
    challenge=inventory(); validate_challenge_scope(challenge); assert len(validate_scenario_inventory(challenge))==22
    for outside in (
        {'id':'595087260244482c0b876964','name':'Hazard Hunt: Chem, Bio, Cyber, Misc','url':'https://app.grayswan.ai/arena/challenge/hazard-hunt-q3'},
        {'id':CHALLENGE_ID,'name':'Hazard Hunt November','url':CHALLENGE_URL},
        {'id':CHALLENGE_ID,'name':CHALLENGE_NAME,'url':'https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26/other'},
    ):
        with pytest.raises(ScopeViolation): validate_challenge_scope(outside)

def test_database_rejects_out_of_scope_inventory_and_never_claims_it(tmp_path):
    from core.database import Database, connect
    async def run():
        db=Database(tmp_path/'scoped.sqlite3'); await db.initialize(); await db.upsert_inventory([inventory()])
        with pytest.raises(ScopeViolation):
            await db.upsert_inventory([{'id':'q3','name':'Hazard Hunt Q3','url':'https://app.grayswan.ai/arena/challenge/hazard-hunt-q3'}])
        # Simulate stale legacy rows already present in the DB: exact scoped queries still cannot select them.
        async with connect(db.path) as conn:
            await conn.execute("INSERT INTO challenges(id,name,url) VALUES('q3','Hazard Hunt Q3','https://app.grayswan.ai/arena/challenge/hazard-hunt-q3')")
            await conn.execute("INSERT INTO scenario_jobs(challenge_id,scenario_id,name,status) VALUES('q3',?,'legacy','paused')",(sorted(SCENARIO_IDS)[0],))
            await conn.commit()
        await db.resume_paused()
        async with connect(db.path) as conn:
            outsider=await (await conn.execute("SELECT status FROM scenario_jobs WHERE challenge_id='q3'")).fetchone()
            assert outsider['status']=='paused'
        claims=await asyncio.gather(*(db.claim_next(f'w{i}') for i in range(30)))
        assert all(row is None or row['id']==CHALLENGE_ID for row in claims)
        assert not any(row and row['id']=='q3' for row in claims)
        assert len(await db.all_challenges())==1 and len(await db.all_scenarios())==22
    asyncio.run(run())

def test_browser_navigation_and_constructor_reject_out_of_scope():
    from browser.challenge_page import ChallengePage
    cfg=SimpleNamespace(retries=0)
    async def run():
        with pytest.raises(ScopeViolation): ChallengePage(object(),cfg,'q3')
        page=ChallengePage(object(),cfg,CHALLENGE_ID)
        with pytest.raises(ScopeViolation): await page.open('https://app.grayswan.ai/arena/challenge/hazard-hunt-q3')
        with pytest.raises(ScopeViolation): await page.open('https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26?next=/dashboard')
    asyncio.run(run())

def test_semantic_fill_action_refreshes_observation_and_rejects_ambiguous():
    from browser.challenge_page import ChallengePage
    class Locator:
        def __init__(self): self.filled=[]
        async def count(self): return 1
        async def fill(self,value,timeout): self.filled.append(value)
    class Page:
        url=CHALLENGE_URL
        def __init__(self): self.observations=0; self.field=Locator()
        async def evaluate(self,script):
            self.observations+=1
            return {'text':'Arena task','elements':[{'ref':'1','tag':'textarea','role':'textbox','name':'Response','placeholder':'Your answer','type':'','disabled':False,'href':None}]}
        def get_by_label(self,*a,**k): return self.field
        def get_by_placeholder(self,*a,**k): return self.field
        async def wait_for_load_state(self,*a,**k): pass
    cfg=SimpleNamespace(retries=0,api_timeout=1)
    async def run():
        page=Page(); cp=ChallengePage(page,cfg,CHALLENGE_ID); cp.assigned_path=CHALLENGE_PATH
        before=await cp.observe(); result=await cp.act({'action':'FILL','ref':'1','value':'answer'})
        assert before['elements'][0]['name']=='Response' and result['text']=='Arena task'
        assert page.observations==3 and page.field.filled==['answer']
    asyncio.run(run())

def test_action_schema_requires_observed_ref_and_excludes_submit():
    from llm.schemas import parse_single_test_plan
    assert parse_single_test_plan('{"action":"CLICK","ref":"3"}')['action']=='CLICK'
    assert parse_single_test_plan('{"action":"SELECT","ref":"3"}')['action']=='SELECT'
    for response in ('{"action":"CLICK","ref":"#submit"}', '{"action":"FILL","ref":"1"}',
                     '{"action":"SUBMIT"}', '{"action":"CLICK","ref":"1","code":"alert(1)"}'):
        with pytest.raises(ValueError): parse_single_test_plan(response)

def test_browser_navigation_retries_transient_failure():
    from browser.challenge_page import ChallengePage
    class Page:
        url='about:blank'
        def __init__(self): self.calls=0
        async def goto(self,url,**kwargs):
            self.calls+=1
            if self.calls==1: raise TimeoutError('transient')
            self.url=url
    async def run():
        page=Page(); cp=ChallengePage(page,SimpleNamespace(retries=1,api_timeout=1),CHALLENGE_ID)
        await cp.open(CHALLENGE_URL); assert page.calls==2 and page.url==CHALLENGE_URL
    asyncio.run(run())

def test_database_dashboard_exposes_only_scoped_scenarios(tmp_path):
    from core.database import Database
    from dashboard.app import create_app
    async def run():
        db=Database(tmp_path/'dashboard.sqlite3'); await db.initialize(); await db.upsert_inventory([inventory()])
        state=await next(r.endpoint for r in create_app(db).routes if getattr(r,'path',None)=='/api/state')()
        assert state['scope']==CHALLENGE_NAME and len(state['scenarios'])==22
        assert {s['scenario_id'] for s in state['scenarios']}==SCENARIO_IDS
        assert all(s['challenge_id']==CHALLENGE_ID for s in state['scenarios'])
    asyncio.run(run())

def test_recovery_never_requeues_uncertain_submission(tmp_path):
    from core.database import Database
    async def run():
        db=Database(tmp_path/'recovery.sqlite3'); await db.initialize(); await db.upsert_inventory([inventory()])
        sid=sorted(SCENARIO_IDS)[0]
        await db.update_scenario(CHALLENGE_ID,sid,status='submitting',submission_state='submitting',success_verified=1)
        await db.recover_running(); await db.resume_paused()
        row=next(x for x in await db.all_scenarios() if x['scenario_id']==sid)
        assert row['status']=='paused' and row['submission_state']=='submitting'
    asyncio.run(run())

def test_scheduler_refuses_out_of_scope_challenge_before_browser_launch(monkeypatch):
    import core.scheduler as scheduler
    class DB:
        async def all_challenges(self): return [{'id':'q3','name':'Hazard Hunt Q3','url':'https://app.grayswan.ai/arena/challenge/hazard-hunt-q3'}]
    class Browser:
        def __init__(self,*a): raise AssertionError('browser must not launch for out-of-scope inventory')
    monkeypatch.setattr(scheduler,'PlaywrightManager',Browser)
    with pytest.raises(ScopeViolation): asyncio.run(scheduler.Scheduler(SimpleNamespace(),DB()).run())

def test_single_test_has_no_fallback_when_november_row_missing(tmp_path):
    import main
    from core.scope import CHALLENGE_ID, SCENARIO_IDS
    outside={'id':'595087260244482c0b876964','name':'Hazard Hunt: Chem, Bio, Cyber, Misc','url':'https://app.grayswan.ai/arena/challenge/hazard-hunt-q3','description':'other edition'}
    report={'listing_complete':True,'challenges':[outside],'inaccessible':[],'malformed':[],'missing_criteria_scenarios':[]}
    path=tmp_path/'inventory.json'; path.write_text(json.dumps(report))
    with pytest.raises(ScopeViolation): main._single_test_candidate(path,sorted(SCENARIO_IDS)[0])


