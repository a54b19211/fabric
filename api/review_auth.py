"""Minimal server-owned reviewer identities for consequential review actions."""
import hmac
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from fastapi import Header, HTTPException

_ISSUER = object()


@dataclass(frozen=True)
class ReviewPrincipal:
    subject: str
    _issuer: object = field(repr=False)


def principal_from_token(token):
    from graph.db.identity import require_human_reviewer
    raw = os.environ.get('DATAFABRIC_REVIEW_TOKENS', '')
    if not raw:
        raise ValueError('Trusted review authentication is not configured')
    try:
        mapping = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError('Trusted review authentication is not configured') from None
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError('Trusted review authentication is not configured')
    if not isinstance(token, str) or not token.isascii():
        raise ValueError('Invalid review token')
    for secret, subject in mapping.items():
        if isinstance(secret, str) and secret.isascii() and isinstance(subject, str) and secret and hmac.compare_digest(secret, token):
            return ReviewPrincipal(require_human_reviewer(subject), _ISSUER)
    raise ValueError('Invalid review token')


def require_trusted_review_context(context):
    if not isinstance(context, ReviewPrincipal) or context._issuer is not _ISSUER:
        raise ValueError('A trusted server review context is required')
    return context.subject


def optional_review_principal(x_review_token: Optional[str] = Header(default=None, alias='X-Review-Token')):
    if x_review_token is None:
        return None
    try:
        return principal_from_token(x_review_token)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


def require_review_principal(x_review_token: Optional[str] = Header(default=None, alias='X-Review-Token')):
    if not x_review_token:
        raise HTTPException(status_code=403, detail='A trusted review token is required')
    return optional_review_principal(x_review_token)
