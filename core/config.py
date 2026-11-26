from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shlex
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

@dataclass
class Config:
    mistral_model: str = ""
    mistral_base_url: str = "https://integrate.api.nvidia.com/v1"
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 1200
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0
    api_timeout: int = 60
    browser: str = "chromium"
    headless: bool = False
    profile_path: str = ".browser-profile"
    listing_url: str = "https://app.grayswan.ai/arena"
    challenge_url_template: str = "https://app.grayswan.ai/arena/challenge/{slug}"
    challenge_entry_url: str = "https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26?panel=criteria"
    concurrency: int = 8
    api_concurrency: int = 4
    expected_challenges: int = 37
    max_iterations: int = 20
    challenge_timeout: int = 1800
    retries: int = 4
    success_selector: str = ""
    submit_selector: str = ""
    submission_confirmation_selector: str = ""
    input_selector: str = "textarea"
    send_selector: str = "button[aria-label='Submit Chat']"
    response_selector: str = ".group\\/reply .prose"
    challenge_link_selector: str = "a[href*='/challenge/']"
    next_page_selector: str = ""
    description_selector: str = ""
    criteria_selector: str = ""
    challenge_adapters: dict = field(default_factory=dict)
    database_path: str = "data/automator.sqlite3"
    log_level: str = "INFO"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000
    interaction_time_limit: int = 1800
    interaction_request_timeout: int = 120
    interaction_context_messages: int = 8
    interaction_max_malformed: int = 3
    interaction_retry_backoff: float = 0.5
    interaction_backoff_base: float = 1.0

    def validate(self,require_api=False):
        if self.concurrency != 8: raise ValueError('concurrency must be exactly 8')
        if self.api_concurrency < 1 or self.retries < 0: raise ValueError('API concurrency must be positive and retries nonnegative')
        if self.expected_challenges < 1 or self.max_iterations < 1 or self.challenge_timeout < 1: raise ValueError('Inventory, iteration, and runtime limits must be positive')
        for label,url in (('Arena listing',self.listing_url),('challenge entry',self.challenge_entry_url),('Mistral API',self.mistral_base_url)):
            parsed=urlparse(url)
            if parsed.scheme!='https' or not parsed.netloc: raise ValueError(f'{label} URL must use HTTPS')
        for label,url in (('Arena listing',self.listing_url),('challenge entry',self.challenge_entry_url)):
            if urlparse(url).hostname!='app.grayswan.ai': raise ValueError(f'{label} must remain on app.grayswan.ai')
        if require_api and (not self.api_key or not self.mistral_model): raise ValueError('MISTRAL_API_KEY and MISTRAL_MODEL are required')
        verification=(self.success_selector,self.submit_selector,self.submission_confirmation_selector)
        if any(value.strip() for value in verification) and not all(value.strip() for value in verification):
            raise ValueError('Configure all official success, submit, and confirmation selectors together')

    def official_selectors_configured(self):
        return all(value.strip() for value in (self.success_selector,self.submit_selector,
                                               self.submission_confirmation_selector,
                                               self.criteria_selector))

def _load_dotenv(path):
    if not path.exists(): return
    for line_no,raw in enumerate(path.read_text().splitlines(),1):
        line=raw.strip()
        if not line or line.startswith('#'): continue
        if line.startswith('export '): line=line[7:].lstrip()
        key,sep,value=line.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key.strip()):
            raise ValueError(f'Invalid .env entry on line {line_no}')
        try:
            words=shlex.split(value,comments=True,posix=True)
            parsed=' '.join(words) if words else ''
        except ValueError as exc: raise ValueError(f'Invalid .env value on line {line_no}') from exc
        os.environ.setdefault(key.strip(),parsed)

def load_config(validate=False,require_api=False):
    _load_dotenv(ROOT/'.env')
    p = ROOT / "config.yaml"
    if not p.exists(): raise FileNotFoundError(f'Missing central configuration: {p}')
    try:
        import yaml
    except ImportError as exc: raise RuntimeError('Install requirements.txt (PyYAML is required)') from exc
    data=yaml.safe_load(p.read_text()) or {}
    if not isinstance(data,dict): raise ValueError('config.yaml must contain a mapping')
    cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
    cfg.api_key = os.getenv("MISTRAL_API_KEY", "")
    cfg.mistral_model = os.getenv("MISTRAL_MODEL", cfg.mistral_model)
    cfg.mistral_base_url = os.getenv("MISTRAL_BASE_URL", cfg.mistral_base_url).rstrip("/")
    cfg.profile_path = str((ROOT / cfg.profile_path).resolve()) if not Path(cfg.profile_path).is_absolute() else cfg.profile_path
    cfg.database_path = str((ROOT / cfg.database_path).resolve())
    if validate: cfg.validate(require_api=require_api)
    return cfg
