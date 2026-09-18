#!/usr/bin/env python3
"""Скачать PDF раздела KOREM «Результаты торгов ТЭМ» с 2022 года.

URL: https://www.korem.kz/ru/news/rezultaty-torgov-tem
По умолчанию скачивает только один файл. Для всей коллекции: --all.
Использует только стандартную библиотеку Python.
"""

import argparse
import datetime as dt
import html.parser
import mimetypes
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_URL = "https://www.korem.kz/ru/news/rezultaty-torgov-tem"
DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(20\d{2})\b")
FILE_RE = re.compile(r"/file/download/(\d+)(?:\?.*)?$")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KOREM archive downloader/1.0)"}


class FileLinks(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        if FILE_RE.search(urllib.parse.urlparse(href).path):
            self.current = [href, []]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            href, parts = self.current
            self.links.append((href, " ".join("".join(parts).split())))
            self.current = None


def discover(year):
    url = f"{BASE_URL}?year={year}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as response:
        parser = FileLinks()
        parser.feed(response.read().decode("utf-8", errors="replace"))

    found = []
    for href, title in parser.links:
        match = DATE_RE.search(title)
        if not match:
            print(f"Пропущена ссылка без даты: {title} ({href})", file=sys.stderr)
            continue
        day, month, item_year = map(int, match.groups())
        try:
            date = dt.date(item_year, month, day)
        except ValueError:
            print(f"Пропущена ссылка с неверной датой: {title}", file=sys.stderr)
            continue
        if date.year != year:
            continue
        url = urllib.parse.urljoin(BASE_URL, href)
        file_id = FILE_RE.search(urllib.parse.urlparse(url).path).group(1)
        found.append((date, file_id, url, title))
    return found


def download(item, destination):
    date, file_id, url, title = item
    folder = destination / str(date.year) / f"{date.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=60) as response:
        content_type = response.headers.get_content_type()
        if content_type == "text/html":
            raise ValueError(f"Вместо файла сервер вернул HTML: {url}")
        extension = mimetypes.guess_extension(content_type) or ".bin"
        if extension == ".jpe":
            extension = ".jpg"
        target = folder / f"{date.isoformat()}_{file_id}{extension}"
        if target.exists():
            print(f"Уже есть: {target}")
            return False
        temporary = target.with_name(target.name + ".part")
        try:
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Скачано: {target} — {title}")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="скачать все найденные файлы")
    parser.add_argument("--limit", type=int, default=1, help="максимум новых файлов (по умолчанию: 1)")
    parser.add_argument("--to", type=Path, default=Path(__file__).resolve().parent / "korem_files", help="папка для файлов")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit должен быть больше нуля")

    items = []
    for year in range(2022, dt.date.today().year + 1):
        yearly = discover(year)
        print(f"{year}: найдено {len(yearly)} файлов")
        items.extend(yearly)

    items = sorted({item[2]: item for item in items}.values(), key=lambda item: (item[0], int(item[1])))
    count = 0
    for item in items:
        if download(item, args.to):
            count += 1
            if not args.all and count >= args.limit:
                break
    print(f"Готово. Новых файлов: {count}; всего найдено: {len(items)}")


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        sys.exit(1)
