"""The LLM provider behind the summariser and the summary search (#296, #297).

Three chat backends, selected by ``LLM_PROVIDER`` (``[llm]`` in ``settings.ini``)
or per service by ``default_params["provider"]``:

* ``template`` (default): no network. The summariser renders a deterministic
  paragraph from the gathered context (:func:`YSE_App.services.summaries.render_template_summary`);
  :func:`chat` refuses so nothing ever calls out by accident.
* ``openai``: any OpenAI-compatible server (``POST {api_base}/chat/completions``):
  OpenAI, Azure-style gateways, vLLM, Ollama, LM Studio, llama.cpp ...
* ``anthropic``: the Anthropic Messages API (``POST {api_base}/v1/messages``).

The API key is never in ``settings.ini``: it is the ``api_key`` (or ``token``)
entry of the :class:`EncryptedCredential` attached to the summary
``ExternalService`` row, or, failing that, the credential whose name or
``service`` slug equals ``LLM_CREDENTIAL`` (#264).

Embeddings come from an OpenAI-compatible ``/embeddings`` endpoint
(``LLM_EMBEDDING_API_BASE`` + ``LLM_EMBEDDING_MODEL``). Without one
:class:`HashedEmbedder` computes a deterministic local hashed bag-of-words
vector, so the summary search works on every deploy, with lexical rather than
semantic matching. Vectors carry the embedder's name and are only compared
with vectors of the same name.

Everything here is plain HTTP through ``requests`` (already a dependency): the
production venv is pinned and no provider SDK is installed there. No summary
is ever generated while rendering a page: :func:`chat` runs inside a job-queue
worker (:mod:`YSE_App.services.summaries`).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

from django.conf import settings
from django.db.models import Q

log = logging.getLogger(__name__)

PROVIDER_TEMPLATE = "template"
PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDERS = (PROVIDER_TEMPLATE, PROVIDER_OPENAI, PROVIDER_ANTHROPIC)
REMOTE_PROVIDERS = (PROVIDER_OPENAI, PROVIDER_ANTHROPIC)

OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
# API model-ID default for the anthropic provider (override with LLM_MODEL / default_params.model).
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
USER_AGENT = "YSE-PZ ai-summaries/1.0"

LOCAL_EMBEDDER_NAME = "local-hash-v1"
LOCAL_EMBEDDER_DIM = 256

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9.+-]*")
_STOPWORDS = frozenset("""
a an the and or of to in on at for from with by is are was were be been being it its this that these those as
which who whom into than then there their has have had not no nor but so if about over under after before
""".split())


class LLMError(Exception):
    """The provider could not be used (not configured, HTTP error, refusal, malformed answer)."""


class LLMNotConfigured(LLMError):
    """No remote provider / model / key is configured; the caller should say so instead of queueing runs."""


# --- configuration -----------------------------------------------------------------

def setting(name: str, default=None):
    value = getattr(settings, name, default)
    return default if value is None else value


def provider_name(params: Optional[Dict] = None) -> str:
    raw = (params or {}).get("provider") or setting("LLM_PROVIDER", PROVIDER_TEMPLATE) or PROVIDER_TEMPLATE
    return str(raw).strip().lower()


def chat_config(params: Optional[Dict] = None) -> Dict:
    """Effective chat settings: ``settings`` overridden by the service's ``default_params``."""
    params = dict(params or {})
    provider = provider_name(params)
    base = str(params.get("api_base") or setting("LLM_API_BASE", "") or "").strip()
    if not base:
        base = ANTHROPIC_DEFAULT_BASE if provider == PROVIDER_ANTHROPIC else OPENAI_DEFAULT_BASE
    model = str(params.get("model") or setting("LLM_MODEL", "") or "").strip()
    if not model and provider == PROVIDER_ANTHROPIC:
        model = str(setting("LLM_ANTHROPIC_DEFAULT_MODEL", ANTHROPIC_DEFAULT_MODEL) or ANTHROPIC_DEFAULT_MODEL)
    temperature = params.get("temperature")
    if temperature is None:
        temperature = setting("LLM_TEMPERATURE", None)
    return {
        "provider": provider,
        "api_base": base.rstrip("/"),
        "model": model,
        "max_tokens": int(params.get("max_tokens") or setting("LLM_MAX_TOKENS", 600) or 600),
        "temperature": None if temperature in (None, "") else float(temperature),
        "timeout": float(params.get("timeout") or setting("LLM_HTTP_TIMEOUT_SECONDS", 60) or 60),
    }


def embedding_config(params: Optional[Dict] = None) -> Dict:
    """Effective embedding settings; an empty ``api_base`` or ``model`` means "use the local hashed embedder"."""
    params = dict(params or {})
    base = str(params.get("embedding_api_base") or setting("LLM_EMBEDDING_API_BASE", "") or "").strip()
    model = str(params.get("embedding_model") or setting("LLM_EMBEDDING_MODEL", "") or "").strip()
    return {
        "api_base": base.rstrip("/"),
        "model": model,
        "timeout": float(setting("LLM_HTTP_TIMEOUT_SECONDS", 60) or 60),
    }


def find_credential(service=None):
    """The credential row for the provider key: the service's, else the one ``LLM_CREDENTIAL`` names."""
    from YSE_App.models.credential_models import EncryptedCredential

    cred = getattr(service, "credential", None) if service is not None else None
    if cred is not None:
        return cred
    name = str(setting("LLM_CREDENTIAL", "") or "").strip()
    if not name:
        return None
    cred = EncryptedCredential.objects.filter(Q(name=name) | Q(service=name), is_active=True).order_by("pk").first()
    if cred is None:
        log.warning("LLM_CREDENTIAL %r names no active credential", name)
    return cred


def api_key(service=None) -> str:
    """The provider key from the credential payload (``api_key``, ``token``, ``api_token``, ``key``, ``bearer``)."""
    cred = find_credential(service)
    if cred is None:
        return ""
    try:
        secret = cred.get_secret(touch=True) or {}
    except Exception as exc:  # noqa: BLE001 - the caller reports "not configured"
        log.warning("credential %s cannot be decrypted: %s", cred.pk, exc)
        return ""
    for key in ("api_key", "token", "api_token", "key", "bearer"):
        if secret.get(key):
            return str(secret[key])
    return ""


def chat_available(service=None, params: Optional[Dict] = None) -> Tuple[bool, str]:
    """``(ok, reason)``: whether a generation run can be queued with the effective provider."""
    cfg = chat_config(params)
    if cfg["provider"] == PROVIDER_TEMPLATE:
        return True, ""
    if cfg["provider"] not in REMOTE_PROVIDERS:
        return False, "unknown LLM provider %r (use template, openai or anthropic)" % cfg["provider"]
    if not cfg["model"]:
        return False, "no model is configured (LLM_MODEL in [llm] or the service's default_params)"
    if not api_key(service):
        return False, ("no API key: store one in an Encrypted credential (api_key entry) attached to the "
                       "summary service or named by LLM_CREDENTIAL")
    return True, ""


# --- HTTP ---------------------------------------------------------------------------

def _post_json(url: str, body: Dict, headers: Dict, timeout: float) -> Dict:
    import requests

    headers = dict(headers)
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("User-Agent", USER_AGENT)
    try:
        response = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise LLMError("%s: %s" % (url, str(exc) or exc.__class__.__name__))
    if response.status_code >= 400:
        text = (response.text or "")[:500]
        raise LLMError("HTTP %s from %s: %s" % (response.status_code, url, text or "empty body"))
    try:
        data = response.json()
    except ValueError:
        raise LLMError("%s did not answer with JSON" % url)
    if not isinstance(data, dict):
        raise LLMError("%s answered with %s, not an object" % (url, type(data).__name__))
    return data


def chat(system: str, user: str, *, service=None, params: Optional[Dict] = None) -> Tuple[str, Dict]:
    """One completion: ``(text, {"provider", "model", "usage"})``. Raises :class:`LLMError`.

    The ``template`` provider never reaches this function (the summariser
    renders the text itself); calling it with that provider raises
    :class:`LLMNotConfigured`.
    """
    cfg = chat_config(params)
    if cfg["provider"] == PROVIDER_TEMPLATE:
        raise LLMNotConfigured("the template provider does not call an LLM")
    ok, reason = chat_available(service, params)
    if not ok:
        raise LLMNotConfigured(reason)
    key = api_key(service)
    if cfg["provider"] == PROVIDER_ANTHROPIC:
        return _chat_anthropic(cfg, key, system, user)
    return _chat_openai(cfg, key, system, user)


def _chat_openai(cfg: Dict, key: str, system: str, user: str) -> Tuple[str, Dict]:
    url = cfg["api_base"] + "/chat/completions"
    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": cfg["max_tokens"],
    }
    if cfg["temperature"] is not None:
        body["temperature"] = cfg["temperature"]
    data = _post_json(url, body, {"Authorization": "Bearer " + key}, cfg["timeout"])
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise LLMError("chat completion without choices: %s" % str(data)[:300])
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some servers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = str(content or "").strip()
    if not text:
        raise LLMError("chat completion with empty content")
    return text, {"provider": PROVIDER_OPENAI, "model": str(data.get("model") or cfg["model"]),
                  "usage": data.get("usage") or {}}


def _chat_anthropic(cfg: Dict, key: str, system: str, user: str) -> Tuple[str, Dict]:
    # Messages API. Sampling parameters are deliberately not sent: current
    # models reject non-default temperature and think adaptively on their own.
    url = cfg["api_base"] + "/v1/messages"
    body = {
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    data = _post_json(url, body, headers, cfg["timeout"])
    if data.get("stop_reason") == "refusal":
        details = data.get("stop_details") or {}
        raise LLMError("the model declined this request (%s)" % (details.get("category") or "refusal"))
    parts = data.get("content") or []
    text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("type") == "text")
    text = text.strip()
    if not text:
        raise LLMError("messages reply with empty content")
    return text, {"provider": PROVIDER_ANTHROPIC, "model": str(data.get("model") or cfg["model"]),
                  "usage": data.get("usage") or {}}


# --- embeddings ----------------------------------------------------------------------

def normalize(vector: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(float(v) * float(v) for v in vector))
    if not norm:
        return [0.0 for _ in vector]
    return [float(v) / norm for v in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def tokenize(text: str) -> List[str]:
    words = [w.strip(".-+") for w in _WORD_RE.findall((text or "").lower())]
    return [w for w in words if w and w not in _STOPWORDS]


class HashedEmbedder:
    """Deterministic hashed bag-of-words (unigrams + bigrams) with a signed hash, L2-normalised.

    Not a semantic embedding: it matches on shared vocabulary ("Type II",
    "Keck", "young"). It exists so the search page works on a deploy with no
    external provider; it needs no download and no key.
    """

    name = LOCAL_EMBEDDER_NAME
    dim = LOCAL_EMBEDDER_DIM

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        tokens = tokenize(text)
        grams = list(tokens) + ["%s %s" % (a, b) for a, b in zip(tokens, tokens[1:])]
        for gram in grams:
            digest = hashlib.sha1(gram.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            weight = 1.0 if " " not in gram else 0.5
            vec[index] += sign * weight
        return normalize(vec)


class RemoteEmbedder:
    """An OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(self, cfg: Dict, key: str):
        self.cfg = cfg
        self.key = key
        self.name = cfg["model"]
        self.dim = 0

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        url = self.cfg["api_base"] + "/embeddings"
        body = {"model": self.cfg["model"], "input": list(texts)}
        data = _post_json(url, body, {"Authorization": "Bearer " + self.key}, self.cfg["timeout"])
        rows = data.get("data") or []
        if len(rows) != len(texts):
            raise LLMError("embeddings endpoint returned %d vectors for %d inputs" % (len(rows), len(texts)))
        rows = sorted(rows, key=lambda r: int(r.get("index", 0)) if isinstance(r, dict) else 0)
        out = []
        for row in rows:
            vec = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(vec, list) or not vec:
                raise LLMError("embeddings endpoint returned a row without an embedding")
            out.append(normalize([float(v) for v in vec]))
        self.dim = len(out[0]) if out else 0
        return out


def embedder(service=None, params: Optional[Dict] = None):
    """The embedder to use now: remote when an ``/embeddings`` endpoint, model and key exist, else local."""
    cfg = embedding_config(params)
    if cfg["api_base"] and cfg["model"]:
        key = api_key(service)
        if key:
            return RemoteEmbedder(cfg, key)
        log.info("embedding endpoint configured but no API key: using the local hashed embedder")
    return HashedEmbedder()


def embedder_name(service=None, params: Optional[Dict] = None) -> str:
    return embedder(service, params).name


def embed_texts(texts: Sequence[str], *, service=None, params: Optional[Dict] = None) -> Tuple[List[List[float]], str]:
    """``(vectors, embedder name)`` for ``texts`` with the current embedder."""
    emb = embedder(service, params)
    vectors = emb.embed(texts)
    return vectors, emb.name
