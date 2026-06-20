#!/usr/bin/env python3

import argparse
import gzip
import os
import re
import sys
import time
from datetime import datetime
from xml.sax.saxutils import escape
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

CHANNEL_LIST_URL = (
    "https://jiotv.data.cdn.jio.com/apis/v3.0/getMobileChannelList/get/"
    "?os=android&devicetype=phone&usertype=tvYR7NSNn7rymo3F"
)

EPG_URLS = [
    "https://jiotv.data.cdn.jio.com/apis/v1.3/getepg/get/?offset={offset}&channel_id={channel_id}",
    "https://jiotv.data.cdn.jio.com/apis/v2.0/getepg/get/?offset={offset}&channel_id={channel_id}",
    "https://jiotv.data.cdn.jio.com/apis/v1.3/getepg/get?offset={offset}&channel_id={channel_id}",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://www.jio.com",
    "Referer": "https://www.jio.com/",
}

LOGO_BASE = "https://jiotv.catchup.cdn.jio.com/dare_images/shows"

_PROXY_URL = os.environ.get("JIOTV_PROXY", "").strip()
PROXIES = {"http": _PROXY_URL, "https": _PROXY_URL} if _PROXY_URL else None


def clean(v):
    if v is None:
        return ""
    return escape(str(v).strip())


def mask_error(e):
    msg = str(e)
    if _PROXY_URL:
        msg = msg.replace(_PROXY_URL, "[PROXY_HIDDEN]")
    return re.sub(r"://[^/@\s]+@", "://[REDACTED]@", msg)


def get_first(d, keys, default=""):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def extract_list(data):
    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    keys = [
        "epg", "EPG", "result", "results", "data",
        "programs", "programmes", "programList",
        "show", "shows", "list"
    ]

    for k in keys:
        val = data.get(k)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            nested = extract_list(val)
            if nested:
                return nested

    for val in data.values():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
        if isinstance(val, dict):
            nested = extract_list(val)
            if nested:
                return nested

    return []


def fetch_json(url, timeout=15):
    r = requests.get(url, headers=HEADERS, proxies=PROXIES, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_channels(lang_filter=None):
    try:
        data = fetch_json(CHANNEL_LIST_URL)
    except Exception as e:
        print(f"[X] Channel list fetch fail: {mask_error(e)}")
        sys.exit(1)

    channels = extract_list(data)

    if lang_filter:
        lf = lang_filter.lower()
        channels = [
            c for c in channels
            if lf in str(c.get("channel_language", "")).lower()
            or lf in str(c.get("lang", "")).lower()
            or lf in str(c.get("language", "")).lower()
        ]

    print(f"[+] Channels: {len(channels)}")
    return channels


def fmt_time(value):
    if value in (None, ""):
        return None

    try:
        if isinstance(value, str):
            value = value.strip()

            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ",
                "%d-%m-%Y %H:%M:%S",
            ):
                try:
                    dt = datetime.strptime(value.replace("+00:00", ""), fmt)
                    return dt.strftime("%Y%m%d%H%M%S") + " +0000"
                except Exception:
                    pass

        ts = int(float(value))

        if ts > 10**12:
            ts = ts / 1000

        dt = datetime.utcfromtimestamp(ts)
        return dt.strftime("%Y%m%d%H%M%S") + " +0000"

    except Exception:
        return None


def fetch_epg_for_channel(channel_id, days=2, delay=0.1, debug=False):
    all_programmes = []

    for offset in range(days):
        got = False

        for template in EPG_URLS:
            url = template.format(offset=offset, channel_id=channel_id)

            try:
                data = fetch_json(url, timeout=12)

                if debug and offset == 0:
                    print(f"[DEBUG] channel={channel_id} keys={list(data.keys()) if isinstance(data, dict) else type(data)}")

                epg_list = extract_list(data)

                if epg_list:
                    all_programmes.extend(epg_list)
                    got = True
                    break

            except Exception as e:
                if debug:
                    print(f"[DEBUG] channel={channel_id} error={mask_error(e)}")

        time.sleep(delay)

    return all_programmes


def normalize_programme(p):
    start = get_first(p, [
        "startEpoch", "start_epoch", "startTime", "start_time",
        "start", "showtime", "showTime", "begin", "beginTime"
    ])

    stop = get_first(p, [
        "endEpoch", "end_epoch", "endTime", "end_time",
        "end", "stoptime", "stopTime", "finish", "finishTime"
    ])

    title = get_first(p, [
        "showname", "showName", "name", "title",
        "programname", "programName", "programmeName",
        "episode_name", "episodeName"
    ], "No Title")

    desc = get_first(p, [
        "description", "desc", "shortDescription",
        "longDescription", "synopsis", "show_desc"
    ])

    subtitle = get_first(p, [
        "episodeTitle", "episode_title", "episodename",
        "episodeName", "subtitle", "subTitle"
    ])

    category = get_first(p, [
        "genre", "category", "showCategory", "programCategory"
    ])

    date = get_first(p, [
        "releaseDate", "release_date", "date", "year"
    ])

    cast_name = get_first(p, [
        "cast", "actors", "actor", "starcast", "starCast"
    ])

    director = get_first(p, [
        "director", "directors"
    ])

    return {
        "start": fmt_time(start),
        "stop": fmt_time(stop),
        "title": title,
        "desc": desc,
        "subtitle": subtitle,
        "category": category,
        "date": date,
        "cast": cast_name,
        "director": director,
    }


def build_xmltv(channels, epg_by_channel, output_path):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        '<tv generator-info-name="jiotv-epg-script" generator-info-url="https://www.jio.com">'
    ]

    for ch in channels:
        ch_id = get_first(ch, ["channel_id", "channelId", "id"])
        if not ch_id:
            continue

        ch_name = get_first(ch, ["channel_name", "channelName", "name"], f"Channel {ch_id}")
        logo = get_first(ch, ["logoUrl", "channelLogoUrl", "logo", "icon"])

        lines.append(f'  <channel id="{clean(ch_id)}">')
        lines.append(f'    <display-name>{clean(ch_name)}</display-name>')

        if logo:
            logo = str(logo)
            logo_url = logo if logo.startswith("http") else f"{LOGO_BASE}{logo}"
            lines.append(f'    <icon src="{clean(logo_url)}" />')

        lines.append("  </channel>")

    programme_count = 0

    for ch_id, programmes in epg_by_channel.items():
        for raw in programmes:
            if not isinstance(raw, dict):
                continue

            p = normalize_programme(raw)

            if not p["start"] or not p["stop"]:
                continue

            lines.append(f'  <programme start="{p["start"]}" stop="{p["stop"]}" channel="{clean(ch_id)}">')
            lines.append(f'    <title lang="en">{clean(p["title"])}</title>')

            if p["subtitle"]:
                lines.append(f'    <sub-title lang="en">{clean(p["subtitle"])}</sub-title>')

            if p["desc"]:
                lines.append(f'    <desc lang="en">{clean(p["desc"])}</desc>')

            if p["date"]:
                lines.append(f'    <date>{clean(p["date"])}</date>')

            if p["director"] or p["cast"]:
                lines.append("    <credits>")
                if p["director"]:
                    lines.append(f'      <director>{clean(p["director"])}</director>')
                if p["cast"]:
                    lines.append(f'      <actor>{clean(p["cast"])}</actor>')
                lines.append("    </credits>")

            if p["category"]:
                lines.append(f'    <category lang="en">{clean(p["category"])}</category>')

            lines.append("  </programme>")
            programme_count += 1

    lines.append("</tv>")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[+] XMLTV ready: {output_path}")
    print(f"[+] Programmes written: {programme_count}")

    return programme_count


def main():
    parser = argparse.ArgumentParser(description="JioTV EPG XMLTV generator")
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--output", type=str, default="epg/jiotv_epg.xml")
    parser.add_argument("--lang", type=str, default=None)
    parser.add_argument("--gzip", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    print("=" * 50)
    print("JioTV EPG -> XMLTV Generator")
    print("=" * 50)
    print(f"[i] Proxy: {'ENABLED' if PROXIES else 'disabled'}")

    channels = fetch_channels(args.lang)

    if args.limit:
        channels = channels[:args.limit]

    epg_by_channel = {}
    total = len(channels)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}

        for ch in channels:
            ch_id = get_first(ch, ["channel_id", "channelId", "id"])
            if ch_id:
                futures[executor.submit(fetch_epg_for_channel, ch_id, args.days, 0.1, args.debug)] = ch_id

        for i, future in enumerate(as_completed(futures), 1):
            ch_id = futures[future]

            try:
                epg_by_channel[ch_id] = future.result()
            except Exception as e:
                print(f"[!] EPG failed channel={ch_id}: {mask_error(e)}")
                epg_by_channel[ch_id] = []

            if i % 25 == 0 or i == len(futures):
                print(f"[{i}/{len(futures)}] channels processed")

    count = build_xmltv(channels, epg_by_channel, args.output)

    if args.gzip:
        gz_path = args.output + ".gz"
        with open(args.output, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            f_out.writelines(f_in)
        print(f"[+] Gzip ready: {gz_path}")

    if count == 0:
        print("[!] WARNING: Programme data 0. API response format/auth issue irukkalam.")
        print("[i] Test command: python3 scripts/jiotv_epg.py --limit 1 --debug")

    print("Done")


if __name__ == "__main__":
    main()
