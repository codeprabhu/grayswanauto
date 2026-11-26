"""Explicit loop state, structured request/response formats, and context management
for persistent agent-to-agent interaction sessions."""
from dataclasses import dataclass, field
from enum import StrEnum
import json
import time
import uuid
from datetime import datetime, timezone

MAX_CONTENT_CHARS=20000
MAX_KEY_POINTS=12
MAX_POINT_CHARS=300
SUMMARY_MESSAGE_LIMIT=4000
RESPONSE_FIELDS={'stage','content','key_points','notes','objective_complete'}

def utcnow(): return datetime.now(timezone.utc).isoformat()

def new_session_id(): return 'sess-'+uuid.uuid4().hex[:12]

class SessionStatus(StrEnum):
    PENDING='pending'
    RUNNING='running'
    PAUSED='paused'
    COMPLETED='completed'
    FAILED='failed'
    CANCELLED='cancelled'
    INTERRUPTED='interrupted'
    NEEDS_MANUAL_INTERVENTION='needs_manual_intervention'

class TerminationReason(StrEnum):
    OBJECTIVE_VERIFIED='objective_verified'
    MAX_ITERATIONS='max_iterations_reached'
    TIME_LIMIT='time_limit_exceeded'
    ENDPOINT_UNAVAILABLE='endpoint_unavailable'
    CANCELLED='cancelled'
    AUTHORIZATION_BOUNDARY='authorization_boundary'
    MANUAL_INTERVENTION='manual_intervention_required'

class AgentState(StrEnum):
    IDLE='idle'
    PLANNING='planning'
    WAITING_RESPONSE='waiting_response'
    EXAMINING='examining'
    EVALUATING='evaluating'
    RETRYING='retrying'
    PAUSED='paused'
    FINISHED='finished'

class ResponseStatus(StrEnum):
    OK='ok'
    MALFORMED='malformed'
    INVALID_STAGE='invalid_stage'
    ENDPOINT_ERROR='endpoint_error'
    CANCELLED='cancelled'

@dataclass
class InteractionSession:
    """The explicit state of one agent-to-agent interaction session."""
    session_id: str
    task_id: str
    objective: str
    conversation_id: str
    maximum_iterations: int
    current_iteration: int = 0
    current_status: str = SessionStatus.PENDING
    agent_state: str = AgentState.IDLE
    conversation_history: list = field(default_factory=list)
    context_summary: str = ''
    latest_request: dict | None = None
    latest_response: dict | None = None
    start_time: str = ''
    last_activity_time: str = ''
    total_duration: float = 0.0
    termination_reason: str = ''
    error_details: str = ''
    evaluator_decision: dict | None = None
    next_action: str = ''
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    malformed_count: int = 0
    cancelled: bool = False
    started_monotonic: float = 0.0  # runtime only; never persisted

    def elapsed(self):
        """Live or final duration in seconds."""
        if self.total_duration: return self.total_duration
        if not self.started_monotonic: return 0.0
        return round(time.monotonic()-self.started_monotonic,3)

RESPONSE_CONTRACT={
    'stage':'must equal the requested stage',
    'content':'markdown text that answers this stage only',
    'key_points':'array of short bullet strings',
    'notes':'optional caveats or open questions',
}

def build_request_payload(*, objective, conversation_id, iteration, stage, stage_title, instruction,
                          constraints, completed_stages, context_summary, verification_feedback=None):
    """Structured request handed to the external model for one iteration."""
    payload={'objective':objective,'stage':stage,'instruction':instruction,
             'response_contract':dict(RESPONSE_CONTRACT)}
    if completed_stages: payload['completed_stages']=list(completed_stages)
    if context_summary: payload['context_summary']=' '.join(str(context_summary).split())[:SUMMARY_MESSAGE_LIMIT]
    if verification_feedback: payload['verification_feedback']=list(verification_feedback)[:6]
    return payload

def request_message(payload):
    return {'role':'user','content':json.dumps(payload,ensure_ascii=False)}

class ResponseFormatError(ValueError):
    """Raised when the external response violates the structured contract."""

def parse_stage_response(text, *, expected_stage):
    """Strictly validate one structured external response; never trust model claims."""
    raw=text.strip() if isinstance(text,str) else ''
    # Strip markdown code fences like ```json ... ```
    if raw.startswith('```'):
        raw=raw.split('\n',1)[1] if '\n' in raw else raw[3:]
        if raw.endswith('```'): raw=raw[:-3]
        raw=raw.strip()
    try: obj=json.loads(raw)
    except json.JSONDecodeError:
        start=raw.find('{'); end=raw.rfind('}')
        if start<0 or end<=start: raise ResponseFormatError('external response was not a JSON object')
        try: obj=json.loads(raw[start:end+1])
        except json.JSONDecodeError as exc: raise ResponseFormatError(f'external response JSON could not be parsed: {exc}') from exc
    if not isinstance(obj,dict): raise ResponseFormatError('external response was not a JSON object')
    unexpected=set(obj)-RESPONSE_FIELDS
    if unexpected: raise ResponseFormatError('unexpected response fields: '+', '.join(sorted(unexpected)))
    if obj.get('stage')!=expected_stage:
        raise ResponseFormatError(f"response stage {obj.get('stage')!r} does not match requested stage {expected_stage!r}")
    content=obj.get('content')
    if not isinstance(content,str) or not content.strip(): raise ResponseFormatError('content must be a non-empty string')
    if len(content)>MAX_CONTENT_CHARS: raise ResponseFormatError('content exceeds the 20000 character limit')
    points=obj.get('key_points')
    if not isinstance(points,list) or not points or len(points)>MAX_KEY_POINTS or not all(isinstance(p,str) and p.strip() for p in points):
        raise ResponseFormatError('key_points must be a non-empty array of short strings')
    if any(len(p)>MAX_POINT_CHARS for p in points): raise ResponseFormatError('a key point exceeds the 300 character limit')
    notes=obj.get('notes', '')
    if not isinstance(notes, str):
        if notes in (None, '', [], {}): notes = ''
        elif isinstance(notes, list): notes='; '.join(str(n) for n in notes)
        else: notes=str(notes)
    return {'stage':obj['stage'],'content':content.strip(),'key_points':[p.strip() for p in points],
            'notes':str(notes)[:2000],'model_claimed_complete':bool(obj.get('objective_complete'))}

class ConversationContext:
    """Bounded message history with a deterministic summary instead of full replay."""
    def __init__(self, system_message, max_messages=8):
        self.system_message=system_message
        self.max_messages=max(2,int(max_messages))
        self.messages=[]
        self.summary=''
    def append(self, role, content):
        self.messages.append({'role':role,'content':str(content)})
    def set_summary(self, text):
        self.summary=' '.join(str(text).split())[:SUMMARY_MESSAGE_LIMIT]
    def materialize(self):
        """Messages actually sent: system, optional summary, and the recent tail only."""
        out=[{'role':'system','content':self.system_message}]
        if self.summary: out.append({'role':'user','content':'[context summary of earlier iterations]\n'+self.summary})
        out.extend(self.messages[-self.max_messages:])
        return out
    def token_estimate(self):
        """Rough prompt size estimate when the endpoint does not report usage."""
        return sum(max(1,len(m.get('content') or '')//4) for m in self.materialize())