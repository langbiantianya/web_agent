"""Generic OIDC discovery、code 交换与 ID Token 校验。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import jwt
from jwt import InvalidTokenError, PyJWK

from app.config.settings import settings
from app.core.crypto import decrypt
from app.models.oauth import OAuthProvider

_ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "EdDSA"})
_JWKS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_JWKS_LOCK = asyncio.Lock()
_JWKS_TTL_SECONDS = 3600


class OIDCError(Exception):
    """OIDC 协议或网络错误；消息可直接映射为稳定登录失败码。"""


@dataclass(frozen=True)
class OIDCEndpoints:
    """一次登录需要的三个 OIDC 端点。"""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


def _is_forbidden_address(address: str) -> bool:
    """判断 DNS 结果是否是内网、回环、链路本地或保留地址。"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def validate_provider_url(url: str) -> None:
    """校验供应商 URL，默认拒绝私网地址并强制 HTTPS。

    地址来自管理员配置而非终端用户，但 discovery 文档中的 endpoint 仍可能被
    恶意供应商指向内网。这里在每个 endpoint 调用前重新校验。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise OIDCError("invalid_provider_url")
    if settings.oauth_allow_private_networks:
        return
    if parsed.scheme != "https":
        raise OIDCError("insecure_provider_url")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname,
            parsed.port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise OIDCError("provider_unreachable") from exc
    if not infos or any(_is_forbidden_address(info[4][0]) for info in infos):
        raise OIDCError("provider_private_address")


async def _fetch_json(url: str) -> dict:
    """不跟随重定向地读取 JSON，防止 discovery 把请求带到未校验地址。"""
    await validate_provider_url(url)
    async with httpx.AsyncClient(
        timeout=settings.oauth_request_timeout_seconds,
        follow_redirects=False,
    ) as client:
        response = await client.get(url, headers={"Accept": "application/json"})
    if response.status_code != 200:
        raise OIDCError("provider_discovery_failed")
    try:
        data = response.json()
    except ValueError as exc:
        raise OIDCError("provider_discovery_invalid") from exc
    if not isinstance(data, dict):
        raise OIDCError("provider_discovery_invalid")
    return data


async def _post_form(
    url: str,
    data: dict[str, str],
    *,
    basic_auth: tuple[str, str] | None = None,
) -> dict:
    """向 token endpoint 提交表单，不跟随重定向。"""
    await validate_provider_url(url)
    async with httpx.AsyncClient(
        timeout=settings.oauth_request_timeout_seconds,
        follow_redirects=False,
    ) as client:
        response = await client.post(
            url,
            data=data,
            auth=basic_auth,
            headers={"Accept": "application/json"},
        )
    if response.status_code != 200:
        raise OIDCError("token_exchange_failed")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OIDCError("token_response_invalid") from exc
    if not isinstance(payload, dict):
        raise OIDCError("token_response_invalid")
    return payload


async def resolve_provider_endpoints(provider: OAuthProvider) -> OIDCEndpoints:
    """从显式 endpoint 或 discovery 文档解析并校验 OIDC 端点。"""
    issuer = provider.issuer.rstrip("/")
    discovery_url = provider.discovery_url or (
        issuer + "/.well-known/openid-configuration"
    )
    authorization = provider.authorization_endpoint
    token = provider.token_endpoint
    jwks = provider.jwks_uri
    if not (authorization and token and jwks):
        document = await _fetch_json(discovery_url)
        document_issuer = str(document.get("issuer", "")).rstrip("/")
        if document_issuer != issuer:
            raise OIDCError("issuer_mismatch")
        authorization = authorization or document.get("authorization_endpoint")
        token = token or document.get("token_endpoint")
        jwks = jwks or document.get("jwks_uri")
    endpoints = OIDCEndpoints(
        issuer=issuer,
        authorization_endpoint=str(authorization or ""),
        token_endpoint=str(token or ""),
        jwks_uri=str(jwks or ""),
    )
    for endpoint in (
        endpoints.authorization_endpoint,
        endpoints.token_endpoint,
        endpoints.jwks_uri,
    ):
        await validate_provider_url(endpoint)
    return endpoints


async def exchange_code(
    provider: OAuthProvider,
    endpoints: OIDCEndpoints,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> str:
    """用 authorization code + PKCE verifier 换取 ID Token。"""
    secret = decrypt(provider.client_secret_cipher)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": provider.client_id,
        "code_verifier": code_verifier,
    }
    basic_auth: tuple[str, str] | None = None
    if secret:
        if provider.token_auth_method == "client_secret_post":
            data["client_secret"] = secret
        else:
            basic_auth = (provider.client_id, secret)
    payload = await _post_form(
        endpoints.token_endpoint,
        data,
        basic_auth=basic_auth,
    )
    id_token = payload.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise OIDCError("id_token_missing")
    return id_token


async def _load_jwks(url: str, *, force: bool = False) -> list[dict]:
    """读取并缓存 JWKS；未知 kid 时强制刷新一次以支持 key rotation。"""
    now = time.monotonic()
    async with _JWKS_LOCK:
        cached = _JWKS_CACHE.get(url)
        if not force and cached is not None and cached[0] > now:
            return cached[1]
        document = await _fetch_json(url)
        keys = document.get("keys")
        if not isinstance(keys, list) or not all(isinstance(k, dict) for k in keys):
            raise OIDCError("jwks_invalid")
        _JWKS_CACHE[url] = (now + _JWKS_TTL_SECONDS, keys)
        return keys


def _select_jwk(keys: list[dict], kid: str | None) -> dict | None:
    """按 kid 选择签名 key；无 kid 时只接受唯一候选。"""
    if kid:
        matches = [key for key in keys if key.get("kid") == kid]
        return matches[0] if len(matches) == 1 else None
    return keys[0] if len(keys) == 1 else None


async def verify_id_token(
    provider: OAuthProvider,
    endpoints: OIDCEndpoints,
    id_token: str,
    *,
    expected_nonce_hash: str,
) -> dict:
    """验签并严格校验 issuer、audience、时间与 nonce。"""
    try:
        header = jwt.get_unverified_header(id_token)
    except InvalidTokenError as exc:
        raise OIDCError("id_token_invalid") from exc
    algorithm = header.get("alg")
    if algorithm not in _ALLOWED_ALGORITHMS:
        raise OIDCError("id_token_algorithm_rejected")

    kid = header.get("kid")
    keys = await _load_jwks(endpoints.jwks_uri)
    selected = _select_jwk(keys, str(kid) if kid else None)
    if selected is None:
        keys = await _load_jwks(endpoints.jwks_uri, force=True)
        selected = _select_jwk(keys, str(kid) if kid else None)
    if selected is None:
        raise OIDCError("signing_key_not_found")
    jwk_algorithm = selected.get("alg")
    if jwk_algorithm and jwk_algorithm != algorithm:
        raise OIDCError("signing_key_algorithm_mismatch")

    try:
        claims = jwt.decode(
            id_token,
            PyJWK(selected).key,
            algorithms=[algorithm],
            audience=provider.client_id,
            issuer=endpoints.issuer,
            leeway=30,
            options={
                "require": ["exp", "iat", "iss", "aud", "sub", "nonce"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except InvalidTokenError as exc:
        raise OIDCError("id_token_invalid") from exc

    audience = claims.get("aud")
    azp = claims.get("azp")
    if isinstance(audience, list) and len(audience) > 1:
        if azp != provider.client_id:
            raise OIDCError("authorized_party_missing")
    if azp is not None and azp != provider.client_id:
        raise OIDCError("authorized_party_mismatch")
    claim_nonce = claims.get("nonce")
    if not isinstance(claim_nonce, str) or not hmac_compare_hash(
        claim_nonce, expected_nonce_hash
    ):
        raise OIDCError("nonce_mismatch")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise OIDCError("subject_missing")
    return claims


def hmac_compare_hash(value: str, expected_hash: str) -> bool:
    """常量时间比较字符串的 SHA-256 与库中摘要。"""
    import hmac

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, expected_hash)
