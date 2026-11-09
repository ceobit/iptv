#!/usr/bin/env python3
"""Build a measured IPTV selection using public playlists and FFmpeg.

Python standard library only. Requires ffprobe and ffmpeg on PATH.
No VPN, account, DRM bypass, or software installation is performed.
"""
import argparse
import concurrent.futures
import datetime as dt
import json
import pathlib
import re
import subprocess
import urllib.request
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent
SOURCES = {
    'iptv-org': 'https://iptv-org.github.io/iptv/countries/ru.m3u',
    'ngrch': 'https://ngrch.github.io/iptv/ru.m3u',
    'iptv-russia': 'https://substanc1.github.io/iptv-russia/streams/ru.m3u',
    'maximkiselev': 'https://raw.githubusercontent.com/MaximKiselev/iptv/main/playlist.m3u',
    'loganettv': 'https://raw.githubusercontent.com/loganettv/playlists/main/mega.m3u',
    'tsavkisi55': 'https://raw.githubusercontent.com/tsavkisi55/iptv-/main/myiptv.m3u',
    'boxer180': 'https://raw.githubusercontent.com/boxer180/RUSSIA_TV/main/IPTV.m3u',
    'nurlan135': 'https://gist.githubusercontent.com/nurlan135/2abf1b34706f1fe6786c230652c808a0/raw',
    'ixxxygrek': 'https://gist.githubusercontent.com/ixxxygrek/64f18ad2cc7ad003f13d52b0554c2fef/raw',
}
# Display name, iptv-org identifier, ngrch identifier.
CHANNELS = [
    ('Россия 1', 'Russia1', 'rossia1'), ('НТВ', 'NTV', 'ntv'),
    ('Первый канал', 'ChannelOne', 'pervy'), ('РЕН ТВ', 'RENTV', 'rentv'),
    ('Пятый канал', 'Channel5', '5kanal-ru'), ('Домашний', 'Domashniy', 'domashny'),
    ('СТС', 'STS', 'sts'), ('ТНТ', 'TNT', 'tnt'), ('ТВ Центр', 'TVCentr', 'tvcentr'),
    ('Карусель', 'Karusel', 'karusel'), ('Звезда', 'Zvezda', 'zvezda'),
    ('ТВ-3', 'TV3', 'tv3'), ('Пятница!', 'Friday', 'piatnica'),
    ('МИР', 'Mir', 'mir'), ('Матч ТВ', 'MatchTV', 'match-tv'),
    ('Россия 24', 'Russia24', 'rossia-24'), ('Россия Культура', 'RussiaK', 'kultura'),
    ('ОТР', 'OTR', 'otr'), ('МУЗ-ТВ', 'MuzTV', 'muztv'),
    ('Солнце', 'Solnce', 'solnce'), ('Мосфильм. Золотая коллекция', 'MosfilmGoldCollection', 'mosfilm'),
    ('Русский роман', 'Russkiyroman', 'russky-roman'), ('Дом кино', 'Domkino', 'domkino'),
    ('Победа', 'Pobeda', 'pobeda'), ('Русский детектив', 'RusskiyDetektiv', 'russky-detektiv'),
    ('Мульт', 'Mult', 'mult'), ('ТНТ4', 'TNT4', 'tnt4'),
    ('Суббота!', 'Subbota', 'super'), ('Ю', 'U', 'yu'), ('Кино ТВ', 'KinoTV', ''),
    ('Мультиландия', 'Multilandia', ''), ('В гостях у сказки', 'Vgostyakhuskazki', ''),
    ('ЧЕ!', 'Che', 'che'),
]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_playlist(text, source):
    entry = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF:'):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
            title = re.split(r',(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)', line, maxsplit=1)[-1]
            entry = dict(source=source, original_name=title.strip(),
                         tvg_id=attrs.get('tvg-id', ''), logo=attrs.get('tvg-logo', ''),
                         ua=attrs.get('http-user-agent', ''), referrer=attrs.get('http-referrer', ''))
        elif entry and line.startswith('#EXTVLCOPT:'):
            key, _, value = line.partition(':')[2].partition('=')
            if key == 'http-user-agent':
                entry['ua'] = value
            if key == 'http-referrer':
                entry['referrer'] = value
        elif entry and line.startswith(('http://', 'https://')):
            entry['url'] = line
            yield entry
            entry = None


def match(entry):
    ident, _, variant = entry['tvg_id'].partition('@')
    # Do not silently replace the requested feed with +2/+7 or international editions.
    if variant and variant not in ('SD', 'HD'):
        return None
    name = re.sub(r'\s*\(\d+\)$', '', entry['original_name']).strip().casefold()
    name = re.sub(r'\s+hd$', '', name)
    if name.rstrip('!') == 'che':
        return 'ЧЕ!'
    for display, org_id, ngrch_id in CHANNELS:
        if ident.casefold() == (org_id + '.ru').casefold() or (ngrch_id and ident == ngrch_id):
            return display
        if name.rstrip('!') == display.casefold().rstrip('!') or (display == 'Мосфильм. Золотая коллекция' and name == 'мосфильм золотая коллекция'):
            return display
    return None


def input_args(entry):
    args = ['-rw_timeout', '10000000']
    if entry.get('ua'):
        args += ['-user_agent', entry['ua']]
    if entry.get('referrer'):
        args += ['-headers', 'Referer: ' + entry['referrer'] + '\r\n']
    return args


def run(args, timeout):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr[-4000:]
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b''
        err = exc.stderr or b''
        if isinstance(out, bytes):
            out = out.decode(errors='replace')
        if isinstance(err, bytes):
            err = err.decode(errors='replace')
        return -1, out, 'wall-clock timeout\n' + err[-4000:]


def probe(entry):
    entry = dict(entry, checked_at=now())
    reject_placeholder(entry)
    if entry.get('rejection'):
        return entry
    code, out, err = run(['ffprobe', '-v', 'error', *input_args(entry),
                          '-analyzeduration', '3000000', '-probesize', '4000000',
                          '-show_streams', '-show_format', '-of', 'json', entry['url']], 24)
    try:
        data = json.loads(out)
    except ValueError:
        data = {}
    videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video' and s.get('width', 0)]
    audios = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
    entry.update(probe_ok=code == 0 and bool(videos) and bool(audios), error=err,
                 video=videos, audio=audios)
    # Select the largest stream actually reported, not the resolution in its name.
    if entry['probe_ok']:
        v = max(videos, key=lambda s: s.get('width', 0) * s.get('height', 0))
        entry.update(width=v['width'], height=v['height'], video_index=v['index'],
                     fps=v.get('avg_frame_rate'), codec=v.get('codec_name'))
        reject_placeholder(entry)
    return entry


def reject_placeholder(entry):
    # User confirmed persistent CINERAMA advertising instead of the channel.
    # Decoding and resolution checks cannot distinguish it from a live feed.
    parsed = urllib.parse.urlsplit(entry['url'])
    host = parsed.hostname or ''
    if host == 'cinerama.uz' or host.endswith('.cinerama.uz'):
        entry.update(probe_ok=False, rejection='CINERAMA advertising card reported on TV; source excluded')
    if host == '31.148.5.241' and parsed.path.startswith('/kanal_6/'):
        entry.update(probe_ok=False, rejection='Russia K test pattern, visually confirmed; feed excluded')
    # Visually confirmed on this network: Wink's territorial blocking card.
    if entry.get('width') == 1980 and entry.get('height') == 1920:
        entry.update(probe_ok=False, rejection='Wink territorial blocking card, visually confirmed')


def resolve_best(entry):
    """Pin the highest declared HLS rendition, then measure that rendition."""
    headers = {}
    if entry.get('ua'):
        headers['User-Agent'] = entry['ua']
    if entry.get('referrer'):
        headers['Referer'] = entry['referrer']
    try:
        with urllib.request.urlopen(urllib.request.Request(entry['url'], headers=headers), timeout=12) as r:
            body = r.read(500000).decode('utf-8-sig')
            base = r.url
        variants, pending = [], None
        for line in body.splitlines():
            if line.startswith('#EXT-X-STREAM-INF:'):
                size = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
                bitrate = re.search(r'(?:^|,)BANDWIDTH=(\d+)', line.partition(':')[2])
                pending = (int(size[1]) * int(size[2]) if size else 0,
                           int(bitrate[1]) if bitrate else 0)
            elif pending is not None and line and not line.startswith('#'):
                variants.append((*pending, urllib.parse.urljoin(base, line.strip())))
                pending = None
        if variants:
            best = max(variants)
            resolved = probe(dict(entry, url=best[2], master_url=entry['url'], declared_bandwidth=best[1]))
            resolved['fallback_urls'] = [item[2] for item in sorted(variants, reverse=True) if item != best]
            return resolved
    except Exception:
        pass
    return entry


def decode(entry, seconds):
    code, out, err = run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                          *input_args(entry), '-i', entry['url'], '-map', '0:' + str(entry['video_index']),
                          '-map', '0:a:0', '-t', str(seconds), '-progress', 'pipe:1',
                          '-f', 'null', '-'], seconds + 35)
    progress = dict(re.findall(r'^(\w+)=(.*)$', out, re.M))
    duration = int(progress.get('out_time_us', '0')) / 1e6
    frames = int(progress.get('frame', '0'))
    return dict(ok=code == 0 and duration >= seconds - 1 and frames >= seconds * 10,
                seconds=duration, frames=frames, error=err, checked_at=now())


def verify_channel(item, seconds, stability_seconds=0):
    name, entries = item
    entries = sorted(entries, key=lambda e: (e['width'] * e['height'], float_fraction(e.get('fps'))), reverse=True)
    fallbacks = []
    for entry in entries:
        entry = resolve_best(entry)
        for url in entry.get('fallback_urls', []):
            fallback = dict(entry, url=url)
            fallback.pop('fallback_urls', None)
            fallbacks.append(fallback)
        if not entry['probe_ok']:
            continue
        entry['decode_checks'] = [decode(entry, seconds)]
        if entry['decode_checks'][0]['ok']:
            entry['decode_checks'].append(decode(entry, seconds))
            if entry['decode_checks'][1]['ok']:
                if stability_seconds:
                    entry['stability'] = decode(entry, stability_seconds)
                    if not entry['stability']['ok']:
                        continue
                snap = ROOT / 'output' / 'frames' / (str(next(i for i,c in enumerate(CHANNELS,1) if c[0] == name)).zfill(2) + '.jpg')
                snap.parent.mkdir(exist_ok=True)
                code, _, _ = run(['ffmpeg', '-y', '-v', 'error', '-nostdin', *input_args(entry),
                                   '-i', entry['url'], '-map', '0:' + str(entry['video_index']),
                                   '-frames:v', '1', '-vf', 'scale=480:-1', str(snap)], 25)
                entry['snapshot'] = str(snap.relative_to(ROOT)) if code == 0 else None
                print('PASS', name, str(entry['width']) + 'x' + str(entry['height']), flush=True)
                return entry
    if fallbacks:
        measured = [probe(e) for e in fallbacks]
        usable = [e for e in measured if e['probe_ok']]
        if usable:
            return verify_channel((name, usable), seconds, stability_seconds)
    print('FAIL', name, flush=True)
    return None


def float_fraction(value):
    try:
        a, b = value.split('/')
        return float(a) / float(b)
    except (ValueError, ZeroDivisionError, AttributeError):
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--seconds', type=int, default=15)
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--stability-seconds', type=int, default=60)
    ap.add_argument('--reuse-probes', action='store_true')
    args = ap.parse_args()
    (ROOT / 'sources').mkdir(exist_ok=True)
    (ROOT / 'output').mkdir(exist_ok=True)
    entries = []
    for name, url in SOURCES.items():
        path = ROOT / 'sources' / (name + '.m3u')
        if args.refresh or not path.exists():
            try:
                path.write_bytes(urllib.request.urlopen(url, timeout=30).read())
            except Exception as exc:
                print('Source unavailable:', name, str(exc), flush=True)
                if not path.exists():
                    continue
        entries.extend(parse_playlist(path.read_text(encoding='utf-8-sig'), name))
    unique = {}
    for entry in entries:
        name = match(entry)
        if name:
            entry['channel'] = name
            key = (name, entry['url'], entry['ua'], entry['referrer'])
            unique.setdefault(key, entry)
    candidates = list(unique.values())
    print('Candidates:', len(candidates), flush=True)
    probe_file = ROOT / 'output' / 'probes.json'
    if args.reuse_probes:
        probes = json.loads(probe_file.read_text())
    else:
        with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
            probes = []
            for i, entry in enumerate(pool.map(probe, candidates), 1):
                probes.append(entry)
                print('PROBE', i, '/', len(candidates), entry['channel'], entry['probe_ok'], flush=True)
        probe_file.write_text(json.dumps(probes, ensure_ascii=False, indent=2))
    for entry in probes:
        reject_placeholder(entry)
    grouped = {name: [e for e in probes if e['channel'] == name and e['probe_ok']] for name, _, _ in CHANNELS}
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        selected = [e for e in pool.map(lambda item: verify_channel(item, args.seconds, args.stability_seconds), grouped.items()) if e]
    for entry in selected:
        entry.update(group=channel_group(entry['channel']), logo=channel_logo(entry['channel']))
    report = dict(checked_at=now(), sources=SOURCES, decode_seconds_per_check=args.seconds,
                  stability_seconds=args.stability_seconds, manual_identity_review_required=True,
                  selected=selected, missing=[n for n in grouped if n not in {e['channel'] for e in selected}],
                  candidates=probes)
    (ROOT / 'output' / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    write_playlist(selected, ROOT / 'output' / 'iptv-germany.m3u')
    print('Selected:', len(selected), '/ 33; missing:', ', '.join(report['missing']), flush=True)


CHANNEL_GROUPS = {
    'Общие': ('Россия 1', 'НТВ', 'Первый канал', 'РЕН ТВ', 'Пятый канал', 'ТВ Центр', 'МИР', 'ОТР'),
    'Развлекательные': ('Домашний', 'СТС', 'ТНТ', 'ТВ-3', 'Пятница!', 'ТНТ4', 'Суббота!', 'Ю', 'ЧЕ!'),
    'Детские': ('Карусель', 'Солнце', 'Мульт', 'Мультиландия', 'В гостях у сказки'),
    'Фильмы и сериалы': ('Мосфильм. Золотая коллекция', 'Русский роман', 'Дом кино', 'Победа', 'Русский детектив', 'Кино ТВ'),
    'Новости': ('Россия 24',),
    'Познавательные и культура': ('Россия Культура', 'Звезда'),
    'Музыка': ('МУЗ-ТВ',),
    'Спорт': ('Матч ТВ',),
}


def channel_group(name):
    return next((group for group, channels in CHANNEL_GROUPS.items() if name in channels), 'Общие')


LOGO_SLUGS = {name: slug for name, _, slug in CHANNELS if slug}
LOGO_SLUGS.update({
    'Суббота!': 'subbota',
    'Кино ТВ': 'kino-tv',
    'Мультиландия': 'multimania',
    'В гостях у сказки': 'v-gostiax-u-skazki',
})


def channel_logo(name):
    slug = LOGO_SLUGS.get(name)
    return 'https://iptvx.one/picons/' + slug + '.png' if slug else ''


def write_playlist(entries, path):
    lines = ['#EXTM3U']
    for entry in entries:
        logo = channel_logo(entry['channel']) or entry.get('logo', '')
        logo_attr = ' tvg-logo="' + logo + '"' if logo else ''
        lines.append('#EXTINF:-1 tvg-id="' + entry['tvg_id'] + '"' + logo_attr + ' group-title="' + channel_group(entry['channel']) + '",' + entry['channel'])
        if entry.get('ua'):
            lines.append('#EXTVLCOPT:http-user-agent=' + entry['ua'])
        if entry.get('referrer'):
            lines.append('#EXTVLCOPT:http-referrer=' + entry['referrer'])
        lines.append(entry.get('playback_url', entry['url']))
    path.write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
