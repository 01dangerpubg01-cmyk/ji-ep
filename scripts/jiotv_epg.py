#!/usr/bin/env python3
"""
JioTV EPG -> XMLTV (.xml) Generator
------------------------------------
Idhu JioTV oda public CDN endpoints use pannitu ella channels oda
EPG (Electronic Program Guide) data eduthu, standard XMLTV format-la
oru .xml file generate pannum.

XMLTV file IPTV players (Tivimate, IPTV Smarters, Jellyfin, Kodi, etc.)
la guide data kaata use pannalam.

Usage:
    python3 jiotv_epg.py                      # default: 2 days, all channels
    python3 jiotv_epg.py --days 3
    python3 jiotv_epg.py --output my_epg.xml
    python3 jiotv_epg.py --lang Tamil          # specific language channels mattum

NOTE: Idhu purely public/unauthenticated JioTV CDN endpoints mattum use pannudhu
(channel list + EPG). Login/OTP venam. Educational/personal use ku mattum.
"""

import argparse
import os
import sys
import time
import gzip
from datetime import datetime, timedelta
from xml.sax.saxutils import escape
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    print("requests library illa. Install pannunga: pip install requests --break-system-packages")
    sys.exit(1)

CHANNEL_LIST_URL = (
    "https://jiotv.data.cdn.jio.com/apis/v3.0/getMobileChannelList/get/"
    "?os=android&devicetype=phone&usertype=tvYR7NSNn7rymo3F"
)
EPG_URL_TEMPLATE = (
    "https://jiotv.data.cdn.jio.com/apis/v1.3/getepg/get/?offset={offset}&channel_id={channel_id}"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/100.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json",
}

LOGO_BASE = "https://jiotv.catchup.cdn.jio.com/dare_images/shows"

# Proxy support - JIOTV_PROXY env var la irundhu eduthukum.
# Format: http://user:pass@host:port  (GitHub Secrets la store pannunga, plain text-la engayum podadhinga)
_PROXY_URL = os.environ.get("JIOTV_PROXY", "").strip()
PROXIES = {"http": _PROXY_URL, "https": _PROXY_URL} if _PROXY_URL else None


def _sanitize_error(exc):
    """Error message-la proxy URL irundha (credentials sahitham), adha mask pannidu.
    Idhu GitHub Actions logs la proxy username/password accidental-a print aagama
    thadukkardhukku."""
    msg = str(exc)
    if _PROXY_URL and _PROXY_URL in msg:
        msg = msg.replace(_PROXY_URL, "[PROXY_HIDDEN]")
    # "user:pass@" pattern irundha edhu vandhalum mask pannidu (safety net)
    import re
    msg = re.sub(r"://[^/@\s]+@", "://[REDACTED]@", msg)
    return msg


def fetch_channels(lang_filter=None, retries=3):
    """JioTV CDN la irundhu full channel list eduthuko."""
    for attempt in range(retries):
        try:
            resp = requests.get(CHANNEL_LIST_URL, headers=HEADERS, proxies=PROXIES, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            print(f"[!] Channel list fetch fail (attempt {attempt+1}/{retries}): {_sanitize_error(e)}")
            time.sleep(2)
    else:
        print("[X] Channel list eduthukka mudila. Network/CDN issue irukalam.")
        sys.exit(1)

    channels = data.get("result", [])
    if lang_filter:
        channels = [c for c in channels if lang_filter.lower() in str(c.get("channel_language", "")).lower()
                    or lang_filter.lower() in str(c.get("lang", "")).lower()]

    print(f"[+] {len(channels)} channels kedachuchu" + (f" (filter: {lang_filter})" if lang_filter else ""))
    return channels


def fetch_epg_for_channel(channel_id, days=2, delay=0.15):
    """Oru channel ku 'days' count of offsets ku EPG data eduthuko."""
    all_programmes = []
    for offset in range(days):
        url = EPG_URL_TEMPLATE.format(offset=offset, channel_id=channel_id)
        try:
            resp = requests.get(url, headers=HEADERS, proxies=PROXIES, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            epg_list = data.get("epg", [])
            all_programmes.extend(epg_list)
        except Exception:
            # Proxy/connection errors silent-a skip pannrom - credentials accidental-a
            # print aagama irukka. Eppadiyo retry loop handle pannum.
            pass
        time.sleep(delay)  # JioTV CDN ku rate-limit aagama irukka konjam delay
    return all_programmes


def fmt_xmltv_time(epoch_ms_or_s):
    """JioTV epoch (seconds nu nenaikalam) -> XMLTV time format (YYYYMMDDHHMMSS +0000)."""
    try:
        ts = int(epoch_ms_or_s)
        # JioTV epg often gives seconds, but sometimes ms - sanity check
        if ts > 10**12:
            ts = ts / 1000
        dt = datetime.utcfromtimestamp(ts)
        return dt.strftime("%Y%m%d%H%M%S") + " +0000"
    except Exception:
        return None


def build_xmltv(channels, epg_by_channel, output_path):
    """Ella data vum sernthu XMLTV .xml file ezhudhu."""
    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<!DOCTYPE tv SYSTEM "xmltv.dtd">')
    lines.append('<tv generator-info-name="jiotv-epg-script" generator-info-url="https://www.jio.com">')

    # --- <channel> tags ---
    for ch in channels:
        ch_id = ch.get("channel_id")
        ch_name = escape(str(ch.get("channel_name", f"Channel {ch_id}")))
        logo_id = ch.get("logoUrl") or ch.get("channelLogoUrl") or ""
        lines.append(f'  <channel id="{ch_id}">')
        lines.append(f'    <display-name>{ch_name}</display-name>')
        if logo_id:
            logo_url = logo_id if str(logo_id).startswith("http") else f"{LOGO_BASE}{logo_id}"
            lines.append(f'    <icon src="{escape(logo_url)}" />')
        lines.append('  </channel>')

    # --- <programme> tags ---
    for ch_id, programmes in epg_by_channel.items():
        for p in programmes:
            start = fmt_xmltv_time(p.get("startEpoch") or p.get("start_time") or p.get("startTime"))
            stop = fmt_xmltv_time(p.get("endEpoch") or p.get("end_time") or p.get("endTime"))
            title = escape(str(p.get("showname") or p.get("title") or "No Title"))
            desc = escape(str(p.get("description") or p.get("desc") or ""))
            category = p.get("genre") or p.get("category") or ""

            if not start or not stop:
                continue

            lines.append(f'  <programme start="{start}" stop="{stop}" channel="{ch_id}">')
            lines.append(f'    <title lang="en">{title}</title>')
            if desc:
                lines.append(f'    <desc lang="en">{desc}</desc>')

            # Extra details (date, name, episode info if available)
            episode_title = escape(str(p.get("episodeTitle") or p.get("episodename") or p.get("subtitle") or ""))
            if episode_title:
                lines.append(f'    <sub-title lang="en">{episode_title}</sub-title>')

            prog_date = p.get("releaseDate") or p.get("date") or p.get("year")
            if prog_date:
                lines.append(f'    <date>{escape(str(prog_date))}</date>')

            cast_name = p.get("cast") or p.get("actors") or p.get("actor")
            director = p.get("director")
            if cast_name or director:
                lines.append('    <credits>')
                if director:
                    lines.append(f'      <director>{escape(str(director))}</director>')
                if cast_name:
                    lines.append(f'      <actor>{escape(str(cast_name))}</actor>')
                lines.append('    </credits>')

            if category:
                lines.append(f'    <category lang="en">{escape(str(category))}</category>')

            lines.append('  </programme>')

    lines.append('</tv>')

    xml_content = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    print(f"[+] XMLTV file ready: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="JioTV EPG -> XMLTV generator")
    parser.add_argument("--days", type=int, default=2, help="Eththana naal EPG venum (default: 2)")
    parser.add_argument("--output", type=str, default="jiotv_epg.xml", help="Output .xml file name")
    parser.add_argument("--lang", type=str, default=None, help="Specific language channels mattum (e.g. Tamil)")
    parser.add_argument("--gzip", action="store_true", help="Output ah .xml.gz ah compress pannu")
    parser.add_argument("--limit", type=int, default=None, help="Testing ku - first N channels mattum process pannu")
    parser.add_argument("--workers", type=int, default=8, help="Parallel threads count (default: 8)")
    args = parser.parse_args()

    print("=" * 50)
    print("JioTV EPG -> XMLTV Generator")
    print("=" * 50)
    print(f"[i] Proxy: {'ENABLED (hidden)' if PROXIES else 'disabled'}")

    channels = fetch_channels(lang_filter=args.lang)
    if args.limit:
        channels = channels[: args.limit]

    epg_by_channel = {}
    total = len(channels)
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_ch = {
            executor.submit(fetch_epg_for_channel, ch.get("channel_id"), args.days): ch
            for ch in channels
        }
        for future in as_completed(future_to_ch):
            ch = future_to_ch[future]
            ch_id = ch.get("channel_id")
            try:
                epg_by_channel[ch_id] = future.result()
            except Exception as e:
                print(f"[!] EPG fetch failed for {ch.get('channel_name')} (id={ch_id}): {e}")
                epg_by_channel[ch_id] = []
            completed += 1
            if completed % 25 == 0 or completed == total:
                print(f"[{completed}/{total}] channels processed...")

    output_path = build_xmltv(channels, epg_by_channel, args.output)

    if args.gzip:
        gz_path = output_path + ".gz"
        with open(output_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            f_out.writelines(f_in)
        print(f"[+] Gzipped version: {gz_path}")

    print("\nMudinjuduchu! 🎉")


if __name__ == "__main__":
    main()
