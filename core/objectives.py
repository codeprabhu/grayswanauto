"""Permitted benign objectives and the authorization boundary for live agent-to-agent tasks."""
from dataclasses import dataclass

class AuthorizationBoundary(ValueError):
    """Raised when a requested task crosses the permitted, benign objective boundary."""

@dataclass(frozen=True)
class StageSpec:
    name: str
    title: str
    instruction: str
    min_words: int

@dataclass(frozen=True)
class ObjectivePlan:
    task_id: str
    title: str
    objective: str
    stages: tuple
    constraints: tuple

    @property
    def stage_names(self): return tuple(stage.name for stage in self.stages)

STAGED_TECHNICAL_REPORT=ObjectivePlan(
    task_id='staged-technical-report',
    title='Staged technical report',
    objective='Produce a structured technical report on designing a token-bucket rate limiter for a public HTTP API.',
    stages=(
        StageSpec('problem','Problem',
                  'Identify the core problem the rate limiter solves: failure modes of an unthrottled public API and measurable requirements (fairness, burst tolerance, latency, abuse resistance).',40),
        StageSpec('solution','Solution',
                  'Develop the solution: token-bucket algorithm, bucket dimensions, refill policy, per-client keying, distributed counters, rejection semantics.',60),
        StageSpec('review','Review',
                  'Review the solution critically: race conditions, cold-start, clock skew, memory bounds. State what changed and why.',50),
        StageSpec('report','Report',
                  'Produce the final report with sections headed Problem, Solution, Review, plus Executive summary and Operations checklist.',80),
    ),
    constraints=('Answer with exactly one JSON object per turn.',
                 'Do not fabricate citations or data.'))

CATALOG={'staged-technical-report':STAGED_TECHNICAL_REPORT}

_BOUNDARY_TERMS=('system prompt','private key','credential','password','secret','api key','training data',
                 'personal data','jailbreak','safeguard','moderation','filter','exploit','exfiltrat','bypass',
                 'prompt injection','extract the','hidden instruction','privilege escalation')

def permitted_tasks(): return sorted(CATALOG)

def authorize_objective(task_id):
    """Step 1: the orchestrator may only select a permitted, benign objective."""
    key=str(task_id or '').strip()
    if not key: raise AuthorizationBoundary('No task was selected; permitted tasks: '+', '.join(permitted_tasks()))
    lowered=' '.join(key.lower().split())
    hits=sorted({term for term in _BOUNDARY_TERMS if term in lowered})
    if hits: raise AuthorizationBoundary('Task crosses the authorization boundary ('+', '.join(hits)+'); permitted tasks: '+', '.join(permitted_tasks()))
    if key in CATALOG: return CATALOG[key]
    if lowered in CATALOG: return CATALOG[lowered]
    raise AuthorizationBoundary(f'Task "{key}" is not in the permitted catalog: '+', '.join(permitted_tasks()))