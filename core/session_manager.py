"""Session manager: creates, persists, records, recovers, and cancels interaction sessions."""
import json
import logging
import time

from core.interaction import InteractionSession, SessionStatus, TerminationReason, new_session_id, utcnow

log=logging.getLogger(__name__)

class SessionManager:
    """Tracks ongoing agent-to-agent conversations and their persisted state."""
    def __init__(self, db): self.db=db

    async def create(self, *, task_id, objective, maximum_iterations, status=SessionStatus.PENDING):
        session_id=new_session_id()
        stamp=utcnow()
        await self.db.create_interaction_session(session_id,task_id,objective,session_id,maximum_iterations,str(status))
        await self.db.event(None,'interaction_started',{'session_id':session_id,'conversation_id':session_id,'task_id':task_id,
            'objective':objective,'maximum_iterations':maximum_iterations})
        return InteractionSession(session_id=session_id,task_id=task_id,objective=objective,conversation_id=session_id,
            maximum_iterations=maximum_iterations,current_status=str(status),start_time=stamp,last_activity_time=stamp,
            started_monotonic=time.monotonic())

    async def load(self, session_id):
        row=await self.db.get_interaction_session(session_id)
        if not row: return None
        return self._to_session(row)

    async def save(self, session):
        await self.db.update_interaction_session(session.session_id,
            current_iteration=session.current_iteration,current_status=str(session.current_status),
            agent_state=str(session.agent_state),conversation_history=session.conversation_history,
            context_summary=session.context_summary,latest_request=session.latest_request,
            latest_response=session.latest_response,next_action=session.next_action,
            evaluator_decision=session.evaluator_decision,termination_reason=session.termination_reason,
            error_details=session.error_details,last_activity_time=session.last_activity_time,
            total_duration=session.total_duration,request_count=session.request_count,
            prompt_tokens=session.prompt_tokens,completion_tokens=session.completion_tokens,
            malformed_count=session.malformed_count,cancelled=session.cancelled)

    async def record_iteration(self, session, *, iteration, stage, request_payload, request_at, response_at,
                               latency_ms, response_status, attempts, usage, decision, next_action,
                               context_messages, estimated_prompt_tokens, response_payload=None, error=None):
        """Persist one iteration and emit a durable progress event."""
        record={'session_id':session.session_id,'iteration':iteration,'stage':stage,'request_payload':request_payload,
                'request_at':request_at,'response_at':response_at,'latency_ms':latency_ms,
                'response_status':str(response_status),'attempts':attempts,'usage':usage,
                'context_messages':context_messages,'estimated_prompt_tokens':estimated_prompt_tokens,
                'decision':decision,'next_action':next_action,
                'response_payload':response_payload if response_payload is not None else {'usage':usage,'latency_ms':latency_ms},
                'error':error}
        await self.db.record_interaction_iteration(**record)
        await self.db.event(None,'interaction_iteration',{
            'session_id':session.session_id,'conversation_id':session.conversation_id,'task_id':session.task_id,
            'objective':session.objective,'iteration':iteration,'maximum_iterations':session.maximum_iterations,
            'stage':stage,'request_at':request_at,'response_at':response_at,'latency_ms':latency_ms,
            'response_status':str(response_status),'attempts':attempts,'agent_state':str(session.agent_state),
            'evaluator_decision':decision,'next_action':next_action,'usage':usage,
            'prompt_tokens':session.prompt_tokens,'completion_tokens':session.completion_tokens,
            'model_claimed_complete':bool((decision or {}).get('model_claimed_complete')),
            'objective_verified':bool((decision or {}).get('complete')),'error':(error or None)})
        return record

    async def cancel_requested(self, session_id):
        row=await self.db.get_interaction_session(session_id)
        return bool(row and row.get('cancelled'))

    async def request_cancel(self, session_id):
        await self.db.request_interaction_cancel(session_id)

    async def recover_interrupted(self):
        return await self.db.recover_interrupted_interactions()

    async def resume_interrupted(self):
        return await self.db.resume_interrupted_interactions()

    async def list(self, statuses=None, limit=60):
        return await self.db.list_interaction_sessions(statuses=statuses,limit=limit)

    async def iterations(self, session_id=None, limit=120):
        return await self.db.list_interaction_iterations(session_id=session_id,limit=limit)

    def _to_session(self, row):
        session=InteractionSession(session_id=row['session_id'],task_id=row['task_id'],objective=row['objective'],
            conversation_id=row['conversation_id'],maximum_iterations=row['maximum_iterations'],
            current_iteration=row['current_iteration'],current_status=row['current_status'],agent_state=row['agent_state'],
            conversation_history=row['conversation_history'],context_summary=row['context_summary'] or '',
            latest_request=row['latest_request'],latest_response=row['latest_response'],
            start_time=row['start_time'] or '',last_activity_time=row['last_activity_time'] or '',
            total_duration=row['total_duration'] or 0.0,termination_reason=row['termination_reason'] or '',
            error_details=row['error_details'] or '',evaluator_decision=row['evaluator_decision'],
            next_action=row['next_action'] or '',request_count=row['request_count'] or 0,
            prompt_tokens=row['prompt_tokens'] or 0,completion_tokens=row['completion_tokens'] or 0,
            malformed_count=row['malformed_count'] or 0,cancelled=bool(row['cancelled']))
        if not session.started_monotonic and session.start_time:
            # Recovered sessions measure duration from their persisted start time.
            from datetime import datetime, timezone
            try:
                parsed=datetime.fromisoformat(session.start_time)
                if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
                session.started_monotonic=time.monotonic()-(datetime.now(timezone.utc)-parsed).total_seconds()
            except ValueError: session.started_monotonic=time.monotonic()
        if not session.started_monotonic: session.started_monotonic=time.monotonic()
        return session