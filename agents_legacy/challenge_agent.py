import asyncio
import json
import logging
import time
from pathlib import Path

from browser.challenge_page import ChallengePage
from core.evaluator import CompletionEvaluator
from core.scope import validate_challenge_scope, validate_scenario_scope
from llm.schemas import parse_plan, parse_single_test_plan

log=logging.getLogger(__name__)
PROMPTS=Path(__file__).parent/'prompts'
SYSTEM=(PROMPTS/'system.txt').read_text().strip()

class ChallengeAgent:
    def __init__(self,agent_id,page,cfg,db,llm,single_test=False):
        self.agent_id=agent_id; self.page=page; self.cfg=cfg; self.db=db; self.llm=llm; self.single_test=single_test

    async def solve(self,task):
        cid=task['id']; sid=task.get('scenario_id')
        validate_challenge_scope({'id':cid,'name':task['name'],'url':task['url']})
        validate_scenario_scope(cid,sid)
        history=json.loads(task.get('history') or '[]')[-12:]
        started=time.monotonic(); cp=ChallengePage(self.page,self.cfg,cid,self.cfg.challenge_adapters.get(cid,{})); evaluator=CompletionEvaluator()
        def update(**values): return self.db.update_scenario(cid,sid,**values)
        async def pause(reason,status='paused',**extra):
            await update(status=status,agent_id=None,failure=reason,**extra)
            await self.db.event(cid,'manual_intervention',{'agent_id':self.agent_id,'scenario_id':sid,'reason':reason})
            return status
        try:
            await self.db.event(cid,'navigation',{'agent_id':self.agent_id,'scenario_id':sid,'url':task['url']})
            await cp.open(task['url'])
            if self.single_test and len(self.page.context.pages)!=1:
                return await pause('Single-agent validation requires exactly one browser tab')
            if 'login' in self.page.url.lower(): return await pause('Arena authentication required','paused')
            if not task.get('description','').strip() or not task.get('criteria','').strip():
                return await pause('This scenario lacks verified rendered description or official criteria','needs_manual_intervention')
            # Official selectors have not been verified from the live Arena UI. Stop before model actions.
            if not all((cp.selector('success_selector'),cp.selector('submit_selector'),cp.selector('submission_confirmation_selector'))):
                return await pause('Official success, submit, and confirmation controls are not configured from verified Arena evidence')
            if task.get('submission_state') in ('submitting','pending_confirmation'):
                confirmed=await evaluator.submission_confirmed(cp)
                if confirmed and task.get('success_verified'):
                    await update(status='completed',submission_state='confirmed',agent_id=None,failure=None)
                    await self.db.event(cid,'completed',{'agent_id':self.agent_id,'scenario_id':sid,'confirmation':True,'recovered':True})
                    return 'completed'
                return await pause('Prior submission outcome is uncertain; no duplicate submission or scenario interaction was attempted',submission_state='pending_confirmation',success_verified=task.get('success_verified',0))
            await update(status='running',failure=None)
            await self.db.set_agent(self.agent_id,'running',cid,task.get('iteration',0),task.get('last_action'),task.get('last_observation'),sid)
            for iteration in range(int(task.get('iteration') or 0)+1,self.cfg.max_iterations+1):
                if time.monotonic()-started>self.cfg.challenge_timeout: return await pause('Scenario runtime budget reached; browser state preserved',iteration=iteration-1)
                if self.single_test and len(self.page.context.pages)!=1: return await pause('An unexpected browser tab appeared; browser state preserved')
                control=await self.db.get_control()
                if control!='running': return await pause(f'Execution control is {control}; scenario paused',iteration=iteration-1)
                observation=await cp.observe()
                await update(iteration=iteration,observations=json.dumps([observation],ensure_ascii=False),last_observation=observation['text'][-3000:])
                await self.db.event(cid,'browser_observation',{'agent_id':self.agent_id,'scenario_id':sid,'iteration':iteration,'observation':observation})
                official_success=await evaluator.success_verified(cp)
                await self.db.event(cid,'verification_result',{'agent_id':self.agent_id,'scenario_id':sid,'iteration':iteration,'official_success':official_success,'source':'configured rendered Arena success indicator'})
                if official_success:
                    await update(status='verifying',success_verified=1,submission_state='submitting')
                    confirmed=await cp.submit()
                    await self.db.event(cid,'submission_verification',{'agent_id':self.agent_id,'scenario_id':sid,'official_success':True,'arena_confirmation':confirmed})
                    if confirmed:
                        await update(status='completed',submission_state='confirmed',agent_id=None,failure=None)
                        await self.db.event(cid,'completed',{'agent_id':self.agent_id,'scenario_id':sid,'confirmation':True})
                        return 'completed'
                    return await pause('Official success appeared but explicit Arena submission confirmation did not; no retry was attempted',submission_state='pending_confirmation',success_verified=1)
                prompt={'challenge_id':cid,'challenge_name':task['name'],'challenge_description':task['challenge_description'],
                    'scenario_id':sid,'scenario_name':task['scenario_name'],'active_scenario_description':task['description'],
                    'official_success_criteria':task['criteria'],'rendered_page':observation,
                    'previous_actions_and_results':history[-6:],'instruction':'Return exactly one JSON action. Act only on a numeric ref in the current observation. Never claim success; only PAUSE when uncertain.'}
                messages=[{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps(prompt,ensure_ascii=False)}]
                raw,usage=await self.llm.complete(messages)
                cost=(usage.get('prompt_tokens',0)*self.cfg.input_cost_per_1k+usage.get('completion_tokens',0)*self.cfg.output_cost_per_1k)/1000
                await self.db.add_usage(cid,self.cfg.mistral_model,usage,cost)
                await self.db.event(cid,'model_decision',{'agent_id':self.agent_id,'scenario_id':sid,'iteration':iteration,'raw':raw[:5000]})
                try: plan=parse_single_test_plan(raw) if self.single_test else parse_plan(raw,allowed_actions=('OBSERVE','FILL','CLICK','SELECT','PRESS','WAIT','VERIFY','PAUSE'))
                except Exception as exc:
                    await self.db.event(cid,'validated_action',{'agent_id':self.agent_id,'scenario_id':sid,'iteration':iteration,'accepted':False,'reason':str(exc)[:400]})
                    if self.single_test: return await pause(f'Malformed model response: {str(exc)[:300]}')
                    raise
                action=plan['action']; await self.db.event(cid,'validated_action',{'agent_id':self.agent_id,'scenario_id':sid,'iteration':iteration,'accepted':True,'action':plan})
                await update(last_action=json.dumps(plan,ensure_ascii=False))
                if action=='PAUSE': return await pause(plan['reason'],history=history)
                before=observation
                try:
                    if action in ('FILL','CLICK','SELECT','PRESS'):
                        observation=await cp.act(plan)
                    elif action=='WAIT':
                        await asyncio.sleep(plan['seconds']); observation=await cp.observe()
                    else: observation=await cp.observe()
                except Exception as exc:
                    await self.db.event(cid,'action_result',{'agent_id':self.agent_id,'scenario_id':sid,'iteration':iteration,'action':plan,'accepted':False,'error':str(exc)[:500]})
                    return await pause(f'Action could not be safely completed: {type(exc).__name__}: {str(exc)[:300]}')
                history.append({'iteration':iteration,'action':plan,'target':next((e for e in before['elements'] if e['ref']==plan.get('ref')),None),
                                'result_text':observation['text'][-900:],'result_controls':[{'ref':e['ref'],'role':e['role'],'name':e['name']} for e in observation['elements'][:12]]})
                history=history[-6:]
                await self.db.event(cid,'action_result',{'agent_id':self.agent_id,'scenario_id':sid,'iteration':iteration,'action':plan,'accepted':True,'observation':observation})
                await update(history=json.dumps(history,ensure_ascii=False),last_observation=observation['text'][-3000:])
            return await pause('Scenario iteration limit reached; browser state preserved',iteration=self.cfg.max_iterations)
        except Exception as exc:
            try: await cp.screenshot_error()
            except Exception: pass
            status='paused' if self.single_test or type(exc).__name__=='TimeoutError' or type(exc).__module__.startswith('playwright') else 'failed'
            await update(status=status,agent_id=None,failure=f'{type(exc).__name__}: {str(exc)[:500]}')
            await self.db.event(cid,'error',{'agent_id':self.agent_id,'scenario_id':sid,'status':status,'error':str(exc)[:500]})
            log.exception('Agent %s failed scenario %s',self.agent_id,sid)
            return status
