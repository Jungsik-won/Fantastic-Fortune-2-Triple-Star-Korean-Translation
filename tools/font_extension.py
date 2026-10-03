"""Reserve a second font bank in a new ELF PT_LOAD segment.

Original Japanese glyph data and mappings stay intact. A small R5900 hook
selects the original bank below cell 3840 and the added bank above it. The
new ELF segment reserves its font RAM before SetupHeap initializes the heap.
All disc additions occupy verified zero padding after the last original file.
"""
import struct

DELTA = 0xfff80
LEADS = [0x84, 0x85, 0x86, 0x87, 0xeb, 0xec, 0xed, 0xee]
FULL_LEADS = [0x84, 0x85, 0x86, 0x87] + list(range(0xeb, 0xfd))
ORIGINAL_BANK = 0x4befc0
ORIGINAL_CELLS = 3840
JAPANESE_CELLS = 3476
CAPACITY = len(LEADS) * 188
EXTRA_CELLS = JAPANESE_CELLS + CAPACITY - ORIGINAL_CELLS


def align(value, boundary):
    return (value + boundary - 1) // boundary * boundary


def lui(register, value):
    return 0x3c000000 | register << 16 | value


def addiu(target, source, value):
    return 0x24000000 | source << 21 | target << 16 | (value & 0xffff)


def address(register, value):
    # addiu sign-extends its low immediate.
    return [lui(register, (value + 0x8000) >> 16), addiu(register, register, value)]


def jump(target, link=False):
    return (0x0c000000 if link else 0x08000000) | ((target >> 2) & 0x03ffffff)


def words(values):
    return struct.pack('<%dI' % len(values), *values)


def root_records(data):
    pos = 0
    result = {}
    while pos < len(data) and data[pos]:
        length = data[pos]
        rec = data[pos:pos + length]
        if not rec[25] & 2:
            result[rec[33:33 + rec[32]].decode('ascii').split(';')[0]] = pos
        pos += length
    return result, pos


def prepare(elf, manifest, iso, full_font=False, ui_strings=None, glyph_limit=None):
    if len(elf) != 2392880 or struct.unpack_from('<H', elf, 44)[0] != 2:
        raise ValueError('Unexpected executable layout')
    first = struct.unpack_from('<8I', elf, 52)
    second = struct.unpack_from('<8I', elf, 84)
    segment_base = first[2] + first[5]
    if first != (1, 128, 0x100000, 0x100000, 2392320, 22080896, 7, 128):
        raise ValueError('Unexpected original load segment')
    if second != (1, 2392448, segment_base, segment_base, 0, 0, 6, 16):
        raise ValueError('Second load segment is already in use')
    code_va = segment_base
    loader_va = code_va + 0x80
    name_va = code_va + 0x100
    space_va = code_va + 0x110
    advance_va = code_va + 0x140
    leads = FULL_LEADS if full_font else LEADS
    capacity = len(leads) * 188
    available_glyphs = JAPANESE_CELLS + capacity
    glyph_limit = available_glyphs if glyph_limit is None else glyph_limit
    if not JAPANESE_CELLS < glyph_limit <= available_glyphs:
        raise ValueError('Invalid allocated glyph limit')
    extra_cells = max(0, glyph_limit - ORIGINAL_CELLS)
    lead_va = code_va + 0x180
    table_va = code_va + (0x280 if full_font else 0x180)
    table_relative = table_va-code_va
    pool_offset=align(table_relative+(24+len(leads))*189,16)
    pool=bytearray()
    relocated=[]
    for item in (ui_strings or []):
        destination=segment_base+pool_offset+len(pool)
        original_address=item['offset']+DELTA
        references=[off for off in range(0xd6600,0xf9000,4) if struct.unpack_from('<I',elf,off)[0]==original_address]
        if not references:
            raise ValueError('No guarded UI pointer found: '+item['id'])
        relocated.append(dict(id=item['id'],source_address=original_address,address=destination,
                              byte_size=len(item['raw'])+1,references=references))
        pool.extend(item['raw']+b'\0')
        pool.extend(bytes(align(len(pool),4)-len(pool)))
    loaded_size = align(pool_offset+len(pool), 128)
    bank_va = segment_base + loaded_size
    # read_file rounds its reads up to complete 2048-byte sectors. Reserve
    # that padding too, so the last read cannot overwrite the new heap.
    bank_size = align(max(1, extra_cells) * 512, 2048)
    heap_start = bank_va + bank_size
    file_offset = align(len(elf), 128)
    extended_size = file_offset + loaded_size
    file_lba = manifest['files']['SLPS_253.96']['lba']
    bank_lba = align(file_lba * 2048 + extended_size, 2048) // 2048
    if bank_lba * 2048 + bank_size > manifest['iso_size']:
        raise ValueError('Disc padding is too small')
    # Original file and BSS end are distinct from the appended segment.
    if not 0x1600000 <= segment_base < bank_va < heap_start < 0x1e00000:
        raise ValueError('Added memory exceeds the reserved font region')
    blob = bytearray(loaded_size)
    blob[pool_offset:pool_offset+len(pool)]=pool
    # v0 contains glyph index. Preserve the original lookup's scratch registers.
    # Branch delay slot computes index * 512 for either bank.
    # Unallocated table slots must never turn into pointers inside the heap.
    # Glyph zero is the original blank (8140); use it for an out-of-range ID.
    lookup = [0x2c430000 | glyph_limit, 0x14600002, 0, 0x00001021]
    lookup += [0x2c430f00, 0x14600005, 0x00022240]
    lookup += address(3, bank_va - ORIGINAL_CELLS * 512)
    lookup += [0x10000003, 0]
    lookup += address(3, ORIGINAL_BANK)
    lookup += [0x00641821, lui(2, 0x2000), 0x00621025, jump(0x197458), 0]
    if len(lookup) * 4 > 0x80:
        raise ValueError('Lookup hook exceeds code reservation')
    blob[:len(lookup) * 4] = words(lookup)
    # The original loader tail-calls read_file(name, destination). This wrapper
    # loads the unchanged-size FONT.GF, then the separate HANGUL.GF asset.
    loader = [addiu(29, 29, -16), 0xffbf0000]
    loader += address(4, 0x347208) + address(5, ORIGINAL_BANK)
    loader += [jump(0x164de0, True), 0]
    loader += address(4, name_va) + address(5, bank_va)
    loader += [jump(0x164de0, True), 0, 0xdfbf0000, 0x03e00008, addiu(29, 29, 16)]
    if len(loader) * 4 > 0x80:
        raise ValueError('Loader hook exceeds code reservation')
    blob[0x80:0x80 + len(loader) * 4] = words(loader)
    blob[0x100:0x10a] = b'HANGUL.GF\0'
    # Keep the parser, blank sprite and the complete stock draw path.
    # The unused word at sp+b8 stores this glyph's horizontal advance;
    # sp+bc holds the original line origin and is kept intact. Only 8140
    # advances half a cell (10 px normally / 21 px enlarged).
    space_hook = [0x34038140, 0x14830002, 0x02401821, 0x00121842,
                  0xafa300b8, jump(0x196f48), 0]
    blob[0x110:0x110 + len(space_hook) * 4] = words(space_hook)
    advance_hook = [0x8fa300b8, jump(0x196ebc), 0x0283a021]
    blob[0x140:0x14c] = words(advance_hook)
    old_table = elf[0x3444c0 - DELTA:0x3444c0 - DELTA + 24 * 189]
    blob[table_relative:table_relative + len(old_table)] = old_table
    new_table = bytearray([255] * (len(leads) * 189))
    slots = []
    custom = bytearray(elf)
    lead_table = bytearray(elf[0x344400-DELTA:0x344400-DELTA+48*4]) + bytearray([255]*(16*4))
    for n, lead in enumerate(leads):
        index = lead - (0x80 if lead < 0xe0 else 0xc0)
        row = 0x344400 - DELTA + index * 4
        if lead < 0xef and elf[row:row + 4] != b'\xff' * 4:
            raise ValueError('Custom lead is already mapped')
        encoded = struct.pack('<hh', 24+n, JAPANESE_CELLS+n*188)
        if full_font:
            lead_table[index*4:index*4+4] = encoded
        else:
            custom[row:row+4] = encoded
        for i in range(188):
            trail = 0x40 + i + (i >= 63)
            new_table[n * 189 + trail - 0x40] = i
            slots.append((bytes([lead, trail]).hex(), JAPANESE_CELLS + n * 188 + i))
    blob[table_relative+24*189:table_relative+(24+len(leads))*189] = new_table
    if full_font:
        blob[0x180:0x280] = lead_table
    for item in relocated:
        for off in item['references']:
            custom[off:off+4]=struct.pack('<I',item['address'])
    changes = []
    def replace(va, before, after):
        off = va - DELTA
        if custom[off:off + len(before)] != before:
            raise ValueError('Instruction guard failed at %x' % va)
        custom[off:off + len(after)] = after
        changes.append(dict(address=va, before=before.hex(), after=after.hex()))
    replace(0x197440, words([lui(3, 0x4c), 0x00022240]), words([jump(code_va), 0]))
    replace(0x197480, words([lui(4, 0x34), lui(5, 0x4c)]), words([jump(loader_va), 0]))
    replace(0x196f10, words([0x1000000d, 0x3044ffff]),
            words([jump(space_va), 0x3044ffff]))
    replace(0x197028, words([0x1000ffa4, 0x0292a021]),
            words([jump(advance_va), 0]))
    high, low = address(3, table_va)
    replace(0x19740c, words([lui(3, 0x34)]), words([high]))
    replace(0x197414, words([addiu(3, 3, 0x44c0)]), words([low]))
    if full_font:
        # Parser and lookup must agree on new leads EF..FC. Extend only the
        # upper bound; newline/page/variable control handling is unchanged.
        replace(0x197320, words([0x28c100ef]), words([0x28c100fd]))
        replace(0x1973c8, words([0x28410030]), words([0x2841003d]))
        hi,lo = address(2,lead_va+2)
        replace(0x1973d4,words([lui(2,0x34)]),words([hi]))
        replace(0x1973d8,words([addiu(2,2,0x4402)]),words([lo]))
        hi,lo = address(2,lead_va)
        replace(0x1973e0,words([lui(2,0x34)]),words([hi]))
        replace(0x1973e8,words([addiu(2,2,0x4400)]),words([lo]))
    # Only SetupHeap moves. Original BSS clearing must stop before our loaded
    # code/table segment, so its separate end constant remains unchanged.
    high, low = address(4, heap_start)
    replace(0x1001d0, words([lui(4, 0x161)]), words([high]))
    replace(0x1001d8, words([addiu(4, 4, -0x1280)]), words([low]))
    # libkernel's sbrk tracks its own initial break in initialized data;
    # SetupHeap alone does not move this allocator pointer.
    replace(0x1ccdb4, words([segment_base]), words([heap_start]))
    custom[84:116] = struct.pack('<8I', 1, file_offset, segment_base, segment_base,
                                loaded_size, loaded_size + bank_size, 7, 128)
    custom.extend(bytes(file_offset - len(custom)))
    custom.extend(blob)
    with open(iso, 'rb') as f:
        f.seek(16 * 2048)
        pvd = f.read(2048)
        root_lba = struct.unpack_from('<I', pvd, 158)[0]
        root_size = struct.unpack_from('<I', pvd, 166)[0]
        f.seek(root_lba * 2048)
        directory = bytearray(f.read(2048))
        f.seek(file_lba * 2048 + len(elf))
        padding = f.read(bank_lba * 2048 + bank_size - f.tell())
    if any(padding):
        raise ValueError('Font disc additions would overwrite nonzero padding')
    records, end = root_records(directory)
    if records.keys() != manifest['files'].keys() or end != root_size:
        raise ValueError('Root directory differs from extracted inventory')
    rec = records['SLPS_253.96']
    directory[rec + 10:rec + 18] = struct.pack('<I', extended_size) + struct.pack('>I', extended_size)
    filename = b'HANGUL.GF;1'
    length = 33 + len(filename) + (len(filename) % 2 == 0)
    if end + length > 2048 or any(directory[end:]):
        raise ValueError('Root directory has no blank record space')
    entry = bytearray(length)
    entry[0] = length
    entry[2:10] = struct.pack('<I', bank_lba) + struct.pack('>I', bank_lba)
    entry[10:18] = struct.pack('<I', bank_size) + struct.pack('>I', bank_size)
    entry[18:25] = directory[rec + 18:rec + 25]
    entry[28:32] = b'\x01\x00\x00\x01'
    entry[32] = len(filename)
    entry[33:33 + len(filename)] = filename
    directory[end:end + length] = entry
    # Both endian copies of root size appear in the PVD and . / .. records.
    root_new_size = end + length
    size_bytes = struct.pack('<I', root_new_size) + struct.pack('>I', root_new_size)
    for offset in [10, directory[0] + 10]:
        directory[offset:offset + 8] = size_bytes
    pvd = bytearray(pvd)
    pvd[166:174] = size_bytes
    metadata = dict(strategy='second_elf_segment_and_separate_font_bank',capacity=capacity,
                    full_font=full_font,lead_table_address=lead_va if full_font else 0x344400,
                    parser_lead_limit=0xfd if full_font else 0xef,
                    ui_relocations=relocated,
                    original_font_address=ORIGINAL_BANK,segment_address=segment_base,
                    table_address=table_va,lookup_hook=code_va,loader_hook=loader_va,
                    space_advance_hook=space_va,space_code='8140',
                    glyph_advance_hook=advance_va,space_hook_size=28,
                    advance_hook_size=12,advance_stack_offset=0xb8,
                    normal_space_pixels=10,enlarged_space_pixels=21,
                    added_font_address=bank_va,added_font_size=bank_size,heap_start=heap_start,
                    allocated_glyph_limit=glyph_limit,allocated_added_cells=extra_cells,
                    segment_file_offset=file_offset,segment_file_size=loaded_size,
                    extended_elf_size=extended_size,added_font_lba=bank_lba,
                    root_directory_size=root_new_size,instruction_changes=changes)
    return custom, slots, metadata, [
        (16 * 2048, bytes(pvd), 'iso:primary-volume-root-size'),
        (root_lba * 2048, bytes(directory), 'iso:font-and-executable-directory')]
