import asyncio
import importlib
import importlib.util
import json
import sys
import types
from types import SimpleNamespace

import pytest


def test_config_reads_dotenv_and_nvidia_model(monkeypatch,tmp_path):
    import core.config as config_module
    monkeypatch.setattr(config_module,'ROOT',tmp_path)
    (tmp_path/'config.yaml').write_text('browser: chromium\nheadless: false\nconcurrency: 8\n')
    (tmp_path/'.env').write_text('MISTRAL_API_KEY=unit-test-secret\nMISTRAL_MODEL=mistralai/mistral-large-4-1t-a52b\nMISTRAL_BASE_URL=https://integrate.api.nvidia.com/v1\n')
    for key in ('MISTRAL_API_KEY','MISTRAL_MODEL','MISTRAL_BASE_URL'): monkeypatch.delenv(key,raising=False)
    cfg=config_module.load_config(validate=True,require_api=True)
    assert cfg.api_key=='unit-test-secret'
    assert cfg.mistral_model=='mistralai/mistral-large-4-1t-a52b'
    assert cfg.mistral_base_url=='https://integrate.api.nvidia.com/v1'
    assert cfg.concurrency==8 and cfg.headless is False


def test_nvidia_client_request_shape_and_response_parse(monkeypatch):
    import llm.mistral as api
    captured={}
    class FakeResponse:
        status=200; ok=True
        headers={}
        async def json(self): return {'choices':[{'message':{'content':'{"ok":true}'}}],'usage':{'prompt_tokens':9,'completion_tokens':4}}
        async def text(self): return ''
        async def __aenter__(self): return self
        async def __aexit__(self,*a): return False
    class FakeSession:
        def post(self,url,json=None):
            captured['url']=url; captured['payload']=json; captured['headers']=dict(self.headers); return FakeResponse()
        async def close(self): pass
        headers={}
    class FakeClient(api.MistralClient):
        async def __aenter__(self):
            self._session=FakeSession()
            self._session.headers={'Authorization':f'Bearer {self.cfg.api_key}','Content-Type':'application/json'}
            return self
    monkeypatch.setattr(api,'MistralClient',FakeClient)
    cfg=SimpleNamespace(api_key='not-logged',mistral_model='mistralai/mistral-large-4-1t-a52b',mistral_base_url='https://integrate.api.nvidia.com/v1',temperature=.1,max_tokens=88,api_timeout=3,retries=0)
    async def run():
        async with FakeClient(cfg) as client:
            return await client.complete([{'role':'user','content':'test'}])
    content,usage=asyncio.run(run())
    assert captured['url']=='https://integrate.api.nvidia.com/v1/chat/completions'
    assert captured['payload']['model']==cfg.mistral_model
    assert captured['payload']['response_format']=={'type':'json_object'}
    assert captured['payload']['reasoning_effort']=='low'
    assert captured['headers']['Authorization']=='Bearer not-logged'
    assert content=='{"ok":true}' and usage['prompt_tokens']==9


def _install_playwright_fake(monkeypatch):
    if importlib.util.find_spec('playwright') is None:
        parent=types.ModuleType('playwright'); api=types.ModuleType('playwright.async_api')
        monkeypatch.setitem(sys.modules,'playwright',parent); monkeypatch.setitem(sys.modules,'playwright.async_api',api)
    api=importlib.import_module('playwright.async_api')
    return api


def test_browser_manager_cleanup_preserves_original_and_stops(monkeypatch,tmp_path):
    api=_install_playwright_fake(monkeypatch)
    class Page:
        url='about:blank'
        def __init__(self): self.handlers={}
        def on(self,event,handler): self.handlers[event]=handler
        async def close(self): pass
    class Context:
        pages=[Page()]
        def on(self,event,handler): self.close_handler=handler
        async def new_page(self): return Page()
        async def close(self): raise RuntimeError('simulated TargetClosedError')
    class Engine:
        async def launch_persistent_context(self,**kwargs): return Context()
    class Playwright:
        chromium=Engine()
        stopped=False
        async def stop(self): self.stopped=True
    pw=Playwright()
    class Factory:
        async def start(self): return pw
    monkeypatch.setattr(api,'async_playwright',lambda:Factory())
    from browser.playwright_manager import PlaywrightManager
    import browser.playwright_manager as manager_module
    monkeypatch.setattr(manager_module,'async_playwright',lambda:Factory())
    cfg=SimpleNamespace(browser='chromium',headless=False,profile_path=str(tmp_path/'profile'),concurrency=8)
    async def run():
        manager=PlaywrightManager(cfg)
        with pytest.raises(ValueError,match='original operation'):
            async with manager:
                raise ValueError('original operation')
        await manager.close()
        assert pw.stopped
    asyncio.run(run())


def test_browser_manager_startup_failure_stops_playwright(monkeypatch,tmp_path):
    api=_install_playwright_fake(monkeypatch)
    class Engine:
        async def launch_persistent_context(self,**kwargs): raise RuntimeError('launch root cause')
    class Playwright:
        chromium=Engine()
        stopped=False
        async def stop(self): self.stopped=True
    pw=Playwright()
    class Factory:
        async def start(self): return pw
    monkeypatch.setattr(api,'async_playwright',lambda:Factory())
    from browser.playwright_manager import PlaywrightManager
    import browser.playwright_manager as manager_module
    monkeypatch.setattr(manager_module,'async_playwright',lambda:Factory())
    manager=PlaywrightManager(SimpleNamespace(browser='chromium',headless=False,profile_path=str(tmp_path/'profile')))
    async def run():
        with pytest.raises(RuntimeError,match='launch root cause'):
            async with manager: pass
        assert pw.stopped
    asyncio.run(run())


def test_browser_manager_normal_close_is_idempotent(monkeypatch,tmp_path):
    api=_install_playwright_fake(monkeypatch)
    class Page:
        url='about:blank'
        def on(self,*args): pass
        async def close(self): pass
    class Context:
        pages=[]
        closes=0
        def on(self,*args): pass
        async def new_page(self): return Page()
        async def close(self): self.closes+=1
    context=Context()
    class Engine:
        async def launch_persistent_context(self,**kwargs): return context
    class PW:
        chromium=Engine()
        stops=0
        async def stop(self): self.stops+=1
    pw=PW()
    class Factory:
        async def start(self): return pw
    from browser.playwright_manager import PlaywrightManager
    import browser.playwright_manager as manager_module
    monkeypatch.setattr(manager_module,'async_playwright',lambda:Factory())
    manager=PlaywrightManager(SimpleNamespace(browser='chromium',headless=False,profile_path=str(tmp_path/'profile')))
    async def run():
        await manager.__aenter__(); await manager.close(); await manager.close()
        assert context.closes==1 and pw.stops==1
    asyncio.run(run())


def test_sqlite_restart_recovers_owned_challenge(tmp_path):
    from core.database import Database
    from core.scope import CHALLENGE_ID, CHALLENGE_NAME, CHALLENGE_URL, SCENARIO_IDS
    async def run():
        db=Database(tmp_path/'recover.sqlite3'); await db.initialize()
        row={'id':CHALLENGE_ID,'name':CHALLENGE_NAME,'url':CHALLENGE_URL,'description':'desc','scenario_details':[{'id':sid,'name':sid,'description':'desc','criteria':'criteria'} for sid in SCENARIO_IDS]}
        await db.upsert_inventory([row])
        claimed=await db.claim_next('agent-3'); assert claimed['id']==CHALLENGE_ID
        sid=claimed['scenario_id']
        await db.update_scenario(CHALLENGE_ID,sid,status='running',iteration=4,history=[{'role':'user','content':'kept'}])
        await db.recover_running()
        resumed=await db.claim_next('agent-3'); assert resumed['scenario_id']==sid
        stored=next(r for r in await db.all_scenarios() if r['scenario_id']==sid)
        assert stored['iteration']==4 and json.loads(stored['history'])==[{'role':'user','content':'kept'}]
    asyncio.run(run())


def test_official_submission_confirmation_and_dashboard_controls(tmp_path):
    from core.database import Database
    from dashboard.app import create_app
    async def run():
        db=Database(tmp_path/'dashboard.sqlite3'); await db.initialize()
        app=create_app(db)
        routes={route.path:route.endpoint for route in app.routes if hasattr(route,'endpoint')}
        assert '/' in routes and '/api/state' in routes and '/api/control/{value}' in routes
        state=await routes['/api/state'](); assert state['control']=='running'
        changed=await routes['/api/control/{value}']('paused'); assert changed['control']=='paused'
        assert (await db.get_control())=='paused'
        assert (await routes['/api/state']())['control']=='paused'
    asyncio.run(run())


def test_action_schema_rejects_extra_browser_capabilities():
    from llm.schemas import parse_plan
    with pytest.raises(ValueError,match='Unexpected fields'):
        parse_plan('{"action":"INTERACT","text":"hello","url":"https://elsewhere"}')
    with pytest.raises(ValueError,match='numeric'):
        parse_plan('{"action":"WAIT","seconds":"forever"}')


def test_challenge_navigation_is_arena_scoped():
    from browser.challenge_page import ChallengePage
    cfg=SimpleNamespace(retries=0)
    async def run():
        with pytest.raises(ValueError,match='outside'):
            await ChallengePage(object(),cfg,'x').open('https://example.com/arena/challenge/x')
    asyncio.run(run())


def test_manual_authentication_flow_uses_browser_profile(monkeypatch):
    from discovery.inventory import _require_arena_auth
    monkeypatch.setattr('builtins.input',lambda prompt:'')
    class Page:
        url='https://app.grayswan.ai/login'
        async def goto(self,url,**kwargs): self.url=url
    class Browser:
        logged=False
        async def authenticated(self): return self.logged
        async def save_auth(self): self.logged=True
    page=Page(); browser=Browser(); cfg=SimpleNamespace(challenge_entry_url='https://app.grayswan.ai/arena/challenge/test',api_timeout=2)
    asyncio.run(_require_arena_auth(browser,page,cfg))
    assert page.url==cfg.challenge_entry_url and browser.logged


def test_login_command_uses_headed_persistent_profile_and_closes(monkeypatch,tmp_path):
    import main as main_module
    from core.config import Config
    captured={}
    class Page:
        url='about:blank'
        main_frame=object()
        def on(self,*args): pass
        def is_closed(self): return False
        async def goto(self,url,**kwargs): self.url=url; captured['url']=url
    class Context:
        pages=[]
        def on(self,*args): pass
    class Browser:
        pages=[Page()]
        context=Context()
        context.pages=pages
        async def __aenter__(self): return self
        async def save_auth(self): captured['saved']=True
    class Manager:
        def __init__(self,cfg): captured['cfg']=cfg
        async def __aenter__(self): self.browser=Browser(); return self.browser
        async def __aexit__(self,*args): captured['closed']=True
    manager_module=types.ModuleType('browser.playwright_manager')
    manager_module.PlaywrightManager=Manager
    monkeypatch.setitem(sys.modules,'browser.playwright_manager',manager_module)
    monkeypatch.setattr(main_module,'ROOT',tmp_path)
    monkeypatch.setattr(main_module,'_arena_login_verified',lambda page: _async_true())
    monkeypatch.setattr('builtins.input',lambda prompt:'')
    asyncio.run(main_module.login(Config(api_timeout=2)))
    assert captured['url']=='https://app.grayswan.ai/arena'
    assert captured['cfg'].browser=='chromium' and captured['cfg'].headless is False
    assert captured['cfg'].profile_path==str(tmp_path/'.browser-profile')
    assert captured['saved'] and captured['closed']


async def _async_true():
    return True


def test_login_accepts_authenticated_arena_dashboard_content():
    import main as main_module
    class Page:
        url='https://app.grayswan.ai/dashboard'
        async def evaluate(self,script):
            assert 'welcome\\s+back' in script and 'arenaNavigation' in script
            return {'signedOut':False,'accountControl':False,'signIn':False,
                    'challengeLinks':0,'welcomeBack':True,'arenaNavigation':True}
    state=asyncio.run(main_module._arena_page_state(Page()))
    assert state['is_arena'] and state['authenticated']
    assert state['welcome_back'] and state['arena_navigation']


def test_discovery_collects_paginated_listing_and_deduplicates():
    from discovery.inventory import _collect_incrementally
    snapshots=[
        {'page_key':'page-1','total':3,'cards':[{'id':'a'},{'id':'b'}]},
        {'page_key':'page-2','total':3,'cards':[{'id':'b'},{'id':'c'}]},
    ]
    index=0
    async def read_snapshot(): return snapshots[index]
    async def advance():
        nonlocal index
        if index+1<len(snapshots): index+=1; return True
        return False
    result=asyncio.run(_collect_incrementally(read_snapshot,advance))
    assert result['complete'] and result['total']==3
    assert {item['id'] for item in result['items']}=={'a','b','c'}
    assert result['page_count']==2 and result['duplicate_count']==1


def test_discovery_collects_incrementally_loaded_cards():
    from discovery.inventory import _collect_incrementally
    cards=[{'id':'a'}]
    async def read_snapshot(): return {'page_key':'listing','total':3,'cards':list(cards)}
    async def advance():
        if len(cards)<3: cards.append({'id':chr(ord('a')+len(cards))}); return True
        return False
    result=asyncio.run(_collect_incrementally(read_snapshot,advance))
    assert result['complete'] and {item['id'] for item in result['items']}=={'a','b','c'}
    assert result['page_count']==1 and result['duplicate_count']==0


def test_discovery_marks_short_listing_incomplete():
    from discovery.inventory import _collect_incrementally
    async def read_snapshot(): return {'page_key':'page-1','total':2,'cards':[{'id':'only-one'}]}
    async def advance(): return False
    result=asyncio.run(_collect_incrementally(read_snapshot,advance))
    assert not result['complete'] and len(result['items'])==1
    assert result['reason']=='Listing stopped at 1 of 2 unique challenges'


def _single_test_row():
    from core.scope import CHALLENGE_ID, CHALLENGE_NAME, CHALLENGE_URL, SCENARIO_IDS
    return {'id':CHALLENGE_ID,'name':CHALLENGE_NAME,'url':CHALLENGE_URL,
            'description':'Rendered November description.','scenario_details':[
                {'id':sid,'name':sid,'description':'Scenario description.','criteria':'CRITERIA\nOfficial criterion.'} for sid in sorted(SCENARIO_IDS)]}


def test_single_test_selection_requires_one_accessible_complete_criteria_record(tmp_path):
    import main
    path=tmp_path/'inventory.json'
    row=_single_test_row()
    path.write_text(json.dumps({'listing_complete':True,'challenges':[row],'inaccessible':[],'malformed':[],
                                'missing_criteria_scenarios':[]}))
    from core.scope import CHALLENGE_ID, SCENARIO_IDS
    sid=sorted(SCENARIO_IDS)[0]
    selected,scenario=main._single_test_candidate(path,sid)
    assert selected['id']==CHALLENGE_ID and scenario['id']==sid
    with pytest.raises(ValueError): main._single_test_candidate(path,'other-id')
    row['scenario_details'][0]['criteria']=''
    path.write_text(json.dumps({'listing_complete':True,'challenges':[row],'inaccessible':[],'malformed':[],
                                'missing_criteria_scenarios':[]}))
    with pytest.raises(ValueError,match='lacks rendered'):
        main._single_test_candidate(path,sid)


def test_single_test_cancellation_does_not_open_browser(monkeypatch,tmp_path):
    import main
    from core.scope import SCENARIO_IDS
    row=_single_test_row()
    (tmp_path/'inventory.json').write_text(json.dumps({'listing_complete':True,'challenges':[row],
        'inaccessible':[],'malformed':[],'missing_criteria_scenarios':[]}))
    monkeypatch.setattr('builtins.input',lambda prompt:'no')
    cfg=SimpleNamespace(database_path=str(tmp_path/'automator.sqlite3'))
    result=asyncio.run(main.single_test(cfg,object(),sorted(SCENARIO_IDS)[0]))
    assert result=='cancelled'


def test_single_test_action_contract_rejects_submission_and_completion():
    from llm.schemas import parse_single_test_plan
    assert parse_single_test_plan('{"action":"FILL","ref":"1","value":"hello"}')['action']=='FILL'
    for action in ('SUBMIT','COMPLETE','INTERACT'):
        with pytest.raises(ValueError,match='Unsupported action'):
            parse_single_test_plan(json.dumps({'action':action}))


def test_challenge_page_submission_requires_official_success_and_confirmation(monkeypatch):
    import browser.challenge_page as module
    from core.scope import CHALLENGE_ID, CHALLENGE_URL, CHALLENGE_PATH
    class Locator:
        def __init__(self,kind,page): self.kind=kind; self.page=page; self.first=self
        async def count(self): return 1
        async def is_visible(self,**kwargs):
            if self.kind=='success': return self.page.success
            if self.kind=='confirm': return self.page.confirmed
            return True
        async def is_enabled(self): return True
        async def click(self,**kwargs): self.page.clicked+=1; self.page.confirmed=self.page.confirm_after_click
    class Page:
        url=CHALLENGE_URL
        success=False; confirmed=False; confirm_after_click=True; clicked=0
        def locator(self,selector):
            if 'success' in selector: return Locator('success',self)
            if 'confirm' in selector: return Locator('confirm',self)
            return Locator('submit',self)
    cfg=SimpleNamespace(retries=0,api_timeout=1,success_selector='success',submit_selector='submit',
                        submission_confirmation_selector='confirm')
    async def run():
        page=Page(); cp=module.ChallengePage(page,cfg,CHALLENGE_ID); cp.assigned_path=CHALLENGE_PATH
        assert await cp.submit() is False and page.clicked==0
        page.success=True; page.confirm_after_click=False
        monkeypatch.setattr(module.asyncio,'sleep',lambda _: _resolved_none())
        assert await cp.submit() is False and page.clicked==1
        page.confirm_after_click=True
        assert await cp.submit() is True and page.clicked==2
        page.url='https://app.grayswan.ai/dashboard'
        with pytest.raises(RuntimeError,match='left its assigned Arena route'):
            await cp.submit()
    asyncio.run(run())


async def _resolved_none():
    return None

