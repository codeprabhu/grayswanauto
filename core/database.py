import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
import asyncio
from .scope import CHALLENGE_ID, CHALLENGE_NAME, CHALLENGE_URL, SCENARIO_IDS, ScopeViolation, validate_challenge_scope, validate_scenario_scope, validate_scenario_inventory

def now(): return datetime.now(timezone.utc).isoformat()

class _Cursor:
    def __init__(self,cursor): self.cursor=cursor
    async def fetchall(self): return self.cursor.fetchall()
    async def fetchone(self): return self.cursor.fetchone()

class _Connection:
    """Tiny async-shaped adapter around short, local sqlite3 operations."""
    def __init__(self,path): self.path=path; self.conn=None
    async def __aenter__(self):
        self.conn=sqlite3.connect(self.path,timeout=30); self.conn.row_factory=sqlite3.Row; return self
    async def __aexit__(self,*_): self.conn.close()
    async def execute(self,*args): return _Cursor(self.conn.execute(*args))
    async def executescript(self,*args): self.conn.executescript(*args)
    async def commit(self): self.conn.commit()

def connect(path): return _Connection(path)

def _json_field(value,default):
    if value is None: return default
    if isinstance(value,(dict,list)): return value
    try: return json.loads(value)
    except Exception: return default

def _interaction_session_row(row):
    item=dict(row)
    item['conversation_history']=_json_field(item.get('conversation_history'),[])
    item['latest_request']=_json_field(item.get('latest_request'),None)
    item['latest_response']=_json_field(item.get('latest_response'),None)
    item['evaluator_decision']=_json_field(item.get('evaluator_decision'),None)
    item['cancelled']=bool(item.get('cancelled'))
    return item

def _interaction_iteration_row(row):
    item=dict(row)
    for key in ('request_payload','response_payload','usage','evaluator_decision'):
        item[key]=_json_field(item.get(key),None)
    return item

class Database:
    def __init__(self, path): self.path = str(path); self._log_lock=asyncio.Lock()
    async def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with connect(self.path) as db:
            await db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS challenges(id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, description TEXT DEFAULT '', criteria TEXT DEFAULT '', inventory_order INTEGER, status TEXT DEFAULT 'pending', batch INTEGER, iteration INTEGER DEFAULT 0, objective TEXT DEFAULT '', history TEXT DEFAULT '[]', observations TEXT DEFAULT '[]', submission_state TEXT DEFAULT 'not_submitted', success_verified INTEGER DEFAULT 0, failure TEXT, agent_id TEXT, last_action TEXT, last_observation TEXT, available INTEGER DEFAULT 1, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS scenario_jobs(challenge_id TEXT NOT NULL,scenario_id TEXT NOT NULL,name TEXT NOT NULL,description TEXT DEFAULT '',criteria TEXT DEFAULT '',status TEXT DEFAULT 'pending',agent_id TEXT,iteration INTEGER DEFAULT 0,history TEXT DEFAULT '[]',observations TEXT DEFAULT '[]',submission_state TEXT DEFAULT 'not_submitted',success_verified INTEGER DEFAULT 0,failure TEXT,last_action TEXT,last_observation TEXT,available INTEGER DEFAULT 1,updated_at TEXT,PRIMARY KEY(challenge_id,scenario_id));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, challenge_id TEXT, kind TEXT, payload TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY AUTOINCREMENT, challenge_id TEXT, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, estimated_cost REAL DEFAULT 0, created_at TEXT);
            CREATE TABLE IF NOT EXISTS agents(agent_id TEXT PRIMARY KEY,status TEXT,challenge_id TEXT,scenario_id TEXT,iteration INTEGER DEFAULT 0,last_action TEXT,last_observation TEXT,updated_at TEXT);
            CREATE TABLE IF NOT EXISTS runtime_controls(name TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT);
            CREATE INDEX IF NOT EXISTS challenges_status_order ON challenges(status, inventory_order);
            CREATE TABLE IF NOT EXISTS interaction_sessions(session_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,objective TEXT NOT NULL,conversation_id TEXT NOT NULL,current_iteration INTEGER DEFAULT 0,maximum_iterations INTEGER NOT NULL,conversation_history TEXT DEFAULT '[]',context_summary TEXT DEFAULT '',latest_request TEXT,latest_response TEXT,current_status TEXT DEFAULT 'pending',agent_state TEXT DEFAULT 'idle',start_time TEXT,last_activity_time TEXT,total_duration REAL DEFAULT 0,termination_reason TEXT,error_details TEXT,evaluator_decision TEXT,next_action TEXT,request_count INTEGER DEFAULT 0,prompt_tokens INTEGER DEFAULT 0,completion_tokens INTEGER DEFAULT 0,malformed_count INTEGER DEFAULT 0,cancelled INTEGER DEFAULT 0,available INTEGER DEFAULT 1,created_at TEXT,updated_at TEXT);
            CREATE TABLE IF NOT EXISTS interaction_iterations(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,iteration INTEGER NOT NULL,stage TEXT,request_payload TEXT,request_at TEXT,response_payload TEXT,response_at TEXT,latency_ms REAL,response_status TEXT,attempts INTEGER DEFAULT 0,usage TEXT,context_messages INTEGER DEFAULT 0,estimated_prompt_tokens INTEGER DEFAULT 0,evaluator_decision TEXT,next_action TEXT,error TEXT,created_at TEXT);
            CREATE INDEX IF NOT EXISTS interaction_iterations_session ON interaction_iterations(session_id,iteration);
            ''')
            cols={r[1] for r in await (await db.execute('PRAGMA table_info(challenges)')).fetchall()}
            for name,typ in [('agent_id','TEXT'),('last_action','TEXT'),('last_observation','TEXT'),('available','INTEGER DEFAULT 1')]:
                if name not in cols: await db.execute(f'ALTER TABLE challenges ADD COLUMN {name} {typ}')
            agent_cols={r[1] for r in await (await db.execute('PRAGMA table_info(agents)')).fetchall()}
            if 'scenario_id' not in agent_cols: await db.execute('ALTER TABLE agents ADD COLUMN scenario_id TEXT')
            await db.commit()
        async with connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO runtime_controls(name,value,updated_at) VALUES('execution','running',?)",(now(),)); await db.commit()
    async def upsert_inventory(self, rows, complete=False):
        rows=list(rows)
        if len(rows)!=1: raise ScopeViolation('Inventory persistence accepts exactly the Hazard Hunt November challenge')
        challenge=validate_challenge_scope(rows[0])
        scenarios=validate_scenario_inventory(challenge)
        async with connect(self.path) as db:
            if complete:
                await db.execute("UPDATE challenges SET available=0,status=CASE WHEN status='completed' THEN status ELSE 'unavailable' END,failure=CASE WHEN status='completed' THEN failure ELSE 'Missing from latest complete inventory discovery' END,updated_at=? WHERE available=1",(now(),))
            for i, c in enumerate(rows, 1):
                await db.execute('''INSERT INTO challenges(id,name,url,description,criteria,inventory_order,status,available,updated_at) VALUES(?,?,?,?,?,?, 'pending',1,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,description=excluded.description,criteria=excluded.criteria,inventory_order=excluded.inventory_order,available=1,status=CASE WHEN challenges.status='unavailable' THEN 'pending' ELSE challenges.status END,failure=CASE WHEN challenges.status='unavailable' THEN NULL ELSE challenges.failure END,updated_at=excluded.updated_at''', (c['id'], c['name'], c['url'], c.get('description',''), c.get('criteria',''), i, now()))
            if complete:
                await db.execute("UPDATE scenario_jobs SET available=0,status=CASE WHEN status='completed' THEN status ELSE 'unavailable' END,failure=CASE WHEN status='completed' THEN failure ELSE 'Missing from latest complete November scenario inventory' END,updated_at=? WHERE challenge_id=?",(now(),CHALLENGE_ID))
            for scenario in scenarios:
                validate_scenario_scope(CHALLENGE_ID,scenario['id'])
                await db.execute('''INSERT INTO scenario_jobs(challenge_id,scenario_id,name,description,criteria,status,available,updated_at)
                    VALUES(?,?,?,?,?,'pending',1,?) ON CONFLICT(challenge_id,scenario_id) DO UPDATE SET
                    name=excluded.name,description=excluded.description,criteria=excluded.criteria,available=1,
                    status=CASE WHEN scenario_jobs.status='unavailable' THEN 'pending' ELSE scenario_jobs.status END,
                    failure=CASE WHEN scenario_jobs.status='unavailable' THEN NULL ELSE scenario_jobs.failure END,updated_at=excluded.updated_at''',
                    (CHALLENGE_ID,scenario['id'],scenario['name'],scenario['description'],scenario['criteria'],now()))
            await db.commit()
    async def all_challenges(self):
        async with connect(self.path) as db:
            cur=await db.execute('SELECT * FROM challenges WHERE id=? AND name=? AND url=? AND available=1',(CHALLENGE_ID,CHALLENGE_NAME,CHALLENGE_URL))
            rows=[dict(r) for r in await cur.fetchall()]
            for row in rows: validate_challenge_scope(row)
            return rows
    async def all_scenarios(self):
        placeholders=','.join('?' for _ in SCENARIO_IDS)
        async with connect(self.path) as db:
            cur=await db.execute(f'''SELECT s.*,c.name challenge_name,c.url challenge_url,c.description challenge_description
                FROM scenario_jobs s JOIN challenges c ON c.id=s.challenge_id
                WHERE s.challenge_id=? AND s.scenario_id IN ({placeholders}) AND s.available=1
                AND c.id=? AND c.name=? AND c.url=? AND c.available=1 ORDER BY s.rowid''',
                (CHALLENGE_ID,*sorted(SCENARIO_IDS),CHALLENGE_ID,CHALLENGE_NAME,CHALLENGE_URL))
            rows=[dict(r) for r in await cur.fetchall()]
            for row in rows:
                validate_challenge_scope({'id':row['challenge_id'],'name':row['challenge_name'],'url':row['challenge_url']})
                validate_scenario_scope(row['challenge_id'],row['scenario_id'])
            return rows
    async def claim_next(self, agent_id):
        placeholders=','.join('?' for _ in SCENARIO_IDS)
        async with connect(self.path) as db:
            await db.execute('BEGIN IMMEDIATE')
            cur=await db.execute(f'''SELECT s.*,c.name challenge_name,c.url challenge_url,c.description challenge_description
                FROM scenario_jobs s JOIN challenges c ON c.id=s.challenge_id
                WHERE s.status='pending' AND s.available=1 AND s.challenge_id=? AND s.scenario_id IN ({placeholders})
                AND c.id=? AND c.name=? AND c.url=? AND c.available=1 ORDER BY s.rowid LIMIT 1''',
                (CHALLENGE_ID,*sorted(SCENARIO_IDS),CHALLENGE_ID,CHALLENGE_NAME,CHALLENGE_URL))
            row=await cur.fetchone()
            if row:
                await db.execute("UPDATE scenario_jobs SET status='assigned',agent_id=?,updated_at=? WHERE challenge_id=? AND scenario_id=? AND status='pending' AND available=1", (agent_id,now(),CHALLENGE_ID,row['scenario_id']))
                cur=await db.execute('SELECT changes()'); changed=(await cur.fetchone())[0]
                if not changed: row=None
            await db.commit()
            if not row: return None
            item=dict(row)
            validate_challenge_scope({'id':item['challenge_id'],'name':item['challenge_name'],'url':item['challenge_url']})
            validate_scenario_scope(item['challenge_id'],item['scenario_id'])
            return {'id':item['challenge_id'],'name':item['challenge_name'],'url':item['challenge_url'],
                    'challenge_description':item['challenge_description'],'scenario_id':item['scenario_id'],
                    'scenario_name':item['name'],'description':item['description'],'criteria':item['criteria'],
                    **{key:item[key] for key in ('status','agent_id','iteration','history','observations','submission_state','success_verified','failure','last_action','last_observation')}}
    async def recover_running(self):
        async with connect(self.path) as db:
            await db.execute("UPDATE scenario_jobs SET status='paused',agent_id=NULL,failure='Process stopped during submission; confirmation requires manual inspection',updated_at=? WHERE challenge_id=? AND status='submitting' OR challenge_id=? AND submission_state IN ('submitting','pending_confirmation')",(now(),CHALLENGE_ID,CHALLENGE_ID))
            await db.execute("UPDATE scenario_jobs SET status='pending',agent_id=NULL,updated_at=? WHERE challenge_id=? AND status IN ('assigned','running','verifying') AND submission_state NOT IN ('submitting','pending_confirmation')",(now(),CHALLENGE_ID)); await db.commit()
    async def resume_paused(self):
        async with connect(self.path) as db:
            await db.execute("UPDATE scenario_jobs SET status='pending',agent_id=NULL,updated_at=? WHERE challenge_id=? AND status='paused' AND submission_state NOT IN ('submitting','pending_confirmation') AND scenario_id IN (%s)" % ','.join('?' for _ in SCENARIO_IDS),(now(),CHALLENGE_ID,*sorted(SCENARIO_IDS))); await db.commit()
    async def update(self, cid, **values):
        if cid!=CHALLENGE_ID: raise ScopeViolation('Challenge update rejected outside Hazard Hunt November')
        allowed={'name','url','description','criteria','inventory_order','status','batch','iteration','objective','history','observations','submission_state','success_verified','failure','agent_id','last_action','last_observation','available'}
        if set(values)-allowed: raise ValueError('Challenge update contains unsupported database columns')
        values['updated_at']=now()
        keys=list(values); vals=[json.dumps(v, ensure_ascii=False) if k in ('history','observations') and not isinstance(v,str) else v for k,v in values.items()]
        async with connect(self.path) as db:
            await db.execute('UPDATE challenges SET '+','.join(f'{k}=?' for k in keys)+' WHERE id=?', (*vals,cid)); await db.commit()
    async def update_scenario(self,cid,scenario_id,**values):
        validate_scenario_scope(cid,scenario_id)
        allowed={'name','description','criteria','status','agent_id','iteration','history','observations','submission_state','success_verified','failure','last_action','last_observation','available'}
        if set(values)-allowed: raise ValueError('Scenario update contains unsupported database columns')
        values['updated_at']=now()
        keys=list(values); vals=[json.dumps(v,ensure_ascii=False) if k in ('history','observations') and not isinstance(v,str) else v for k,v in values.items()]
        async with connect(self.path) as db:
            await db.execute('UPDATE scenario_jobs SET '+','.join(f'{k}=?' for k in keys)+' WHERE challenge_id=? AND scenario_id=? AND available=1',(*vals,cid,scenario_id))
            await db.commit()
    async def event(self,cid,kind,payload):
        if cid is not None and cid!=CHALLENGE_ID: raise ScopeViolation('Event rejected for an out-of-scope challenge')
        if isinstance(payload,dict) and payload.get('scenario_id') is not None: validate_scenario_scope(cid,payload['scenario_id'])
        item={'challenge_id':cid,'kind':kind,'payload':payload,'created_at':now()}
        async with connect(self.path) as db:
            await db.execute('INSERT INTO events(challenge_id,kind,payload,created_at) VALUES(?,?,?,?)',(cid,kind,json.dumps(payload,ensure_ascii=False),item['created_at'])); await db.commit()
        async with self._log_lock:
            path=Path(self.path).parent.parent/'logs'/'actions.jsonl'; path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('a',encoding='utf-8') as f: f.write(json.dumps(item,ensure_ascii=False)+'\n')
    async def add_usage(self,cid,model,usage,cost=0):
        if not usage:return
        if cid is not None and cid!=CHALLENGE_ID: raise ScopeViolation('Model usage rejected for an out-of-scope challenge')
        async with connect(self.path) as db:
            await db.execute('INSERT INTO usage(challenge_id,model,prompt_tokens,completion_tokens,estimated_cost,created_at) VALUES(?,?,?,?,?,?)',(cid,model,usage.get('prompt_tokens'),usage.get('completion_tokens'),cost,now())); await db.commit()
    async def create_interaction_session(self,session_id,task_id,objective,conversation_id,maximum_iterations,status='pending'):
        stamp=now()
        async with connect(self.path) as db:
            await db.execute('''INSERT INTO interaction_sessions(session_id,task_id,objective,conversation_id,current_iteration,maximum_iterations,conversation_history,context_summary,latest_request,latest_response,current_status,agent_state,start_time,last_activity_time,total_duration,termination_reason,error_details,evaluator_decision,next_action,request_count,prompt_tokens,completion_tokens,malformed_count,cancelled,available,created_at,updated_at)
                VALUES(?,?,?,?,0,?,'[]','',NULL,NULL,?,'idle',?,?,0,NULL,NULL,NULL,NULL,0,0,0,0,0,1,?,?)''',
                (session_id,task_id,objective,conversation_id,maximum_iterations,str(status),stamp,stamp,stamp,stamp)); await db.commit()
    async def update_interaction_session(self,session_id,**values):
        allowed={'current_iteration','current_status','agent_state','conversation_history','context_summary','latest_request','latest_response','next_action','evaluator_decision','termination_reason','error_details','last_activity_time','total_duration','request_count','prompt_tokens','completion_tokens','malformed_count','cancelled'}
        if set(values)-allowed: raise ValueError('Interaction session update contains unsupported database columns')
        if not values: return
        values['updated_at']=now()
        keys=list(values)
        vals=[json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else (int(v) if isinstance(v,bool) else v) for v in values.values()]
        async with connect(self.path) as db:
            await db.execute('UPDATE interaction_sessions SET '+','.join(f'{k}=?' for k in keys)+' WHERE session_id=?',(*vals,session_id)); await db.commit()
    async def get_interaction_session(self,session_id):
        async with connect(self.path) as db:
            row=await (await db.execute('SELECT * FROM interaction_sessions WHERE session_id=?',(session_id,))).fetchone()
            return _interaction_session_row(row) if row else None
    async def list_interaction_sessions(self,statuses=None,limit=60):
        sql='SELECT * FROM interaction_sessions WHERE available=1'
        params=[]
        if statuses:
            sql+=' AND current_status IN ('+','.join('?' for _ in statuses)+')'
            params+=[str(status) for status in statuses]
        sql+=' ORDER BY updated_at DESC LIMIT ?'; params.append(int(limit))
        async with connect(self.path) as db:
            rows=await (await db.execute(sql,params)).fetchall()
            return [_interaction_session_row(row) for row in rows]
    async def record_interaction_iteration(self,**record):
        async with connect(self.path) as db:
            await db.execute('''INSERT INTO interaction_iterations(session_id,iteration,stage,request_payload,request_at,response_payload,response_at,latency_ms,response_status,attempts,usage,context_messages,estimated_prompt_tokens,evaluator_decision,next_action,error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (record['session_id'],record['iteration'],record['stage'],
                 json.dumps(record.get('request_payload'),ensure_ascii=False) if record.get('request_payload') is not None else None,
                 record.get('request_at'),
                 json.dumps(record.get('response_payload'),ensure_ascii=False) if record.get('response_payload') is not None else None,
                 record.get('response_at'),record.get('latency_ms'),str(record.get('response_status') or ''),
                 record.get('attempts') or 0,
                 json.dumps(record.get('usage') or {},ensure_ascii=False),
                 record.get('context_messages') or 0,record.get('estimated_prompt_tokens') or 0,
                 json.dumps(record.get('decision'),ensure_ascii=False) if record.get('decision') is not None else None,
                 record.get('next_action'),(record.get('error') or '')[:2000],now())); await db.commit()
    async def list_interaction_iterations(self,session_id=None,limit=120):
        sql='SELECT * FROM interaction_iterations'
        params=[]
        if session_id is not None: sql+=' WHERE session_id=?'; params.append(session_id)
        sql+=' ORDER BY id DESC LIMIT ?'; params.append(int(limit))
        async with connect(self.path) as db:
            rows=await (await db.execute(sql,params)).fetchall()
            return [_interaction_iteration_row(row) for row in reversed(rows)]
    async def request_interaction_cancel(self,session_id):
        async with connect(self.path) as db:
            await db.execute('UPDATE interaction_sessions SET cancelled=1,updated_at=? WHERE session_id=? AND available=1',(now(),session_id)); await db.commit()
        await self.event(None,'interaction_cancel',{'session_id':session_id})
    async def recover_interrupted_interactions(self):
        """Mark sessions left running by a stopped process as interrupted; the iteration is never reset."""
        async with connect(self.path) as db:
            rows=await (await db.execute("SELECT session_id,current_iteration FROM interaction_sessions WHERE available=1 AND current_status='running'")).fetchall()
            await db.execute("UPDATE interaction_sessions SET current_status='interrupted',agent_state='idle',termination_reason='process_interrupted',next_action='recover where safe',updated_at=? WHERE available=1 AND current_status='running'",(now(),)); await db.commit()
        for row in rows:
            await self.event(None,'interaction_interrupted',{'session_id':row['session_id'],'current_iteration':row['current_iteration'],'reason':'process stopped while the session was running'})
        return [row['session_id'] for row in rows]
    async def resume_interrupted_interactions(self):
        async with connect(self.path) as db:
            rows=await (await db.execute("SELECT session_id FROM interaction_sessions WHERE available=1 AND current_status='interrupted'")).fetchall()
            await db.execute("UPDATE interaction_sessions SET current_status='pending',termination_reason=NULL,updated_at=? WHERE available=1 AND current_status='interrupted'",(now(),)); await db.commit()
        for row in rows:
            await self.event(None,'interaction_resume_requested',{'session_id':row['session_id']})
        return [row['session_id'] for row in rows]
    async def interaction_summary(self,limit=40):
        sessions=await self.list_interaction_sessions(limit=limit)
        iterations=await self.list_interaction_iterations(limit=120)
        counts={}
        for session in sessions: counts[session['current_status']]=counts.get(session['current_status'],0)+1
        return {'sessions':sessions,'iterations':iterations,'counts':counts,
                'active':sum(1 for s in sessions if s['current_status'] in ('pending','running','paused')),
                'completed':sum(1 for s in sessions if s['current_status']=='completed')}
    async def set_agent(self,agent_id,status,challenge_id=None,iteration=0,last_action=None,last_observation=None,scenario_id=None):
        if challenge_id is not None and challenge_id!=CHALLENGE_ID: raise ScopeViolation('Agent assignment rejected outside Hazard Hunt November')
        if scenario_id is not None: validate_scenario_scope(challenge_id,scenario_id)
        async with connect(self.path) as db:
            await db.execute('''INSERT INTO agents(agent_id,status,challenge_id,scenario_id,iteration,last_action,last_observation,updated_at) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET status=excluded.status,challenge_id=excluded.challenge_id,scenario_id=excluded.scenario_id,iteration=excluded.iteration,last_action=excluded.last_action,last_observation=excluded.last_observation,updated_at=excluded.updated_at''',(agent_id,status,challenge_id,scenario_id,iteration,last_action,last_observation,now())); await db.commit()
    async def set_control(self,name,value):
        if value not in ('running','paused','shutdown'): raise ValueError('invalid runtime control')
        async with connect(self.path) as db:
            await db.execute('INSERT INTO runtime_controls(name,value,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(name,value,now())); await db.commit()
        await self.event(None,'control_changed',{'control':name,'value':value})
    async def get_control(self,name='execution'):
        async with connect(self.path) as db:
            row=await (await db.execute('SELECT value FROM runtime_controls WHERE name=?',(name,))).fetchone()
            return row['value'] if row else 'running'
    async def summary(self):
        async with connect(self.path) as db:
            cur=await db.execute('SELECT status,COUNT(*) n FROM scenario_jobs WHERE challenge_id=? AND available=1 AND scenario_id IN (%s) GROUP BY status' % ','.join('?' for _ in SCENARIO_IDS),(CHALLENGE_ID,*sorted(SCENARIO_IDS))); counts={r['status']:r['n'] for r in await cur.fetchall()}
            cur=await db.execute('SELECT COUNT(*) n FROM scenario_jobs WHERE challenge_id=? AND scenario_id IN (%s) AND available=0' % ','.join('?' for _ in SCENARIO_IDS),(CHALLENGE_ID,*sorted(SCENARIO_IDS))); counts['unavailable']=(await cur.fetchone())['n']
            cur=await db.execute('SELECT * FROM challenges WHERE id=? AND name=? AND url=?',(CHALLENGE_ID,CHALLENGE_NAME,CHALLENGE_URL)); rows=[dict(r) for r in await cur.fetchall()]
            available_total=sum(1 for row in rows if row['available'])
            cur=await db.execute('SELECT COALESCE(SUM(prompt_tokens),0) p,COALESCE(SUM(completion_tokens),0) c,COALESCE(SUM(estimated_cost),0) cost FROM usage WHERE challenge_id=?',(CHALLENGE_ID,)); u=dict(await cur.fetchone())
            cur=await db.execute('SELECT * FROM agents WHERE challenge_id IS NULL OR challenge_id=? ORDER BY agent_id',(CHALLENGE_ID,)); agents=[dict(r) for r in await cur.fetchall()]
            cur=await db.execute('SELECT challenge_id,kind,payload,created_at FROM events WHERE challenge_id IS NULL OR challenge_id=? ORDER BY id DESC LIMIT 250',(CHALLENGE_ID,)); events=[dict(r) for r in await cur.fetchall()]
            for e in events:
                try:e['payload']=json.loads(e['payload'])
                except Exception:pass
            cur=await db.execute("SELECT value FROM runtime_controls WHERE name='execution'"); control=await cur.fetchone()
            jobs=await self.all_scenarios()
            return {'scope':CHALLENGE_NAME,'counts':counts,'challenges':rows,'scenarios':jobs,'available_total':available_total,'scenario_total':len(jobs),'usage':u,'agents':agents,'events':events,'control':control['value'] if control else 'running','interactions':await self.interaction_summary()}
