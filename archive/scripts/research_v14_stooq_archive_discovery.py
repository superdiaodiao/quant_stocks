#!/usr/bin/env python3
"""Resolve Stooq's browser challenge and discover current official archives."""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re

import requests


BASE_URL = "https://stooq.com/db/h/"


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag.lower() != "a":
            return
        values = dict(attrs)
        if values.get("href"):
            self.hrefs.append(values["href"])


def solve_pow(token: str, difficulty: int) -> int:
    prefix = "0" * difficulty
    nonce = 0
    while not hashlib.sha256(f"{token}{nonce}".encode()).hexdigest().startswith(prefix):
        nonce += 1
    return nonce


def archive_links(page: str) -> list[str]:
    parser = _Links()
    parser.feed(page)
    quoted = re.findall(r'''["']([^"']+\.zip(?:\?[^"']*)?)["']''', page)
    return sorted({
        href for href in [*parser.hrefs, *quoted]
        if ".zip" in href.lower() or "db/d/?b=" in href.lower()
    })


def discover(session: requests.Session | None = None) -> tuple[list[str], requests.Session]:
    session = session or requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/127.0 Safari/537.36"
        )
    })
    response = session.get(BASE_URL, timeout=30)
    response.raise_for_status()
    match = re.search(r'const c="([^"]+)",d=(\d+)', response.text)
    if match:
        token, difficulty = match.group(1), int(match.group(2))
        verified = session.post(
            "https://stooq.com/__verify",
            data={"c": token, "n": solve_pow(token, difficulty)},
            timeout=30,
        )
        verified.raise_for_status()
        response = session.get(BASE_URL, timeout=30)
        response.raise_for_status()
    links = archive_links(response.text)
    if not links:
        parser = _Links()
        parser.feed(response.text)
        snippet = re.sub(r"\s+", " ", response.text[:500])
        raise RuntimeError(
            "Stooq archive page contains no ZIP links after verification: "
            + snippet + " hrefs=" + repr(parser.hrefs[:50])
        )
    return links, session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument(
        "--output", type=Path,
        default=Path("output/research_only/v14/stooq/d_us_txt.zip"),
    )
    args = parser.parse_args()
    links, session = discover()
    candidates = [link for link in links if "d_us_txt" in link.lower()]
    if not candidates:
        print("\n".join(links))
        raise RuntimeError("No US daily text archive found")
    selected = candidates[-1]
    if not args.download:
        print(selected)
        return
    url = requests.compat.urljoin("https://stooq.com/", selected)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    digest = hashlib.sha256()
    size = 0
    with session.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with temporary.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    temporary.replace(args.output)
    print(f"url={url}")
    print(f"path={args.output}")
    print(f"bytes={size}")
    print(f"sha256={digest.hexdigest()}")


if __name__ == "__main__":
    main()
