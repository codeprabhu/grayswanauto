import hashlib
from urllib.parse import urlparse

def parse_candidate(url,name,description='',criteria=''):
    parsed=urlparse(url)
    canonical=parsed._replace(query='',fragment='').geturl().rstrip('/')
    slug=parsed.path.rstrip('/').split('/')[-1]
    cid=hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return {'id':cid,'slug':slug,'name':name.strip() or slug,'url':canonical,'description':description.strip(),'criteria':criteria.strip()}
