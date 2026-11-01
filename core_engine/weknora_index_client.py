"""固定版本索引客户端；只调用受控 HTTP，不直接访问 WeKnora 数据库。"""

import os
import time
from urllib.parse import urlsplit

import httpx

from biz_rules.history_models import HistoryError, canonical, sha256


def publication_id(manifest):
    # weknora-kdd-v1 的公开身份算法；摘要不参与ID，但异摘要必须由服务拒绝。
    return sha256(canonical([
        manifest["knowledge_base_id"], manifest["source_instance"],
        manifest["source_id"], manifest["source_record_id"], manifest["version_id"],
    ]))


class IndexServiceError(HistoryError):
    pass


class WeKnoraIndexClient:
    def __init__(self, *, client=None):
        base = os.environ.get("WEKNORA_KDD_URL", "").rstrip("/")
        token = os.environ.get("WEKNORA_KDD_TOKEN", "")
        parts = urlsplit(base)
        if not base or not token or parts.scheme not in {"http", "https"} or parts.username or parts.password:
            raise IndexServiceError("index_not_configured", "索引服务地址或凭据尚未配置", 503)
        self.base, self.token = base, token
        self.client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
        self.owned = client is None

    def close(self):
        if self.owned:
            self.client.close()

    def _request(self, method, path, body=None, *, allow_missing=False):
        deadline = time.monotonic() + 20
        try:
            with self.client.stream(
                method, self.base + "/api/v1/kdd" + path,
                headers={"Authorization": "Bearer " + self.token, "Accept-Encoding": "identity"},
                json=body, timeout=httpx.Timeout(5),
            ) as response:
                if time.monotonic() >= deadline:
                    raise httpx.ReadTimeout("deadline")
                if response.status_code == 404 and allow_missing:
                    return None
                if not 200 <= response.status_code < 300:
                    raise IndexServiceError(
                        "index_http_" + str(response.status_code),
                        "索引服务未确认本次操作；保留待办并对账", response.status_code,
                    )
                if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                    raise IndexServiceError("index_response_encoding", "索引回执编码不受支持", 502)
                data = bytearray()
                for chunk in response.iter_bytes():
                    # 每次底层读取最多5秒；持续慢流也不能无限续租。
                    if time.monotonic() >= deadline:
                        raise httpx.ReadTimeout("deadline")
                    data.extend(chunk)
                    if len(data) > 4 * 1024 * 1024:
                        raise IndexServiceError("index_response_budget", "索引回执超过读取预算", 502)
            import json
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except httpx.TimeoutException:
            raise IndexServiceError("index_timeout", "索引请求超时，结果待核对", 504) from None
        except httpx.HTTPError:
            raise IndexServiceError("index_unavailable", "索引连接不可用，结果待核对", 503) from None
        except (ValueError, TypeError):
            raise IndexServiceError("index_invalid_receipt", "索引回执不符合约定", 502) from None

    def status(self, pid):
        return self._request("GET", "/publications/" + pid, allow_missing=True)

    def publish(self, body):
        return self._request("POST", "/publications", body)

    def disable(self, pid, reason):
        return self._request(
            "POST", "/publications/" + pid + "/disable",
            {"reason": "Fabric current state: " + reason, "state": reason},
        )
