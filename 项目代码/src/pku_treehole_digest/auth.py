from __future__ import annotations

import getpass
import base64
import json
import os
import re
import stat
import uuid as uuid_lib
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
import keyring
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding


IAAA_LOGIN_URL = "https://iaaa.pku.edu.cn/iaaa/oauthlogin.do"
TREEHOLE_CALLBACK = "https://treehole.pku.edu.cn/chapi/cas_iaaa_login"
KEYRING_SERVICE = "PKU Treehole Daily Digest"


def session_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "PKUTreeholeDigest" / "session.json"


@dataclass(slots=True)
class Credentials:
    token: str
    uuid: str

    @classmethod
    def load(cls) -> "Credentials | None":
        env_token = os.environ.get("PKU_TREEHOLE_TOKEN", "").strip()
        env_uuid = os.environ.get("PKU_TREEHOLE_UUID", "").strip()
        if env_token:
            return cls(env_token, env_uuid or make_uuid())
        path = session_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(str(data["token"]), str(data["uuid"]))
        except (OSError, KeyError, ValueError, TypeError):
            return None

    def save(self) -> Path:
        path = session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return path


def make_uuid() -> str:
    # 当前网页跳转到 IAAA 时使用 UUID 的末 12 位；同一值也可用于 API 请求头。
    return uuid_lib.uuid4().hex[-12:]


def _with_query(url: str, **params: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query.update({key: [value] for key, value in params.items()})
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _find_treehole_token(response: requests.Response) -> str | None:
    # Do not inspect history item URLs: the callback request itself contains the
    # short-lived IAAA token. Only the final URL and redirect destinations can
    # contain the Treehole bearer token we want to persist.
    candidate_urls = [response.url]
    candidate_urls.extend(
        item.headers["Location"]
        for item in reversed(response.history)
        if item.headers.get("Location")
    )
    for url in candidate_urls:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        query.update(parse_qs(parsed.fragment))
        for key in ("token", "access_token", "pku_token"):
            if query.get(key):
                return query[key][0]
    for key in ("pku_token", "token"):
        if response.cookies.get(key):
            return response.cookies.get(key)
    try:
        data = response.json()
    except ValueError:
        data = {}
    for key in ("token", "access_token", "pku_token"):
        if isinstance(data, dict) and data.get(key):
            return str(data[key])
        nested = data.get("data", {}) if isinstance(data, dict) else {}
        if isinstance(nested, dict) and nested.get(key):
            return str(nested[key])
    match = re.search(r'(?:token|access_token)["\'=:\s]+([A-Za-z0-9._-]+)', response.text)
    return match.group(1) if match else None


def _encrypt_password(session: requests.Session, password: str) -> str:
    response = session.get("https://iaaa.pku.edu.cn/iaaa/getPublicKey.do", timeout=20)
    response.raise_for_status()
    try:
        data = response.json()
        key_text = data["key"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("无法获取 IAAA 登录公钥。") from exc
    try:
        public_key = serialization.load_pem_public_key(str(key_text).encode("utf-8"))
        encrypted = public_key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("IAAA 公钥格式无法识别。") from exc
    return base64.b64encode(encrypted).decode("ascii")


def login_with_iaaa(username: str | None = None, password: str | None = None) -> Credentials:
    """Authenticate directly against PKU IAAA without storing the password."""
    username = username or input("北大学号：").strip()
    password = password or getpass.getpass("IAAA 密码（输入时不会显示）：")
    client_uuid = make_uuid()
    callback = _with_query(TREEHOLE_CALLBACK, version="3", uuid=client_uuid, plat="web")
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/135 Safari/537.36"
    )
    encrypted_password = _encrypt_password(session, password)
    auth = session.post(
        IAAA_LOGIN_URL,
        data={
            "appid": "PKU Helper",
            "userName": username,
            "password": encrypted_password,
            "randCode": "",
            "smsCode": "",
            "otpCode": "",
            "remTrustChk": "false",
            "redirUrl": callback,
        },
        timeout=30,
    )
    auth.raise_for_status()
    try:
        auth_data = auth.json()
    except ValueError as exc:
        raise RuntimeError("IAAA 返回了无法识别的登录响应。") from exc
    iaaa_token = auth_data.get("token") if isinstance(auth_data, dict) else None
    if not iaaa_token:
        message = auth_data.get("msg") or auth_data.get("message") if isinstance(auth_data, dict) else None
        raise RuntimeError(f"IAAA 登录失败：{message or '请检查账号、密码或二次验证要求'}")
    callback_response = session.get(_with_query(callback, token=str(iaaa_token)), timeout=30)
    callback_response.raise_for_status()
    token = (
        _find_treehole_token(callback_response)
        or session.cookies.get("pku_token")
        or session.cookies.get("token")
    )
    if not token:
        raise RuntimeError(
            "IAAA 已通过，但没有识别到树洞令牌；网站登录流程可能已更新。"
            "请改用 `pku-digest login --token`，或联系维护者更新登录适配。"
        )
    return Credentials(token=token, uuid=client_uuid)


def prompt_for_token() -> Credentials:
    token = getpass.getpass("树洞 Bearer token（输入时不会显示）：").strip()
    if not token:
        raise RuntimeError("token 不能为空")
    return Credentials(token=token, uuid=make_uuid())


def save_login_secret(username: str, password: str) -> None:
    """Save IAAA credentials in the current user's Windows Credential Manager."""
    keyring.set_password(KEYRING_SERVICE, "iaaa_username", username)
    keyring.set_password(KEYRING_SERVICE, "iaaa_password", password)


def load_login_secret() -> tuple[str, str] | None:
    username = keyring.get_password(KEYRING_SERVICE, "iaaa_username")
    password = keyring.get_password(KEYRING_SERVICE, "iaaa_password")
    if username and password:
        return username, password
    return None


def delete_login_secret() -> None:
    for key in ("iaaa_username", "iaaa_password"):
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass


def save_deepseek_key(api_key: str) -> None:
    keyring.set_password(KEYRING_SERVICE, "deepseek_api_key", api_key)


def load_deepseek_key() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, "deepseek_api_key")
