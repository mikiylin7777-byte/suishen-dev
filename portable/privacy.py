"""One conservative classifier/redactor for model, notification and archive boundaries."""
import re

REDACTED='[凭据已脱敏]'
KEYS=r'api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|client[_-]?secret|app[_-]?secret|password|passwd|secret|密码|密钥|验证码|cookie|token'
SENSITIVE_KEY=re.compile(r'(?:'+KEYS+r')',re.I)
# Only exact documented placeholders. A user's password starting with your_ is secret.
PLACEHOLDER=re.compile(r'(?i)^(?:YOUR_API_KEY|YOUR_TOKEN|YOUR_PASSWORD|<API_KEY>|<TOKEN>|<PASSWORD>|\[凭据已脱敏\]|redacted|placeholder)$')
QUOTED=re.compile(r'''(?<![\w])(?P<label>["']?(?:'''+KEYS+r''')["']?\s*[:=：]\s*)(?P<quote>["'])(?P<body>(?:\\.|(?!(?P=quote))[^\\])*)(?P=quote)''',re.I)
BARE=re.compile(r'''(?P<label>(?<![\w])["']?(?:'''+KEYS+r''')["']?\s*[:=：]\s*)(?P<body>[^\s"'<>;,}{\[\]]+)''',re.I)
PATTERNS=[
 re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----.*?(?:-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|\Z)',re.S),
 re.compile(r'\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b'),
 re.compile(r'\b(?:AIza[0-9A-Za-z_-]{20,}|atm_[A-Za-z0-9_-]{10,})\b'),
 re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b'),
 re.compile(r'(?i)\b(?:Basic|Bearer)\s+[A-Za-z0-9._~+/=-]{12,}'),
 re.compile(r'''(?i)(?<![\w])["']?authorization["']?\s*[:=：]\s*["']?(?:Basic|Bearer)\s+[A-Za-z0-9._~+/=-]+'''),
 re.compile(r'(?im)^Cookie\s*:[^\r\n]+'),
 re.compile(r'''(?i)(?:(?<![\w])["']?authorization["']?\s*[:=：]\s*["']?)[A-Za-z0-9._~+/=-]{16,}'''),
# Database/broker URLs that carry user:password@ (gitleaks does not flag these). Pure ${VAR}/<placeholder> passwords are not secrets.
 re.compile(r'''(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?|amqps?)(?:\+[a-z0-9]+)?://[^\s:/@'"]*:(?!\$\{|\$[A-Za-z_]|<[^>\s]*>@|\*+@)[^\s@/'"]+@'''),
]

def redact_text(text):
    text=str(text)
    def quoted(match):
        return match.group(0) if PLACEHOLDER.fullmatch(match['body']) else match['label']+match['quote']+REDACTED+match['quote']
    text=QUOTED.sub(quoted,text)
    text=BARE.sub(lambda m:m.group(0) if PLACEHOLDER.fullmatch(m['body']) else m['label']+REDACTED,text)
    for pattern in PATTERNS:text=pattern.sub(REDACTED,text)
    return text

def sanitize(value):
    if isinstance(value,str):return redact_text(value)
    if isinstance(value,list):return [sanitize(x) for x in value]
    if isinstance(value,tuple):return tuple(sanitize(x) for x in value)
    if isinstance(value,dict):
        result={}
        for k,v in value.items():
            sensitive=bool(SENSITIVE_KEY.fullmatch(str(k)))
            if str(k).lower()=='authorization' and isinstance(v,str) and re.fullmatch(r'(?:[A-Za-z0-9._~+/=-]{16,}|(?i:Basic|Bearer)\s+\S+)',v):sensitive=True
            result[redact_text(k)]=REDACTED if sensitive and v not in (None,'') and not (isinstance(v,str) and PLACEHOLDER.fullmatch(v)) else sanitize(v)
        return result
    return value

class CredentialContent:
    def search(self,data):
        text=data.decode('utf-8','replace') if isinstance(data,bytes) else str(data)
        for m in QUOTED.finditer(text):
            if PLACEHOLDER.fullmatch(m['body']):continue
            label=m['label'].lstrip();body=m['body']
            if label.startswith(('\"',"'")):return True
            if re.fullmatch(r'\+[A-Za-z_][A-Za-z_0-9]*\+',body):continue
            if body.startswith(('$(', '${')):continue
            if len(body)>=8 or re.search('password|passwd|密码|密钥|验证码',label,re.I):return True
        if any(pattern.search(text) for pattern in PATTERNS):return True
        for m in BARE.finditer(text):
            body=m['body']
            if m['label'].lstrip().startswith(('\"',"'")) and text[max(0,m.start()-3):m.start()].rstrip().endswith(('==','!=')):continue
            # Match real opaque assignments, not Python attribute/index expressions.
            if not PLACEHOLDER.fullmatch(body) and (re.fullmatch(r'[A-Za-z0-9_+/=-]{8,}',body) or re.search('密码|密钥|验证码',m['label']) or (re.search('password|passwd',m['label'],re.I) and body.isdigit())):return True
        return False


def secret_lines(text):
    """Line numbers (1-based) that the conservative classifier flags; never returns the values themselves."""
    detector=CredentialContent()
    return [n for n,line in enumerate(str(text).splitlines(),1) if detector.search(line)]
