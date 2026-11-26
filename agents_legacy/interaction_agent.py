"""Internal agent: prepares structured requests and selects the next step for one session."""
import json
import logging

from core.interaction import (AgentState, ConversationContext, ResponseFormatError, build_request_payload,
                              request_message, parse_stage_response)

log=logging.getLogger(__name__)

SYSTEM_MESSAGE='You are an expert technical writer. Respond with ONLY a raw JSON object (no markdown fences, no commentary) containing exactly these keys: stage (string matching the request), content (string), key_points (array of strings), notes (string).'

class InteractionAgent:
    """Owns reasoning and next-step selection: it never establishes completion itself."""
    def __init__(self, plan, context, evaluator):
        self.plan=plan; self.context=context; self.evaluator=evaluator

    @classmethod
    def for_session(cls, plan, evaluator, max_context_messages, resume_history=None, resume_summary=''):
        context=ConversationContext(SYSTEM_MESSAGE,max_context_messages)
        if resume_history:
            for message in resume_history[-max_context_messages:]:
                role=message.get('role')
                if role in ('user','assistant'): context.append(role,message.get('content') or '')
            context.set_summary(resume_summary)
        return cls(plan,context,evaluator)

    def prepare(self, session):
        """Steps 3/9/10: choose the next stage and build the follow-up structured request."""
        stage_name=self.evaluator.retry_stage()
        spec=next(stage for stage in self.plan.stages if stage.name==stage_name)
        decision=session.evaluator_decision or {}
        feedback=list(decision.get('reasons') or [] ) if not decision.get('complete') else []
        payload=build_request_payload(objective=session.objective,conversation_id=session.conversation_id,
            iteration=session.current_iteration+1,stage=spec.name,stage_title=spec.title,instruction=spec.instruction,
            constraints=list(self.plan.constraints),completed_stages=self.evaluator.completed_stages(),
            context_summary=self.context.summary,verification_feedback=feedback)
        action=(f'resend stage "{spec.name}" with evaluator feedback' if feedback
                else f'request stage "{spec.name}" from the external model')
        return {'stage':spec.name,'payload':payload,'action':action}

    def examine(self, raw, expected_stage):
        """Step 7: validate the external response against the structured contract."""
        try:
            return parse_stage_response(raw,expected_stage=expected_stage),None
        except ResponseFormatError as exc:
            return None,str(exc)

    def record_request(self, payload):
        self.context.append('user',json.dumps(payload,ensure_ascii=False))

    def record_response(self, raw):
        self.context.append('assistant',raw)

    def refresh_summary(self):
        """Replace dropped history with a deterministic summary of verified stages."""
        lines=['Objective: '+self.plan.objective]
        for name in self.evaluator.completed_stages():
            section=self.evaluator.sections.get(name) or {}
            points='; '.join(section.get('key_points') or [])[:600]
            lines.append(f'Stage {name} (iteration {section.get("iteration")}, {section.get("words")} words): {points}')
        self.context.set_summary('\n'.join(lines))

    def restore_summary(self):
        if not self.context.summary: self.refresh_summary()