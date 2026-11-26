"""Independent completion evaluation. External model claims are never treated as evidence."""
import re


class CompletionEvaluator:
    """Treat only official page indicators as proof; model output is never evidence."""
    async def success_verified(self, challenge_page):
        return await challenge_page.success()

    async def submission_confirmed(self, challenge_page):
        return await challenge_page.submitted()


class StagedReportEvaluator:
    """Independently verifies a staged objective from actual response content.

    The external model may claim completion; this evaluator ignores that claim and
    verifies stage coverage, structural content gates, and final-report sections.
    """
    def __init__(self, plan):
        self.plan=plan
        self.sections={}          # stage -> {'iteration','key_points','words','content'}
        self.consecutive_invalid=0
        self.model_completion_claims=0

    def _spec(self, stage):
        return next((spec for spec in self.plan.stages if spec.name==stage),None)

    def completed_stages(self):
        return [spec.name for spec in self.plan.stages if spec.name in self.sections]

    def next_stage(self):
        """First stage that has not yet been independently verified."""
        for spec in self.plan.stages:
            if spec.name not in self.sections: return spec.name
        return None

    def retry_stage(self):
        """Stage the next request must target: first missing, else the final stage again."""
        return self.next_stage() or self.plan.stages[-1].name

    def restore(self, records):
        """Rebuild verified section state from persisted iteration records after a restart."""
        for record in sorted(records,key=lambda item:item.get('iteration') or 0):
            payload=record.get('response_payload') or {}
            parsed=payload.get('parsed')
            stage=record.get('stage')
            if record.get('response_status')=='ok' and parsed and parsed.get('stage')==stage and stage:
                self.sections[stage]={'iteration':record.get('iteration'),'key_points':parsed.get('key_points') or [],
                                      'words':len(str(parsed.get('content') or '').split()),
                                      'content':parsed.get('content') or ''}
        return self

    def evaluate(self, iteration, expected_stage, parsed, error=None):
        """Verify one iteration and decide the next step without trusting the model."""
        if error is not None:
            self.consecutive_invalid+=1
            return {'iteration':iteration,'stage':expected_stage,'valid':False,'complete':False,'decision':'reject',
                    'reasons':[f'external response validation failed: {error}'],'next_stage':expected_stage,
                    'next_action':f'resend a schema-valid "{expected_stage}" response',
                    'model_claimed_complete':False,'words':0}
        self.consecutive_invalid=0
        claimed=bool(parsed.get('model_claimed_complete'))
        self.model_completion_claims+=1 if claimed else 0
        spec=self._spec(expected_stage)
        reasons=[]
        words=len(parsed['content'].split())
        if spec and words<spec.min_words:
            reasons.append(f'"{expected_stage}" content has {words} words; at least {spec.min_words} required')
        self.sections[expected_stage]={'iteration':iteration,'key_points':parsed['key_points'],'words':words,'content':parsed['content']}
        verdict=self.verify()
        reasons=verdict['reasons']+reasons
        complete=verdict['complete'] and not reasons
        next_stage=self.retry_stage() if not complete else None
        if complete: next_action='terminate: objective independently verified'
        elif self.next_stage(): next_action=f'request stage "{self.next_stage()}" from the external model'
        else: next_action=f'request a corrected final "{self.plan.stages[-1].name}" report (unverified sections remain)'
        return {'iteration':iteration,'stage':expected_stage,'valid':True,'complete':complete,
                'decision':'complete' if complete else 'continue','reasons':reasons[:8],'next_stage':next_stage,
                'next_action':next_action,'model_claimed_complete':claimed,'words':words}

    def verify(self):
        """Independent completion verdict from recorded sections only."""
        reasons=[f'stage "{spec.name}" has no verified response' for spec in self.plan.stages if spec.name not in self.sections]
        final=self.plan.stages[-1].name
        if final in self.sections:
            content=self.sections[final]['content']
            for spec in self.plan.stages[:-1]:
                if not re.search(rf'(?im)^\s*#{{0,4}}\s*{re.escape(spec.name)}\b',content):
                    reasons.append(f'final report lacks a verified "{spec.name}" section heading')
            minimum=self.plan.stages[-1].min_words
            if len(content.split())<minimum:
                reasons.append(f'final report has {len(content.split())} words; at least {minimum} required')
        return {'complete':not reasons,'reasons':reasons}