import json

def parse_plan(text, allowed_actions=None):
    """Parse and strictly validate the narrow browser action contract."""
    try:
        obj=json.loads(text)
    except json.JSONDecodeError:
        start=text.find('{'); end=text.rfind('}')
        if start < 0 or end <= start: raise ValueError('Model response was not JSON')
        obj=json.loads(text[start:end+1])
    allowed=tuple(allowed_actions or ('OBSERVE','INTERACT','FILL','CLICK','SELECT','PRESS','WAIT','VERIFY','SUBMIT','COMPLETE','PAUSE'))
    if not isinstance(obj,dict) or obj.get('action') not in allowed:
        raise ValueError(f'Unsupported action; allowed: {", ".join(allowed)}')
    fields={'OBSERVE':{'action'},'INTERACT':{'action','text'},'FILL':{'action','ref','value'},'CLICK':{'action','ref'},'SELECT':{'action','ref','value'},'PRESS':{'action','ref','key'},'WAIT':{'action','seconds'},
            'VERIFY':{'action'},'SUBMIT':{'action'},'COMPLETE':{'action'},'PAUSE':{'action','reason'}}[obj['action']]
    if obj['action']=='SELECT' and 'value' not in obj: fields={'action','ref'}
    if set(obj)-fields: raise ValueError(f'Unexpected fields for {obj["action"]}: {", ".join(sorted(set(obj)-fields))}')
    if obj['action']=='INTERACT' and (not isinstance(obj.get('text'),str) or not obj['text'].strip() or len(obj['text'])>8000):
        raise ValueError('INTERACT requires text of 1 to 8000 characters')
    if obj['action']=='FILL' and (not isinstance(obj.get('value'),str) or len(obj['value'])>8000): raise ValueError('FILL requires a string value of at most 8000 characters')
    if obj['action']=='SELECT' and 'value' in obj and (not isinstance(obj.get('value'),str) or len(obj['value'])>8000): raise ValueError('SELECT value must be a string of at most 8000 characters')
    if obj['action'] in ('FILL','CLICK','SELECT','PRESS') and not str(obj.get('ref','')).isdigit(): raise ValueError('Browser action target must be a numeric ref from the current observation')
    if obj['action']=='PRESS' and obj.get('key') not in ('Enter','Tab','Escape','ArrowUp','ArrowDown','Space',' '): raise ValueError('PRESS key is outside the safe key allowlist')
    if obj.get('action')=='WAIT':
        seconds=obj.get('seconds',3)
        if isinstance(seconds,bool) or not isinstance(seconds,(int,float)): raise ValueError('WAIT seconds must be numeric')
        obj['seconds']=max(1,min(15,int(seconds)))
    if obj['action']=='PAUSE': obj['reason']=str(obj.get('reason','Agent requested pause'))[:500]
    return obj

def parse_single_test_plan(text):
    """Strict action contract; targets must reference the current rendered observation."""
    try: obj=json.loads(text)
    except json.JSONDecodeError:
        start=text.find('{'); end=text.rfind('}')
        if start<0 or end<=start: raise ValueError('Model response was not JSON')
        obj=json.loads(text[start:end+1])
    if not isinstance(obj,dict) or obj.get('action') not in ('OBSERVE','FILL','CLICK','SELECT','PRESS','WAIT','VERIFY','PAUSE'):
        raise ValueError('Unsupported action; allowed: OBSERVE, FILL, CLICK, SELECT, PRESS, WAIT, VERIFY, PAUSE')
    action=obj['action']
    fields={'OBSERVE':{'action'},'FILL':{'action','ref','value'},'CLICK':{'action','ref'},'SELECT':{'action','ref','value'},'PRESS':{'action','ref','key'},'WAIT':{'action','seconds'},'VERIFY':{'action'},'PAUSE':{'action','reason'}}[action]
    if action=='SELECT' and 'value' not in obj: fields={'action','ref'}
    if set(obj)-fields: raise ValueError(f'Unexpected fields for {action}: {", ".join(sorted(set(obj)-fields))}')
    if action=='FILL' and (not isinstance(obj.get('value'),str) or len(obj['value'])>8000): raise ValueError('FILL requires a string value of at most 8000 characters')
    if action=='SELECT' and 'value' in obj and (not isinstance(obj.get('value'),str) or len(obj['value'])>8000): raise ValueError('SELECT value must be a string of at most 8000 characters')
    if action in ('FILL','CLICK','SELECT','PRESS') and (not str(obj.get('ref','')).isdigit()): raise ValueError(f'{action} requires a numeric ref from the current observation')
    if action=='PRESS' and obj.get('key') not in ('Enter','Tab','Escape','ArrowUp','ArrowDown','Space',' '): raise ValueError('PRESS key is outside the safe key allowlist')
    if action=='WAIT':
        seconds=obj.get('seconds',2)
        if isinstance(seconds,bool) or not isinstance(seconds,(int,float)): raise ValueError('WAIT seconds must be numeric')
        obj['seconds']=max(1,min(10,int(seconds)))
    if action=='PAUSE': obj['reason']=str(obj.get('reason','Agent requested pause'))[:500]
    return obj
