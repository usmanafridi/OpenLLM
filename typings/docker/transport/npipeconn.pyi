"""This type stub file was generated by pyright."""

import urllib3
import urllib3.connection
from docker.transport.basehttpadapter import BaseHTTPAdapter

RecentlyUsedContainer = ...

class NpipeHTTPConnection(urllib3.connection.HTTPConnection):
    def __init__(self, npipe_path, timeout=...) -> None: ...
    def connect(self): ...

class NpipeHTTPConnectionPool(urllib3.connectionpool.HTTPConnectionPool):
    def __init__(self, npipe_path, timeout=..., maxsize=...) -> None: ...

class NpipeHTTPAdapter(BaseHTTPAdapter):
    __attrs__ = ...
    def __init__(self, base_url, timeout=..., pool_connections=..., max_pool_size=...) -> None: ...
    def get_connection(self, url, proxies=...): ...
    def request_url(self, request, proxies): ...
