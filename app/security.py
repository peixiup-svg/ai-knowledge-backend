import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User

password_hasher = PasswordHash.recommended()
_dummy_hash = password_hasher.hash("timing-equalization-password-only")
bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    result = password_hasher.verify(password, password_hash or _dummy_hash)
    return bool(password_hash and result)


def create_access_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": user_id,
            "iat": now,
            "exp": now + timedelta(minutes=get_settings().jwt_expire_minutes),
            "iss": "knowledge-backend",
            "aud": "knowledge-api",
            "jti": str(uuid.uuid4()),
        },
        get_settings().jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: Session = Depends(get_db),
) -> User:
    denied = HTTPException(
        status_code=401, detail="请先登录，或重新获取有效令牌。", headers={"WWW-Authenticate": "Bearer"}
    )
    if credentials is None:
        raise denied
    try:
        payload = jwt.decode(
            credentials.credentials,
            get_settings().jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience="knowledge-api",
            issuer="knowledge-backend",
            options={"require": ["sub", "exp", "iat"]},
        )
        user = session.get(User, payload["sub"])
    except (jwt.PyJWTError, TypeError, ValueError):
        raise denied from None
    if user is None:
        raise denied
    # End the read transaction before a potentially long SSE response.
    session.expunge(user)
    session.rollback()
    return user
