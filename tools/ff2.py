#!/usr/bin/env python3
"""Fantastic Fortune 2 Triple Star (SLPS-25396) localization tools.

Original ISO remains immutable. All offsets are verified against source bytes.
Scenario layout and font lookup were recovered from the supplied executable.
"""
import argparse
import collections
import gzip
import io
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / 'work/original'
CATALOG = ROOT / 'translation/catalog.jsonl'
SECTOR = 2048
SCENARIOS = {
    'MXXX.BIN': (0xed7a0, 2752, 0xf9428, 339),
    'QXXX.BIN': (0xf0680, 2752, 0x10f2c8, 339),
    'OXXX.BIN': (0xf3560, 2792, 0x125168, 329),
    'ADXX.BIN': (0xf6440, 47, 0x13b008, 35),
}
STRING_PATTERN = re.compile(rb'(?:(?:[\x81-\x9f\xe0-\xee][\x40-\x7e\x80-\xfc])|[\x20-\x7e\x01\x0a\x0c]){2,}\x00')
FORMAT_PATTERN = re.compile(r'%(?:\d+\$)?[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]*[diuoxXfFeEgGcs%]')



# Stock draw routine 0x196e30 advances 21 px normally / 42 px after 0x01,
# and 24 / 48 px vertically (0x196e6c, 0x196e74, 0x196f30).
# Native 640x480 dialogue starts at x=140; 22 cells end at x=602,
# inside the frame at x~614. Two-byte spaces advance 10 / 21 px through
# the added draw hook; all visible glyphs keep their 21 / 42 px advance.
DIALOGUE_COLUMNS = 22
DIALOGUE_LINE_UNITS = 4
NAME_COLUMNS = 6
GLYPH_PIXELS = 21
SPACE_PIXELS = 10


def token_pixels(token, scale):
    if token in (' ', '\u3000'):
        return SPACE_PIXELS if scale == 1 else GLYPH_PIXELS
    return (NAME_COLUMNS if token == '%s' else 1) * scale * GLYPH_PIXELS


def display_tokens(text):
    return re.findall(FORMAT_PATTERN.pattern + r'|[\s\S]', text)


def layout_lines(text):
    """Yield (columns, height in 24px units) per rendered dialogue line."""
    columns = 0
    scale = 1
    for token in display_tokens(text):
        if token in ('\n', '\f'):
            yield columns / GLYPH_PIXELS, scale
            columns = 0
            if token == '\f':
                scale = 1
        elif token == '\x01':
            scale = 2
        else:
            columns += token_pixels(token, scale)
    if columns:
        yield columns / GLYPH_PIXELS, scale


def validate_layout(text, kind):
    if kind not in ('dialogue', 'choice'):
        return
    if kind == 'choice' and any(c in text for c in '\n\f\x01'):
        raise ValueError('Choice must remain a single normal-size line')
    for page in text.split('\f'):
        lines = list(layout_lines(page))
        if any(width > DIALOGUE_COLUMNS for width, _ in lines):
            raise ValueError('Text exceeds 22 display cells (including names and enlarged text)')
        if kind == 'dialogue' and sum(height for _, height in lines) > DIALOGUE_LINE_UNITS:
            raise ValueError('Dialogue exceeds 96-pixel text height')


def wrap_dialogue(text, width=DIALOGUE_COLUMNS):
    """Prefer word boundaries within four line units; never cut format tokens.

    Dynamic programming allows a rare word split when that is required to keep
    a page inside the box. Content too long even with splits stays flagged for
    manual shortening. Page/large-font controls stay in their original order.
    """
    from functools import lru_cache
    def page_wrap(page, budget):
        tokens = display_tokens(page.replace('\n', ''))
        size = len(tokens)
        scales = [1]
        for token in tokens:
            scales.append(2 if token == '\x01' else scales[-1])
        @lru_cache(None)
        def solve(start, remaining):
            if start == size:
                return 0, []
            if remaining <= 0:
                return None
            used = 0
            best = None
            for stop in range(start + 1, size + 1):
                token = tokens[stop - 1]
                if token != '\x01':
                    used += token_pixels(token, scales[stop])
                if used > width * GLYPH_PIXELS:
                    break
                if token == '\x01':
                    continue
                following = stop
                while following < size and tokens[following] == ' ':
                    following += 1
                tail = solve(following, remaining - scales[stop])
                if tail is None:
                    continue
                split = -.9 if token in '。．！？、，：；' and used >= width * GLYPH_PIXELS / 2 else 0
                if stop < size and token != ' ' and tokens[stop] != ' ':
                    split = -.9 if token in '。．！？、，：；' else (0 if token == '…' else 20)
                    if token in '（「『':
                        split += 100
                if following < size and tokens[following] in '。．！？、，：；）」』':
                    split += 100
                # Last line may be short; other lines should be reasonably full.
                cost = tail[0] + split + 1 + (0 if following == size else (width-used/GLYPH_PIXELS)**2 / 100)
                if best is None or cost < best[0]:
                    best = cost, [''.join(tokens[start:stop]).rstrip(' ')] + tail[1]
            return best
        found = solve(0, budget)
        return '\n'.join(found[1]) if found else None
    result = []
    for page in text.split('\f'):
        wrapped = page_wrap(page, DIALOGUE_LINE_UNITS)
        if wrapped is None:
            # Do not delete words or invent extra pages when translation is too
            # long. Renderable over-height draft is reported for manual review.
            wrapped = page_wrap(page, max(4, len(page) * 2))
        result.append(wrapped if wrapped is not None else page)
    return '\f'.join(result)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def iso_files(iso):
    with Path(iso).open('rb') as f:
        f.seek(16 * SECTOR)
        pvd = f.read(SECTOR)
        if pvd[:7] != b'\x01CD001\x01':
            raise ValueError('Expected ISO9660 primary volume descriptor')
        root = pvd[156:190]
        f.seek(struct.unpack_from('<I', root, 2)[0] * SECTOR)
        data = f.read(struct.unpack_from('<I', root, 10)[0])
    entries = {}
    pos = 0
    while pos < len(data):
        length = data[pos]
        if not length:
            pos = (pos // SECTOR + 1) * SECTOR
            continue
        rec = data[pos:pos + length]
        pos += length
        if rec[25] & 2:
            continue
        name = rec[33:33 + rec[32]].decode('ascii').split(';')[0]
        entries[name] = {'lba': struct.unpack_from('<I', rec, 2)[0],
                         'size': struct.unpack_from('<I', rec, 10)[0]}
    required = set(SCENARIOS) | {'FONT.GF', 'SLPS_253.96'}
    if not required <= entries.keys():
        raise ValueError('This image is not the supported SLPS-25396 disc')
    return entries


def font_map(elf, original=True):
    # ELF PT_LOAD: file offset 0x80, virtual address 0x100000.
    delta = 0xfff80
    lead_hi = struct.unpack_from('<I',elf,0x1973e0-delta)[0]&0xffff
    lead_lo = struct.unpack_from('<h',elf,0x1973e8-delta)[0]
    lead_offset = virtual_offset(elf,(lead_hi<<16)+lead_lo)
    lead_limit = struct.unpack_from('<I',elf,0x197320-delta)[0]&0xffff
    row_count = max(48,lead_limit-0xc0)
    rows = [struct.unpack_from('<hh', elf, lead_offset+i*4) for i in range(row_count)]
    table_hi = struct.unpack_from('<I', elf, 0x19740c - delta)[0] & 0xffff
    table_lo = struct.unpack_from('<h', elf, 0x197414 - delta)[0]
    table_address = (table_hi << 16) + table_lo
    table_offset = virtual_offset(elf, table_address)
    result = {}
    for lead in list(range(0x81, 0xa0)) + list(range(0xe0, lead_limit)):
        group, base = rows[lead - (0x80 if lead < 0xe0 else 0xc0)]
        if group < 0 or base < 0:
            continue
        for trail in range(0x40, 0xfd):
            index = elf[table_offset + group * 189 + trail - 0x40]
            if index != 255:
                result[bytes([lead, trail]).hex()] = base + index
    if original and (len(result) != 3476 or max(result.values()) != 3475):
        raise ValueError('Unexpected original font lookup table')
    return result


def virtual_offset(elf, address):
    phoff = struct.unpack_from('<I', elf, 28)[0]
    for n in range(struct.unpack_from('<H', elf, 44)[0]):
        kind, off, va, pa, size, memsize, flags, alignment = struct.unpack_from('<8I', elf, phoff + n * 32)
        if kind == 1 and va <= address < va + size:
            return off + address - va
    raise ValueError('Address is not in a loaded ELF file segment: %x' % address)


def source_entry(name, offset, raw, capacity, **context):
    try:
        text = raw.decode('cp932')
    except UnicodeDecodeError:
        return None
    if not text or not any('\u3040' <= c <= '\u9fff' for c in text):
        return None
    return dict(id='%s:%08x' % (name, offset), file=name, offset=offset,
                capacity=capacity, source=text, source_hex=raw.hex(), target='', **context)


def extract(iso):
    files = iso_files(iso)
    ORIGINAL.mkdir(parents=True, exist_ok=True)
    selected = set(SCENARIOS) | {'FONT.GF', 'SLPS_253.96', 'SYSTEM.CNF'}
    # Keep small UI/character assets for follow-up analysis; no audio/video copy.
    selected |= {name for name, info in files.items() if name.endswith('.BIN') and info['size'] < 12000000}
    with Path(iso).open('rb') as f:
        for name in sorted(selected):
            info = files[name]
            f.seek(info['lba'] * SECTOR)
            data = f.read(info['size'])
            if len(data) != info['size']:
                raise ValueError('Truncated ISO file: ' + name)
            (ORIGINAL / name).write_bytes(data)
    elf = (ORIGINAL / 'SLPS_253.96').read_bytes()
    catalog = []
    stats = {}
    for name, (toc_off, toc_count, event_off, event_count) in SCENARIOS.items():
        data = (ORIGINAL / name).read_bytes()
        toc = struct.unpack_from('<%dI' % toc_count, elf, toc_off)
        if toc[0] != 0 or toc[-1] * SECTOR != len(data):
            raise ValueError('Scenario extent table mismatch: ' + name)
        events = collections.defaultdict(list)
        for i in range(event_count):
            off = event_off + i * 264
            event = elf[off:off + 8].rstrip(b'\0').decode('ascii')
            for branch, resource in enumerate(struct.unpack_from('<64I', elf, off + 8)):
                if resource:
                    events[resource].append('%s/%02d' % (event, branch))
        before = len(catalog)
        records = 0
        for resource in range(1, len(toc) - 1):
            start, end = toc[resource] * SECTOR, toc[resource + 1] * SECTOR
            count = struct.unpack_from('<I', data, start)[0]
            if (start + 180 + count * 244 + SECTOR - 1) // SECTOR * SECTOR != end:
                raise ValueError('Scenario record layout mismatch: %s/%d' % (name, resource))
            records += count
            context = dict(resource=resource, events=events[resource])
            for choice in range(4):
                off = start + 4 + choice * 44
                field = data[off:off + 44]
                raw = field.split(b'\0')[0]
                capacity = 44 if not any(field[len(raw):]) else len(raw) + 1
                entry = source_entry(name, off, raw, capacity, kind='choice', record=choice, **context)
                if entry:
                    catalog.append(entry)
            for record in range(count):
                rec = start + 180 + record * 244
                off = rec + 52
                field = data[off:rec + 244]
                raw = field.split(b'\0')[0]
                if len(raw) == len(field):
                    if raw:
                        raise ValueError('Unterminated scenario text')
                    continue
                capacity = 192 if not any(field[len(raw):]) else len(raw) + 1
                entry = source_entry(name, off, raw, capacity, kind='dialogue', record=record,
                                     command=data[rec:rec + 52].hex(), **context)
                if entry:
                    catalog.append(entry)
        stats[name] = {'resources': len(toc) - 2, 'records': records, 'strings': len(catalog) - before}
    # ELF string discovery remains a candidate list, with manual translation review.
    for match in STRING_PATTERN.finditer(elf):
        raw = match[0][:-1]
        try:
            text = raw.decode('cp932')
        except UnicodeDecodeError:
            continue
        kana = sum('\u3040' <= c <= '\u30ff' for c in text)
        if kana < 3 or kana < len(text) * 0.12:
            continue
        entry = source_entry('SLPS_253.96', match.start(), raw, len(raw) + 1, kind='ui_candidate')
        if entry:
            catalog.append(entry)
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    with CATALOG.open('w', encoding='utf-8') as f:
        for entry in catalog:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    manifest = {'iso_name': Path(iso).name, 'iso_size': Path(iso).stat().st_size,
                'iso_sha256': sha256(iso), 'files': files,
                'extracted_sha256': {name: sha256(ORIGINAL / name) for name in sorted(selected)},
                'statistics': stats, 'catalog_entries': len(catalog)}
    write_json(ROOT / 'work/manifest.json', manifest)
    write_json(ROOT / 'work/analysis/font_map.json', font_map(elf))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print('Catalog entries:', len(catalog))


def codes(raw):
    i = 0
    while i < len(raw):
        lead = raw[i]
        if 0x81 <= lead <= 0x9f or 0xe0 <= lead <= 0xfc:
            if i + 1 < len(raw):
                yield raw[i:i + 2].hex()
            i += 2
        else:
            i += 1


def validate_text(source, target):
    if '\0' in target:
        raise ValueError('Translation contains an embedded NUL')
    if FORMAT_PATTERN.findall(source) != FORMAT_PATTERN.findall(target):
        raise ValueError('Format placeholders must retain their order and spelling')
    # Newline wrapping may change; event/page controls must remain in order.
    controls = lambda s: [c for c in s if ord(c) < 32 and c != '\n']
    if controls(source) != controls(target):
        raise ValueError('Page/variable control characters changed')


def encode_text(text, mapping):
    substitutions = {' ': '\u3000', '.': '。', ',': '、', '?': '？', '!': '！',
                     '(': '（', ')': '）', ':': '：', '-': '－'}
    result = bytearray()
    i = 0
    while i < len(text):
        c = text[i]
        placeholder = FORMAT_PATTERN.match(text, i)
        if placeholder:
            result.extend(placeholder[0].encode('ascii'))
            i = placeholder.end()
            continue
        if c in mapping:
            result.extend(bytes.fromhex(mapping[c]['code']))
        elif ord(c) < 32:
            result.append(ord(c))
        else:
            result.extend(substitutions.get(c, c).encode('cp932'))
        i += 1
    return bytes(result)


HANGUL_BASELINE = 17
HANGUL_PEN_X = 1
HANGUL_FONT_SIZE = 19


def make_glyph(character, font_path):
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    # Match the original cell: 32x32, low nibble is the left pixel.
    # Indices 0..7 blend the center color into the bright outline; 8..14
    # are its antialias alpha ramp. 15 stays transparent. The stock game
    # draws a separate navy pass, so no extra shadow offset is introduced.
    # Original Japanese ink occupies x=0..19, y=0..21 inside the 32x32
    # texture cell. Use a shared font baseline, not per-character ink bottoms.
    # Centering/bottom-aligning glyph bounds moves short syllables like 노/로
    # down independently and breaks normal Korean text alignment.
    font = ImageFont.truetype(str(font_path), HANGUL_FONT_SIZE)
    mask = Image.new('L', (32, 32), 0)
    draw = ImageDraw.Draw(mask)
    box = draw.textbbox((HANGUL_PEN_X, HANGUL_BASELINE), character, font=font, anchor='ls')
    width, height = box[2] - box[0], box[3] - box[1]
    if not (0 < width <= 18 and 0 < height <= 20 and
            box[0] >= 1 and box[1] >= 1 and box[2] <= 19 and box[3] <= 21):
        raise ValueError('Font glyph does not fit: ' + character)
    draw.text((HANGUL_PEN_X, HANGUL_BASELINE), character, font=font, fill=255, anchor='ls')
    outline = mask.filter(ImageFilter.MaxFilter(3))
    pixels = []
    for ink, edge in zip(mask.getdata(), outline.getdata()):
        if ink >= 24:
            pixels.append(min(7, round((255 - ink) * 7 / 255)))
        elif edge >= 24:
            pixels.append(8 + min(6, round((255 - edge) * 6 / 255)))
        else:
            pixels.append(15)
    return bytes(pixels[i] | (pixels[i + 1] << 4) for i in range(0, 1024, 2))


def style_dialogue_palettes(elf):
    """Keep stock color identities and navy pass; add bright glyph borders."""
    result = bytearray(elf)
    changes = []
    for address, center in [(0x3442c0, (52, 79, 111)),
                            (0x344380, (112, 224, 224)),
                            (0x3443c0, (224, 112, 224))]:
        off = virtual_offset(elf, address)
        original = elf[off:off + 64]
        expected_color = (224,224,224) if address == 0x3442c0 else center
        expected = bytes(v for i in range(15) for v in (*expected_color,128-i*8)) + bytes(4)
        if original != expected:
            raise ValueError('Unexpected stock dialogue palette: %x' % address)
        colors = []
        for i in range(8):
            colors.append(tuple(round(c + (224-c)*i/7) for c in center) + (128,))
        colors += [(224,224,224,128-i*16) for i in range(7)]
        colors.append((0,0,0,0))
        styled = bytes(v for color in colors for v in color)
        result[off:off+64] = styled
        changes.append(dict(address=address,offset=off,before=original.hex(),after=styled.hex()))
    return result, changes


def stable_mapping(characters, free, existing=None):
    """Preserve assigned codes: saved character names contain these bytes."""
    mapping={c:dict(v) for c,v in (existing or {}).items()}
    valid=dict(free)
    if any(len(c)!=1 or not '\uac00'<=c<='\ud7a3' or valid.get(v['code'])!=v['glyph'] for c,v in mapping.items()):
        raise ValueError('Invalid stable Hangul registry')
    used={v['code'] for v in mapping.values()}
    if len(used)!=len(mapping):raise ValueError('Duplicate stable font codes')
    available=iter((code,glyph) for code,glyph in free if code not in used)
    for c in sorted(set(characters)-mapping.keys()):
        try:code,glyph=next(available)
        except StopIteration:raise ValueError('Hangul registry exceeds selected bank')
        mapping[c]=dict(code=code,glyph=glyph)
    return mapping

def build(translations, font_path, output, extended_font=False, full_font=False, assets=None):
    manifest = json.loads((ROOT / 'work/manifest.json').read_text())
    catalog = {e['id']: e for e in read_jsonl(CATALOG)}
    edits = read_jsonl(translations)
    active = [e for e in edits if e.get('target')]
    if len({e['id'] for e in active}) != len(active):
        raise ValueError('Duplicate translation IDs')
    # Expose 364 blank tail cells through two previously disabled lead bytes.
    # This preserves every existing Japanese glyph and its lookup entry.
    original_elf = (ORIGINAL / 'SLPS_253.96').read_bytes()
    lookup = font_map(original_elf)
    font_data = (ORIGINAL / 'FONT.GF').read_bytes()
    if len(font_data) != 3840 * 512 or any(b != 255 for b in font_data[3476 * 512:]):
        raise ValueError('Expected 364 empty font cells')
    delta = 0xfff80
    table_off = 0x3444c0 - delta + 24 * 189
    if original_elf[table_off:table_off + 378] != bytes([255]) * 378:
        raise ValueError('Expected two unused font lookup groups')
    free = []
    custom_elf = bytearray(original_elf)
    table = bytearray([255] * 378)
    lead_off = 0x344400 - delta + 4 * 4
    if original_elf[lead_off:lead_off + 8] != bytes([255]) * 8:
        raise ValueError('Font leads 0x84/0x85 are already active')
    custom_elf[lead_off:lead_off + 8] = struct.pack('<4h', 24, 3476, 25, 3664)
    for i in range(364):
        group, index = divmod(i, 188)
        trail = 0x40 + index + (1 if index >= 63 else 0)
        code, glyph = bytes([0x84 + group, trail]).hex(), 3476 + i
        table[group * 189 + trail - 0x40] = index
        free.append((code, glyph))
        lookup[code] = glyph
    custom_elf[table_off:table_off + 378] = table
    extension = None
    iso_hunks = []
    hangul = sorted({c for e in active for c in e['target'] if '\uac00' <= c <= '\ud7a3'})
    registry_path=ROOT/'translation/font_registry.json'
    registry=json.loads(registry_path.read_text()) if full_font and registry_path.exists() else None
    if extended_font or full_font:
        from font_extension import prepare,LEADS,FULL_LEADS
        planned_free=[(bytes([lead,0x40+i+(i>=63)]).hex(),3476+n*188+i)
                      for n,lead in enumerate(FULL_LEADS if full_font else LEADS) for i in range(188)]
        preliminary=stable_mapping(hangul,planned_free,registry['mapping']) if registry else {c:{'code':code,'glyph':glyph} for c,(code,glyph) in zip(hangul,planned_free)}
        ui_strings=[]
        for edit in active:
            entry=catalog[edit['id']]
            if entry['kind']!='ui_candidate':continue
            raw=encode_text(edit['target'],preliminary)
            if len(raw)+1>entry['capacity']:
                ui_strings.append(dict(id=entry['id'],offset=entry['offset'],raw=raw))
        glyph_limit=max(v['glyph'] for v in preliminary.values())+1
        custom_elf, free, extension, iso_hunks = prepare(original_elf, manifest, ROOT / manifest['iso_name'],full_font=full_font,ui_strings=ui_strings,glyph_limit=glyph_limit)
        lookup = font_map(original_elf)
        lookup.update(dict(free))
    custom_elf, palette_changes = style_dialogue_palettes(custom_elf)
    verified_lookup = font_map(custom_elf, original=False)
    if lookup != verified_lookup:
        raise ValueError('Custom font lookup round trip failed')
    if len(hangul) > len(free):
        raise ValueError('Need %d Hangul glyphs; selected bank has %d slots.' % (len(hangul), len(free)))
    mapping = stable_mapping(hangul,free,registry['mapping']) if registry else {c: {'code': code, 'glyph': glyph} for c, (code, glyph) in zip(hangul, free)}
    hunks = []
    data = {}
    def add(name, off, new, label):
        if name not in data:
            if sha256(ORIGINAL / name) != manifest['extracted_sha256'][name]:
                raise ValueError('Extracted original changed: ' + name)
            data[name] = (ORIGINAL / name).read_bytes()
        old = data[name][off:off + len(new)]
        if len(old) != len(new):
            raise ValueError('Patch extends outside source file')
        if old != new:
            hunks.append(dict(file=name, offset=off,
                              iso_offset=manifest['files'][name]['lba'] * SECTOR + off,
                              before=old.hex(), after=new.hex(), label=label))
    def add_iso(off, new, label):
        with (ROOT / manifest['iso_name']).open('rb') as f:
            f.seek(off)
            old = f.read(len(new))
        if len(old) != len(new):
            raise ValueError('Added font data exceeds ISO bounds')
        if old != new:
            hunks.append(dict(file='@ISO', offset=off, iso_offset=off,
                              before=old.hex(), after=new.hex(), label=label))
    if extension:
        start = None
        for i in range(len(original_elf) + 1):
            changed = i < len(original_elf) and original_elf[i] != custom_elf[i]
            if changed and start is None:
                start = i
            elif not changed and start is not None:
                add('SLPS_253.96', start, custom_elf[start:i], 'font:elf-code-and-mapping')
                start = None
        add_iso(manifest['files']['SLPS_253.96']['lba'] * SECTOR + len(original_elf),
                custom_elf[len(original_elf):], 'font:added-elf-segment')
        for off, new, label in iso_hunks:
            add_iso(off, new, label)
        added_font = bytearray([255] * extension['added_font_size'])
    else:
        add('SLPS_253.96', lead_off, custom_elf[lead_off:lead_off + 8], 'font:activate-hangul-leads')
        add('SLPS_253.96', table_off, table, 'font:hangul-lookup')
        for item in palette_changes:
            add('SLPS_253.96',item['offset'],bytes.fromhex(item['after']),'font:bright-outline-palette')
    relocated={e['id']:e for e in extension.get('ui_relocations',[])} if extension else {}
    for edit in active:
        entry = catalog[edit['id']]
        if edit.get('source', entry['source']) != entry['source']:
            raise ValueError('Translation source mismatch: ' + edit['id'])
        validate_text(entry['source'], edit['target'])
        raw = encode_text(edit['target'], mapping)
        if len(raw) + 1 > entry['capacity'] and entry['id'] not in relocated:
            raise ValueError('%s exceeds %d-byte capacity (%d bytes)' % (edit['id'], entry['capacity'], len(raw) + 1))
        for code in codes(raw):
            if code not in lookup:
                raise ValueError('Unmapped font code: ' + code)
        validate_layout(edit['target'], entry['kind'])
        if entry['id'] not in relocated:
            add(entry['file'], entry['offset'], raw + bytes(entry['capacity'] - len(raw)), entry['id'])
    for c, value in mapping.items():
        if extension and value['glyph'] >= 3840:
            off = (value['glyph'] - 3840) * 512
            added_font[off:off + 512] = make_glyph(c, font_path)
        else:
            add('FONT.GF', value['glyph'] * 512, make_glyph(c, font_path), 'glyph:' + c)
    if extension:
        add_iso(extension['added_font_lba'] * SECTOR, added_font, 'font:added-glyph-bank')
    graphics=None
    if assets:
        asset_dir=Path(assets)
        graphics=json.loads((asset_dir/'report.json').read_text())
        from tim2_assets import parse
        inventory=json.loads((ROOT/'work/analysis/textures/export/inventory.json').read_text())
        asset_files={}
        for item in graphics['reports']:
            name=item['file']
            if name not in manifest['files'] or name in ('MXXX.BIN','QXXX.BIN','OXXX.BIN','ADXX.BIN'):
                raise ValueError('Invalid graphics resource')
            if name not in asset_files:
                asset_files[name]=((ORIGINAL/name).read_bytes(),(asset_dir/name).read_bytes())
            original,modified=asset_files[name]
            if len(modified)!=len(original):raise ValueError('Graphics resource size changed')
            info=next(i for i in inventory if i['file']==name and i['number']==item['number'])
            if hashlib.sha256(original[info['offset']:info['offset']+info['total_size']]).hexdigest()!=item['original_sha256']:
                raise ValueError('Graphics source guard mismatch')
            if original[info['offset']:info['image_offset']]!=modified[info['offset']:info['image_offset']] or original[info['clut_offset']:info['offset']+info['total_size']]!=modified[info['clut_offset']:info['offset']+info['total_size']]:
                raise ValueError('Graphics layout or palette changed')
            start=item['image_offset'];size=item['image_size']
            parse(modified,info['offset'])
            # Emit only changed index runs. Whole CG texture payloads need not
            # be embedded in a patch merely to replace a small text plate.
            old_image=original[start:start+size];new_image=modified[start:start+size]
            begin=None;last=-1
            for i,(old,new) in enumerate(zip(old_image,new_image)):
                if old==new:continue
                if begin is not None and i-last>64:
                    add(name,start+begin,new_image[begin:last+1],'graphics:%s:%03d'%(name,item['number']))
                    begin=None
                if begin is None:begin=i
                last=i
            if begin is not None:
                add(name,start+begin,new_image[begin:last+1],'graphics:%s:%03d'%(name,item['number']))
    hunks.sort(key=lambda h: h['iso_offset'])
    for first, second in zip(hunks, hunks[1:]):
        if first['iso_offset'] + len(bytes.fromhex(first['after'])) > second['iso_offset']:
            raise ValueError('Patch hunks overlap')
    patch = dict(format='ff2-ko-patch-v1', source_sha256=manifest['iso_sha256'],
                 source_size=manifest['iso_size'], translated_strings=len(active),
                 hangul_glyphs=len(mapping), free_glyph_slots=len(free), mapping=mapping, hunks=hunks)
    patch['font_style'] = dict(style='thin-colored-center-bright-outline',
                              palette_changes=palette_changes,
                              stock_navy_shadow_palette_preserved=True,
                              stock_shadow_address=0x344340,
                              glyph_size=HANGUL_FONT_SIZE,
                              baseline=HANGUL_BASELINE)
    if extension:
        patch['font_extension'] = extension
    if graphics:
        patch['graphics']=graphics
    if registry:
        patch['font_mapping_stable']=True
        patch['font_registry_base']=registry['base']
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('wb') as raw:
        with gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as packed:
            with io.TextIOWrapper(packed,encoding='utf-8') as f:
                json.dump(patch, f, ensure_ascii=False)
    write_json(output.with_suffix('.report.json'), {k: v for k, v in patch.items() if k != 'hunks'})
    # Rebuild each edited file and check no bytes outside approved spans changed.
    for name, original in data.items():
        modified = bytearray(original)
        for h in hunks:
            if h['file'] == name:
                raw = bytes.fromhex(h['after'])
                modified[h['offset']:h['offset'] + len(raw)] = raw
        if name == 'SLPS_253.96' and extension:
            modified.extend(custom_elf[len(original_elf):])
        dest = ROOT / 'work/patched' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(modified)
    if extension:
        (ROOT / 'work/patched/HANGUL.GF').write_bytes(added_font)
    write_json(ROOT / 'work/current-build.json',dict(patch=str(output.resolve()),translations=str(Path(translations).resolve())))
    if registry:
        registry['mapping']=mapping
        write_json(registry_path,registry)
    print('Built %s: %d strings, %d Hangul glyphs, %d available slots (%d remaining)' % (output, len(active), len(mapping), len(free), len(free) - len(mapping)))


def load_patch(path):
    with gzip.open(str(path), 'rt', encoding='utf-8') as f:
        patch = json.load(f)
    if patch['format'] != 'ff2-ko-patch-v1':
        raise ValueError('Unsupported patch format')
    last_end = 0
    for h in patch['hunks']:
        before, after = bytes.fromhex(h['before']), bytes.fromhex(h['after'])
        off = h['iso_offset']
        if len(before) != len(after) or off < last_end or off + len(after) > patch['source_size']:
            raise ValueError('Invalid/overlapping patch span')
        last_end = off + len(after)
    return patch


def export_event(event, output):
    entries = [e for e in read_jsonl(CATALOG)
               if any(context.split('/')[0] == event for context in e.get('events', []))]
    if not entries:
        raise ValueError('Unknown/empty event: ' + event)
    path = Path(output)
    if path.exists():
        raise ValueError('Refusing to overwrite existing translation file: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in entries), encoding='utf-8')
    print('%s: exported %d strings to %s' % (event, len(entries), path))


def apply_patch(iso, patch_path, output):
    iso, output = Path(iso), Path(output)
    if iso.resolve() == output.resolve() or output.exists():
        raise ValueError('Output must be a new file; original ISO cannot be overwritten')
    patch = load_patch(patch_path)
    if iso.stat().st_size != patch['source_size'] or sha256(iso) != patch['source_sha256']:
        raise ValueError('Original ISO SHA-256 mismatch')
    # Verify every source span before allocating an output image.
    with iso.open('rb') as f:
        for h in patch['hunks']:
            f.seek(h['iso_offset'])
            before = bytes.fromhex(h['before'])
            if f.read(len(before)) != before:
                raise ValueError('Source hunk mismatch: ' + h['label'])
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + '.building')
    if temp.exists():
        raise ValueError('Temporary output exists: ' + str(temp))
    try:
        # APFS copy-on-write clone; fall back to an ordinary local copy.
        result = subprocess.run(['cp', '-c', str(iso), str(temp)], capture_output=True)
        if result.returncode:
            if temp.exists():
                temp.unlink()
            shutil.copyfile(str(iso), str(temp))
        with temp.open('r+b') as f:
            for h in patch['hunks']:
                f.seek(h['iso_offset'])
                f.write(bytes.fromhex(h['after']))
        verify(iso, patch_path, temp)
        temp.rename(output)
    except Exception:
        if temp.exists():
            temp.unlink()
        raise
    print('Created:', output)


def verify(iso, patch_path, output):
    patch = load_patch(patch_path)
    if sha256(iso) != patch['source_sha256']:
        raise ValueError('Original ISO SHA-256 mismatch')
    if Path(output).stat().st_size != patch['source_size']:
        raise ValueError('Patched ISO size changed')
    with Path(iso).open('rb') as a, Path(output).open('rb') as b:
        pos = 0
        for h in patch['hunks'] + [{'iso_offset': patch['source_size'], 'after': '', 'before': ''}]:
            end = h['iso_offset']
            while pos < end:
                n = min(4 * 1024 * 1024, end - pos)
                if a.read(n) != b.read(n):
                    raise ValueError('Unexpected change outside patch at 0x%x' % pos)
                pos += n
            after = bytes.fromhex(h['after'])
            before = bytes.fromhex(h['before'])
            if a.read(len(before)) != before or b.read(len(after)) != after:
                raise ValueError('Patch bytes mismatch at 0x%x' % pos)
            pos += len(after)
    print('Verified: same ISO length, every intended edit present, every other byte unchanged')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    extract_parser = sub.add_parser('extract')
    extract_parser.add_argument('iso')
    export_parser = sub.add_parser('export-event')
    export_parser.add_argument('event')
    export_parser.add_argument('output')
    build_parser = sub.add_parser('build')
    build_parser.add_argument('translations')
    build_parser.add_argument('--font', default='/Library/Fonts/NanumGothic.ttf')
    build_parser.add_argument('--output', default=str(ROOT / 'output/ff2-ko-alpha.ff2patch.gz'))
    build_parser.add_argument('--assets',help='compiled Korean TIM2 font-cell resource directory')
    build_parser.add_argument('--extended-font', action='store_true')
    build_parser.add_argument('--full-font', action='store_true',help='4136 Hangul slots with expanded lead decoding')
    for verb in ['apply', 'verify']:
        p = sub.add_parser(verb)
        p.add_argument('iso')
        p.add_argument('patch')
        p.add_argument('output')
    args = parser.parse_args()
    if args.command == 'extract':
        extract(args.iso)
    elif args.command == 'export-event':
        export_event(args.event, args.output)
    elif args.command == 'build':
        build(args.translations, args.font, args.output, args.extended_font,args.full_font,args.assets)
    elif args.command == 'apply':
        apply_patch(args.iso, args.patch, args.output)
    else:
        verify(args.iso, args.patch, args.output)


if __name__ == '__main__':
    main()
