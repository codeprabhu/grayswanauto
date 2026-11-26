"""Orchestrator: controls the persistent agent-to-agent interaction loop (steps 1-13)."""
import asyncio
import logging
import time

from core.interaction import (AgentState, InteractionSession, SessionStatus, TerminationReason,
                              new_session_id, request_message)
from core.objectives import authorize_objective
from core.evaluator import StagedReportEvaluator
from core.session_manager import SessionManager
from llm.adapter import CommunicationAdapter, AdapterError, EndpointUnavailableError, RequestCancelledError, DuplicateRequestError
from agents.interaction_agent import InteractionAgent

log=logging.getLogger(__name__)

class Orchestrator:
    """Runs one interaction session from start to verified termination."""
    def __init__(self, db, llm_client, cfg, session_manager, semaphore=None,
                 cancel_event=None, sleeper=asyncio.sleep):
        self.db=db; self.llm_client=llm_client; self.cfg=cfg
        self.session_manager=session_manager; self.semaphore=semaphore
        self.cancel_event=cancel_event; self._sleep=sleeper
        self.adapter=CommunicationAdapter(llm_client,cfg)
        self._running=False

    async def start(self, task_id):
        """Steps 1-2: select a permitted task and create the session."""
        plan=authorize_objective(task_id)
        maximum=int(getattr(self.cfg,'max_iterations',plan.stages[-1].min_words//25 or 20))
        session=await self.session_manager.create(task_id=plan.task_id,objective=plan.objective,
            maximum_iterations=maximum,status=SessionStatus.PENDING)
        evaluator=StagedReportEvaluator(plan)
        context_messages=int(getattr(self.cfg,'interaction_context_messages',8))
        agent=InteractionAgent.for_session(plan,evaluator,context_messages)
        session.evaluator=evaluator
        session.agent=agent
        session.plan=plan
        return session

    async def resume(self, session):
        """Rebuild evaluator and agent from a persisted session after a restart."""
        plan=authorize_objective(session.task_id)
        evaluator=StagedReportEvaluator(plan)
        iterations=await self.session_manager.iterations(session_id=session.session_id,limit=200)
        evaluator.restore(iterations)
        context_messages=int(getattr(self.cfg,'interaction_context_messages',8))
        history=session.conversation_history
        summary=session.context_summary
        agent=InteractionAgent.for_session(plan,evaluator,context_messages,resume_history=history,resume_summary=summary)
        session.evaluator=evaluator
        session.agent=agent
        session.plan=plan
        return session

    async def run_session(self, session):
        """Execute the loop until a valid termination condition occurs (steps 3-13)."""
        self._running=True
        try:
            session.current_status=SessionStatus.RUNNING
            session.agent_state=AgentState.PLANNING
            self._sync_history(session)
            await self.session_manager.save(session)
            while self._running:
                # Global cancellation check
                if self.cancel_event is not None and self.cancel_event.is_set():
                    await self._terminate(session,TerminationReason.CANCELLED,'global cancellation requested')
                    break
                # Session-specific cancellation check (persisted cancel flag in the database)
                cancel_requested=await self.session_manager.cancel_requested(session.session_id)
                if cancel_requested:
                    session.cancelled=True
                    await self._terminate(session,TerminationReason.CANCELLED,'session cancellation requested')
                    break
                # Time limit
                if session.elapsed()>=getattr(self.cfg,'interaction_time_limit',1800):
                    await self._terminate(session,TerminationReason.TIME_LIMIT,f'time limit {session.elapsed():.1f}s >= {self.cfg.interaction_time_limit}s')
                    break
                # Max iterations
                if session.current_iteration>=session.maximum_iterations:
                    await self._terminate(session,TerminationReason.MAX_ITERATIONS,f'maximum iterations {session.maximum_iterations} reached')
                    break
                # Steps 3-11: one iteration
                await self._run_iteration(session)
                # A cancellation observed during the iteration must still terminate cleanly.
                if session.cancelled and session.termination_reason!=TerminationReason.CANCELLED:
                    await self._terminate(session,TerminationReason.CANCELLED,'session cancellation requested')
                    break
            return session
        finally:
            self._running=False

    def stop(self): self._running=False

    def _sync_history(self, session):
        """Copy the live agent conversation context into the persisted session state."""
        if getattr(session, 'agent', None) is not None:
            session.conversation_history=list(session.agent.context.messages)
            session.context_summary=session.agent.context.summary

    async def _refresh_cancel_flag(self, session):
        """Read the persisted cancel flag so a local save cannot clear an operator request."""
        if not session.cancelled and await self.session_manager.cancel_requested(session.session_id):
            session.cancelled=True
        return session.cancelled

    async def _cancellable_sleep(self, seconds):
        """Sleep in small slices so an operator cancellation is honoured quickly."""
        deadline=time.monotonic()+max(0.0,float(seconds))
        while time.monotonic()<deadline:
            await asyncio.sleep(min(0.25,max(0.0,deadline-time.monotonic())))

    async def _run_iteration(self, session):
        """One pass through the request/response/evaluate cycle."""
        if await self._refresh_cancel_flag(session):
            await self._terminate(session,TerminationReason.CANCELLED,'session cancellation requested')
            return
        session.agent_state=AgentState.PLANNING
        self._sync_history(session)
        await self.session_manager.save(session)
        # Step 3: internal agent prepares the next structured request
        prep=session.agent.prepare(session)
        session.current_iteration+=1
        session.agent_state=AgentState.WAITING_RESPONSE
        request_payload=prep['payload']
        session.latest_request=request_payload
        session.agent.record_request(request_payload)
        self._sync_history(session)
        await self.session_manager.save(session)
        request_at=time.time()
        # Step 4: dispatch with semaphore, timeout, retries, cancellation
        messages=[request_message(request_payload)]
        try:
            adapter_response=await self._dispatch_with_semaphore(
                messages,
                request_key=self.adapter.request_key(messages,session_id=session.session_id,iteration=session.current_iteration),
                cancel_event=self.cancel_event,
                timeout=getattr(self.cfg,'interaction_request_timeout',120),
                on_retry=lambda attempt,exc,wait: self._log_retry(session,attempt,exc,wait))
        except RequestCancelledError as exc:
            await self._terminate(session,TerminationReason.CANCELLED,str(exc))
            return
        except EndpointUnavailableError as exc:
            await self._terminate(session,TerminationReason.ENDPOINT_UNAVAILABLE,str(exc))
            return
        except DuplicateRequestError as exc:
            log.warning('Duplicate request blocked: %s',exc)
            await self._terminate(session,TerminationReason.AUTHORIZATION_BOUNDARY,str(exc))
            return
        except AdapterError as exc:
            await self._terminate(session,TerminationReason.ENDPOINT_UNAVAILABLE,str(exc))
            return
        response_at=time.time()
        latency_ms=round((response_at-request_at)*1000,1)
        session.request_count+=1
        session.prompt_tokens+=adapter_response.usage.get('prompt_tokens') or 0
        session.completion_tokens+=adapter_response.usage.get('completion_tokens') or 0
        session.latest_response={'content':adapter_response.content,'usage':adapter_response.usage,'latency_ms':latency_ms}
        # Step 5-6: response received and parsed
        session.agent_state=AgentState.EXAMINING
        await self._refresh_cancel_flag(session)
        self._sync_history(session)
        await self.session_manager.save(session)
        session.agent.record_response(adapter_response.content)
        parsed,error=session.agent.examine(adapter_response.content,prep['stage'])
        if error and adapter_response.content:
            error=f'{error}; raw preview: {adapter_response.content[:200]!r}'
        response_status='ok' if error is None else 'malformed'
        if error: session.malformed_count+=1
        # Step 7-8: internal agent examines, evaluator verifies
        session.agent_state=AgentState.EVALUATING
        evaluator=session.evaluator
        decision=evaluator.evaluate(session.current_iteration,prep['stage'],parsed,error)
        session.evaluator_decision=decision
        session.next_action=decision.get('next_action') or ''
        # Record the iteration (persistent event)
        await self.session_manager.record_iteration(session,iteration=session.current_iteration,stage=prep['stage'],
            request_payload=request_payload,request_at=utcnow(),response_at=utcnow(),latency_ms=latency_ms,
            response_status=response_status,attempts=adapter_response.attempts,usage=adapter_response.usage,
            decision=decision,next_action=session.next_action,
            context_messages=len(session.agent.context.materialize()),estimated_prompt_tokens=session.agent.context.token_estimate(),
            response_payload={'content':adapter_response.content,'usage':adapter_response.usage,'latency_ms':latency_ms,'parsed':parsed},
            error=error)
        # Step 8: check termination
        if decision.get('complete'):
            await self._terminate(session,TerminationReason.OBJECTIVE_VERIFIED,'objective independently verified')
            return
        if decision.get('decision')=='reject':
            # Step 9: internal agent determines next step (already baked into retry_stage)
            session.agent_state=AgentState.RETRYING
            if session.malformed_count>=getattr(self.cfg,'interaction_max_malformed',3):
                await self._terminate(session,TerminationReason.AUTHORIZATION_BOUNDARY,
                    f'{session.malformed_count} consecutive malformed responses')
                return
            # Brief backoff before resending the same stage; abort the wait on cancellation.
            await self._cancellable_sleep(getattr(self.cfg,'interaction_retry_backoff',0.5))
            if await self._refresh_cancel_flag(session):
                await self._terminate(session,TerminationReason.CANCELLED,'session cancellation requested')
            return
        # Continue: advance to the next stage
        session.agent.refresh_summary()
        session.agent_state=AgentState.PLANNING
        session.last_activity_time=utcnow()
        await self._refresh_cancel_flag(session)
        self._sync_history(session)
        await self.session_manager.save(session)

    def _dispatch_with_semaphore(self, messages, request_key, cancel_event, timeout, on_retry):
        """Dispatch via the adapter, optionally limited by a concurrency semaphore."""
        async def _send():
            return await self.adapter.send(messages,request_key=request_key,cancel_event=cancel_event,timeout=timeout,on_retry=on_retry)
        if self.semaphore is not None:
            async def _limited():
                async with self.semaphore: return await _send()
            return _limited()
        return _send()

    async def _terminate(self, session, reason, detail=''):
        if reason==TerminationReason.OBJECTIVE_VERIFIED:
            session.current_status=SessionStatus.COMPLETED
        elif reason in (TerminationReason.CANCELLED,TerminationReason.TIME_LIMIT,TerminationReason.MAX_ITERATIONS):
            session.current_status=SessionStatus.CANCELLED if reason==TerminationReason.CANCELLED else SessionStatus.FAILED
        else:
            session.current_status=SessionStatus.FAILED
        session.termination_reason=str(reason)
        session.error_details=str(detail)
        session.agent_state=AgentState.FINISHED
        session.total_duration=session.elapsed()
        session.last_activity_time=utcnow()
        self._sync_history(session)
        await self.session_manager.save(session)
        await self.db.event(None,'interaction_ended',{'session_id':session.session_id,'conversation_id':session.conversation_id,
            'task_id':session.task_id,'objective':session.objective,'current_iteration':session.current_iteration,
            'maximum_iterations':session.maximum_iterations,'termination_reason':str(reason),'error_details':str(detail),
            'total_duration':session.total_duration,'request_count':session.request_count,
            'prompt_tokens':session.prompt_tokens,'completion_tokens':session.completion_tokens,
            'malformed_count':session.malformed_count,'evaluator_decision':session.evaluator_decision,
            'completed_stages':session.evaluator.completed_stages() if session.evaluator else []})
        log.info('Session %s terminated: %s (%s)',session.session_id,reason,detail)
        self._running=False

    def _log_retry(self, session, attempt, exc, wait):
        log.warning('Session %s: external endpoint retry %d in %.2fs (%s)',session.session_id,attempt,wait,type(exc).__name__)
        asyncio.create_task(self.db.event(None,'interaction_retry',{'session_id':session.session_id,'attempt':attempt,'wait':wait,'error':str(exc)[:300]}))

def utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()