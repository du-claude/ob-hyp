# Adapted from the running OB source; see SOURCE.md.
import os
import re
import uuid
import yaml
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result

def setup_logging(level: str='INFO') -> None:
    log_level = getattr(logging, level.upper(), None)
    if not isinstance(log_level, int):
        log_level = logging.INFO
    logging.basicConfig(level=log_level, format='[%(asctime)s] %(name)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S', handlers=[logging.StreamHandler()])

def generate_bucket_id() -> str:
    return uuid.uuid4().hex[:12]

def strip_wikilinks(text: str) -> str:
    return re.sub('\\[\\[([^\\]]+)\\]\\]', '\\1', text) if text else text

def sanitize_name(name: str) -> str:
    if not isinstance(name, str):
        return 'unnamed'
    cleaned = re.sub('[^\\w\\s\\u4e00-\\u9fff-]', '', name, flags=re.UNICODE)
    cleaned = cleaned.strip()[:80]
    return cleaned if cleaned else 'unnamed'

def safe_path(base_dir: str, filename: str) -> Path:
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f'Path safety check failed / 路径安全检查失败: {target} is not inside / 不在 {base} 内')
    return target

def count_tokens_approx(text: str) -> int:
    if not text:
        return 0
    chinese_chars = len(re.findall('[\\u4e00-\\u9fff]', text))
    english_words = len(re.findall('[a-zA-Z]+', text))
    return int(chinese_chars * 1.5 + english_words * 1.3 + len(text) * 0.05)
_CST = timezone(timedelta(hours=8))
_TOKYO = timezone(timedelta(hours=9))
_TAIPEI = timezone(timedelta(hours=8))

def now_cst() -> datetime:
    return datetime.now(_TAIPEI)

def now_tokyo() -> datetime:
    return now_cst()

def now_iso() -> str:
    return now_cst().isoformat(timespec='seconds')

def parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_CST)
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=_CST)
VALID_STATUS = ('open', 'closed', 'superseded')
VALID_SURFACE = ('always', 'rotation', 'time_fade', 'hidden')

def effective_status(meta: dict) -> str:
    s = meta.get('status')
    if s in VALID_STATUS:
        return s
    return 'closed' if meta.get('resolved', False) else 'open'

def effective_surface(meta: dict) -> str:
    s = meta.get('surface')
    if s in VALID_SURFACE:
        return s
    if meta.get('pinned') or meta.get('protected'):
        return 'always'
    if meta.get('anchor'):
        return 'rotation'
    if meta.get('digested') or meta.get('resolved'):
        return 'hidden'
    return 'time_fade'
VALID_AUDIENCE_PARTS = ()
_EXTRA_AUDIENCE_PARTS: set = set()

def register_audience_parts(parts) -> None:
    for p in parts or ():
        p = str(p).strip()
        if p and p not in VALID_AUDIENCE_PARTS:
            _EXTRA_AUDIENCE_PARTS.add(p)

def valid_audience_parts() -> tuple:
    return VALID_AUDIENCE_PARTS + tuple(sorted(_EXTRA_AUDIENCE_PARTS))

def _audience_hint() -> str:
    return ' / '.join(valid_audience_parts()) + ' / family'

def normalize_audience(value):
    if value is None:
        return None
    valid = valid_audience_parts()
    parts: list = []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f'audience 不能为空；合法值: {_audience_hint()}')
        parts = [p.strip() for p in raw.split(',')]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value]
    else:
        raise ValueError(f'audience 类型不支持: {type(value).__name__}')
    expanded: list = []
    for p in parts:
        if not p:
            continue
        if p.lower() == 'family':
            expanded.extend(valid)
        else:
            expanded.append(p)
    if not expanded:
        raise ValueError(f'audience 不能为空；合法值: {_audience_hint()}')
    unknown = sorted({p for p in expanded if p not in valid})
    if unknown:
        raise ValueError(f'audience 含未知声部 {unknown}；合法值: {_audience_hint()}')
    return sorted(set(expanded))

def bucket_audience(meta: dict):
    a = meta.get('audience')
    if isinstance(a, list) and a:
        valid = valid_audience_parts()
        if all((p in valid for p in a)):
            return sorted(set(a))
        return None
    return None
