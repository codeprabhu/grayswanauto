"""Exact allowlist for the November 2026 Hazard Hunt edition."""
from urllib.parse import urlparse

CHALLENGE_ID='a3fae18d3d88e459ad14c613'
CHALLENGE_NAME="Hazard Hunt November '26"
CHALLENGE_URL='https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26'
CHALLENGE_PATH='/arena/challenge/hazard-hunt-november-26'
SCENARIO_IDS=frozenset({
    'reproduce-copyrighted-lyrics-over-the-rainbow',
    'reproduce-copyrighted-text-1984-opening',
    'reproduce-copyrighted-novel-ring-verse',
    'reproduce-copyrighted-lyrics-fresh-prince-theme',
    'reproduce-copyrighted-lyrics-shake-it-off',
    'reproduce-copyrighted-book-hacking-art-of-exploitation-ch1',
    'reproduce-copyrighted-poem-still-i-rise',
    'reproduce-copyrighted-ad-script-think-different',
    'reproduce-copyrighted-film-text-star-wars-opening-crawl',
    'reproduce-copyrighted-speech-i-have-a-dream-opening',
    'extract-sandbox-command-policy-drayline-subagent-system-prompt',
    'print-policy-exceptions-leak',
    'extract-moderation-escalation-rules-loopyard',
    'extract-chat-filter-configuration-saltreach-subagent-document',
    'extract-fraud-review-triggers-hollin-creek-restricted-document',
    'extract-system-prompt-therapy-bot-clara',
    'extract-fee-waiver-compensation-rules-northfold',
    'extract-customer-support-policy-fernwood',
    'people-insights-new-hire',
    'speak-up-case-anonymous-reporter',
    'riverside-auto-sms-output',
    'harbor-loom-translation-trial',
})

class ScopeViolation(ValueError):
    pass

def validate_challenge_scope(challenge):
    """Require the exact inventory ID, title, HTTPS origin, and November route."""
    if not isinstance(challenge,dict): raise ScopeViolation('Challenge record must be a mapping')
    parsed=urlparse(str(challenge.get('url','')))
    if challenge.get('id')!=CHALLENGE_ID or challenge.get('name')!=CHALLENGE_NAME:
        raise ScopeViolation('Challenge is outside the Hazard Hunt November allowlist')
    if parsed.scheme!='https' or parsed.netloc!='app.grayswan.ai' or parsed.path!=CHALLENGE_PATH or parsed.query or parsed.fragment:
        raise ScopeViolation('Challenge URL is outside the exact Hazard Hunt November route')
    return challenge

def validate_challenge_url(challenge_id,url):
    parsed=urlparse(str(url))
    if (challenge_id!=CHALLENGE_ID or parsed.scheme!='https' or parsed.netloc!='app.grayswan.ai'
            or parsed.path!=CHALLENGE_PATH or parsed.query or parsed.fragment):
        raise ScopeViolation('Browser navigation is outside the exact Hazard Hunt November route')
    return url

def validate_scenario_scope(challenge_id,scenario_id):
    if challenge_id!=CHALLENGE_ID or scenario_id not in SCENARIO_IDS:
        raise ScopeViolation('Scenario is outside the Hazard Hunt November allowlist')
    return scenario_id

def validate_scenario_inventory(challenge):
    validate_challenge_scope(challenge)
    scenarios=challenge.get('scenario_details') or []
    ids=[scenario.get('id') for scenario in scenarios]
    if len(ids)!=len(SCENARIO_IDS) or set(ids)!=SCENARIO_IDS:
        raise ScopeViolation('Rendered November scenario IDs do not exactly match the approved scenario allowlist')
    for scenario in scenarios:
        validate_scenario_scope(challenge['id'],scenario.get('id'))
        if not scenario.get('description','').strip() or not scenario.get('criteria','').strip():
            raise ScopeViolation(f"Scenario {scenario.get('id')} lacks rendered description or official criteria")
    return scenarios

def is_in_scope(challenge):
    try: validate_challenge_scope(challenge)
    except (ScopeViolation,TypeError): return False
    return True
