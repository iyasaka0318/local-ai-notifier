"""Fetch public web pages as plain text for the model to read."""

import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
from charset_normalizer import from_bytes

from automation_config import MAX_PAGE_BYTES, MAX_PAGE_TEXT_CHARS


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)

    def text(self):
        return "\n".join(self.parts)


def validate_public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("unsupported URL")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("local URL is not allowed")
    for info in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError("non-public URL is not allowed")


def decode_response_body(response, raw):
    content_type = response.headers.get("content-type", "").lower()
    if "charset=" in content_type and response.encoding:
        encoding = response.encoding
    else:
        match = from_bytes(raw).best()
        encoding = match.encoding if match is not None else "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def fetch_page_text(url):
    current = url
    for _ in range(4):
        validate_public_url(current)
        response = requests.get(
            current,
            headers={"User-Agent": "LocalAIResearch/1.0"},
            timeout=20,
            allow_redirects=False,
            stream=True,
        )
        if 300 <= response.status_code < 400 and response.headers.get("location"):
            current = urljoin(current, response.headers["location"])
            continue
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type and "text/plain" not in content_type:
            raise ValueError("unsupported content type")
        chunks = []
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_PAGE_BYTES:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        decoded = decode_response_body(response, raw)
        if "text/html" in content_type:
            parser = TextExtractor()
            parser.feed(decoded)
            decoded = parser.text()
        return current, decoded[:MAX_PAGE_TEXT_CHARS]
    raise ValueError("too many redirects")
