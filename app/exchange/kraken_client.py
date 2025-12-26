import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Any, Dict, Optional

import requests


class KrakenClient:
    """
    Kraken Spot REST API client (public + private).
    Private endpoints require:
      - API-Key header (public key)
      - API-Sign header (signature)
      - nonce field (monotonically increasing)
    See Kraken docs. :contentReference[oaicite:3]{index=3}
    """

    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api.kraken.com", timeout: int = 30):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _nonce(self) -> str:
        # millisecond nonce; must be increasing per key
        return str(int(time.time() * 1000))

    def _sign(self, url_path: str, data: Dict[str, Any]) -> str:
        # Per Kraken algorithm: API-Sign = base64( HMAC-SHA512( base64_decode(secret), url_path + SHA256(nonce + postdata) ) )
        # postdata is URL-encoded form data.
        postdata = urllib.parse.urlencode(data)
        encoded = (data["nonce"] + postdata).encode()
        message = url_path.encode() + hashlib.sha256(encoded).digest()
        secret = base64.b64decode(self.api_secret)
        sig = hmac.new(secret, message, hashlib.sha512).digest()
        return base64.b64encode(sig).decode()

    def public(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url_path = f"/0/public/{method}"
        url = self.base_url + url_path
        r = self.session.get(url, params=params or {}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def private(self, method: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url_path = f"/0/private/{method}"
        url = self.base_url + url_path
        payload = dict(data or {})
        payload["nonce"] = self._nonce()

        headers = {
            "API-Key": self.api_key,
            "API-Sign": self._sign(url_path, payload),
        }

        r = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()
