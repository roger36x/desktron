"""Redaction policy for historical text exposed to models or search indexes."""
import re

_PEM = re.compile(r'-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----',re.S)
_BASIC = re.compile(r'(?i)(\bbasic\s+)[A-Za-z0-9+/=]{8,}')
_GITHUB = re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}\b')
_AWS = re.compile(r'\bAKIA[0-9A-Z]{16}\b')
_EXTRA = re.compile(r'(?i)((?:cookie|session[_-]?id|client[_-]?secret|private[_-]?key)'
                    r'[\s"\x27]*[:=][\s"\x27]*)[^\s"\x27&,;]+')
_USERINFO = re.compile(r'(?i)(https?://)[^\s/@:]+:[^\s/@]+@')


def redact_history_text(value,limit=None):
    text=str(value or '')
    text=re.sub(r'(?i)(bearer\s+)[\w.~/+\-=]+',r'\1[redacted]',text)
    text=re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)'
                r'[\s"\x27]*[:=][\s"\x27]*)[^\s"\x27&,;]+',r'\1[redacted]',text)
    text=re.sub(r'\bsk-[A-Za-z0-9_-]{12,}','[redacted]',text)
    text=_PEM.sub('[redacted private key]',text)
    text=_BASIC.sub(r'\1[redacted]',text)
    text=_GITHUB.sub('[redacted]',text)
    text=_AWS.sub('[redacted]',text)
    text=_EXTRA.sub(r'\1[redacted]',text)
    text=_USERINFO.sub(r'\1[redacted]@',text)
    if limit is None:return text
    return text[:limit]+('…' if len(text)>limit else '')
