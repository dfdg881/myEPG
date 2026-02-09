import xml.etree.ElementTree as ET
from collections import defaultdict
import aiohttp
import asyncio
from tqdm.asyncio import tqdm_asyncio  # 引入 tqdm 的异步支持
from datetime import datetime, timezone, timedelta
import gzip
import shutil
from xml.dom import minidom
import re
from copy import deepcopy
from opencc import OpenCC
import os
from tqdm import tqdm  # 引入 tqdm 的同步支持

TZ_UTC_PLUS_8 = timezone(timedelta(hours=8))


def transform2_zh_hans(string):
    cc = OpenCC("t2s")
    new_str = cc.convert(string)
    return new_str


async def fetch_epg(url):
    connector = aiohttp.TCPConnector(limit=16, ssl=False)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession(connector=connector, trust_env=True, headers=headers) as session:
            async with session.get(url) as response:
                if url.endswith('.gz'):
                    compressed_data = await response.read()
                    return gzip.decompress(compressed_data).decode('utf-8', errors='ignore')
                else:
                    return await response.text(encoding='utf-8')
    except aiohttp.ClientError as e:
        print(f"{url}HTTP请求错误: {e}")
    except asyncio.TimeoutError:
        print("{url}请求超时")
    except Exception as e:
        print(f"{url}其他错误: {e}")
    return None

def process_display_name(display_name):
    if display_name.endswith('高清'):
        display_name = display_name[:-2]
    return display_name

def parse_epg(epg_content):
    try:
        parser = ET.XMLParser(encoding='UTF-8')
        root = ET.fromstring(epg_content, parser=parser)
    except ET.ParseError as e:
        print(f"Error parsing XML: {e}")
        print(f"Problematic content: {epg_content[:500]}")
        return {}, defaultdict(list)

    channels = {}
    programmes = defaultdict(list)

    for channel in root.findall('channel'):
        channel_id = transform2_zh_hans(channel.get('id'))
        channel_display_names = []
        for name in channel.findall('display-name'):
            t_name = transform2_zh_hans(name.text)
            t_name = process_display_name(t_name)
            channel_display_names.append([t_name, name.get('lang', 'zh')])
        if not channel_id.isdigit() and channel_id not in channel_display_names:
            channel_display_names.append([channel_id, 'zh'])
        channels[channel_id] = channel_display_names

    today = datetime.now(TZ_UTC_PLUS_8).date()
    valid_channels = set()

    for programme in root.findall('programme'):
        channel_id = transform2_zh_hans(programme.get('channel'))
        channel_start = datetime.strptime(
            re.sub(r'\s+', '', programme.get('start')), "%Y%m%d%H%M%S%z")
        channel_stop = datetime.strptime(
            re.sub(r'\s+', '', programme.get('stop')), "%Y%m%d%H%M%S%z")
        channel_start = channel_start.astimezone(TZ_UTC_PLUS_8)
        channel_stop = channel_stop.astimezone(TZ_UTC_PLUS_8)

        if channel_stop.date() == today:
            valid_channels.add(channel_id)

        channel_elem = ET.SubElement(
            root, 'programme', attrib={"channel": channel_id, "start": channel_start.strftime("%Y%m%d%H%M%S %z"), "stop": channel_stop.strftime("%Y%m%d%H%M%S %z")})
        for title in programme.findall('title'):
            if title.text is None:
                channel_title = "精彩节目"
            else:
                channel_title = title.text.strip()
            langattr = title.get('lang')
            if langattr == 'zh' or langattr is None:
                channel_title = transform2_zh_hans(channel_title)
            channel_elem_t = ET.SubElement(
                channel_elem, 'title')
            channel_elem_t.text = channel_title
            if langattr is not None:
                channel_elem_t.set('lang', langattr)
        for desc in programme.findall('desc'):
            if desc.text is None:
                continue
            langattr = desc.get('lang')
            channel_desc = desc.text.strip()
            if langattr == 'zh' or langattr is None:
                channel_desc = transform2_zh_hans(channel_desc)
            channel_elem_d = ET.SubElement(
                channel_elem, 'desc')
            channel_elem_d.text = channel_desc.strip()
            if langattr is not None:
                channel_elem_d.set('lang', langattr)
        programmes[channel_id].append(channel_elem)

    # Filter channels that don't have any program ending today
    channels = {k: v for k, v in channels.items() if k in valid_channels}
    # Optional: Filter programmes as well to keep data consistent, 
    # though only valid channels are returned so main loop might be fine.
    # But filtering programmes dict saves memory and ensures correctness if main iterates programmes keys logic changes.
    programmes = {k: v for k, v in programmes.items() if k in valid_channels}

    return channels, programmes


def write_to_xml(channels_id, channels_names, programmes, filename):
    # 目录不存在
    if not os.path.exists('output'):
        os.makedirs('output')
    current_time = datetime.now(TZ_UTC_PLUS_8).strftime("%Y%m%d%H%M%S %z")
    root = ET.Element('tv', attrib={'date': current_time})
    for channel_id in channels_id:
        channel_elem = ET.SubElement(
            root, 'channel', attrib={"id": channel_id})
        for display_name_node in channels_names[channel_id]:
            display_name = display_name_node[0]
            langattr = display_name_node[1]
            display_name_elem = ET.SubElement(
                channel_elem, 'display-name', attrib={"lang": langattr})
            display_name_elem.text = display_name
        for prog in programmes[channel_id]:
            prog.set('channel', channel_id)  # 设置 programme 的 channel 属性
            root.append(prog)

    # Beautify the XML output
    rough_string = ET.tostring(root, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(reparsed.toprettyxml(indent='\t', newl='\n'))


def compress_to_gz(input_filename, output_filename):
    with open(input_filename, 'rb') as f_in:
        with gzip.open(output_filename, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)


def get_urls():
    urls = []
    with open('config.txt', 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith('#'):
                urls.append(line)
    return urls


def normalize_channel_name(name):
    if not name:
        return ""
    name = transform2_zh_hans(name).strip().lower()
    name = re.sub(r"[\s\-_/]", "", name)
    return name


def get_demo_channels():
    channels = []
    with open('demo.txt', 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith('#'):
                channels.append(line)
    return channels


def get_alias_map():
    alias_map = defaultdict(set)
    if not os.path.exists('alias.txt'):
        return alias_map
    with open('alias.txt', 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            names = [item.strip() for item in line.split(',') if item.strip()]
            if not names:
                continue
            canonical = names[0]
            for alias_name in names:
                alias_map[canonical].add(alias_name)
    return alias_map


def is_cctv16_4k(name):
    n_name = normalize_channel_name(name)
    return 'cctv16' in n_name and '4k' in n_name


def is_cctv16_non_4k(name):
    n_name = normalize_channel_name(name)
    return 'cctv16' in n_name and '4k' not in n_name


def build_target_aliases(demo_channels, alias_map):
    demo_norm_to_name = {normalize_channel_name(ch): ch for ch in demo_channels}
    target_aliases = {}
    for target_name in demo_channels:
        target_norm = normalize_channel_name(target_name)
        alias_set = {target_norm}

        for canonical, aliases in alias_map.items():
            canonical_norm = normalize_channel_name(canonical)
            alias_norms = {normalize_channel_name(alias_name) for alias_name in aliases}
            if canonical_norm == target_norm or target_norm in alias_norms:
                alias_set.update(alias_norms)
                alias_set.add(canonical_norm)

        target_aliases[target_name] = alias_set

    return demo_norm_to_name, target_aliases


def match_channel_to_demo(candidates, demo_channels, demo_norm_to_name, target_aliases):
    # Special handling for duplicated CCTV16 aliases:
    # keep CCTV16-4K and CCTV-16 as two distinct channels.
    has_4k = any(is_cctv16_4k(name) for name in candidates)
    has_non_4k = any(is_cctv16_non_4k(name) for name in candidates)
    if has_4k and 'CCTV16-4K' in demo_channels:
        return 'CCTV16-4K'
    if has_non_4k and 'CCTV-16' in demo_channels:
        return 'CCTV-16'

    candidate_norms = []
    for name in candidates:
        n_name = normalize_channel_name(name)
        if n_name:
            candidate_norms.append(n_name)

    # exact match with demo names first
    for n_name in candidate_norms:
        if n_name in demo_norm_to_name:
            return demo_norm_to_name[n_name]

    # exact match with alias map
    for target_name in demo_channels:
        aliases = target_aliases[target_name]
        if any(n_name in aliases for n_name in candidate_norms):
            return target_name

    # fuzzy contains match
    best_target = None
    best_score = -1
    for target_name in demo_channels:
        target_norm = normalize_channel_name(target_name)
        aliases = target_aliases[target_name]
        score = 0
        for n_name in candidate_norms:
            if n_name == target_norm:
                score = max(score, 100)
            elif any(n_name in alias_name or alias_name in n_name for alias_name in aliases):
                score = max(score, 10)
            elif n_name in target_norm or target_norm in n_name:
                score = max(score, 5)
        if score > best_score:
            best_score = score
            best_target = target_name

    return best_target if best_score > 0 else None


def remap_to_demo_channels(all_channel_id, all_channel_names, all_programmes):
    demo_channels = get_demo_channels()
    alias_map = get_alias_map()
    demo_norm_to_name, target_aliases = build_target_aliases(demo_channels, alias_map)

    remapped_channel_ids = []
    remapped_channel_names = defaultdict(list)
    remapped_programmes = defaultdict(list)

    for channel_id in all_channel_id:
        display_names = all_channel_names.get(channel_id, [])
        candidates = [channel_id]
        for node in display_names:
            if node and node[0]:
                candidates.append(node[0])

        target_name = match_channel_to_demo(
            candidates, demo_channels, demo_norm_to_name, target_aliases)
        if target_name is None:
            continue

        programme_list = all_programmes.get(channel_id, [])
        if len(programme_list) == 0:
            for name in candidates:
                if len(all_programmes.get(name, [])) > 0:
                    programme_list = all_programmes[name]
                    break
        if len(programme_list) == 0:
            continue

        if target_name not in remapped_channel_ids:
            remapped_channel_ids.append(target_name)

        # Keep the richer programme source when multiple channels map to same target.
        if len(remapped_programmes[target_name]) < len(programme_list):
            remapped_programmes[target_name] = programme_list

        remapped_channel_names[target_name] = [[target_name, 'zh']]

    # Fallback: if CCTV16-4K has no programme, reuse CCTV-16 programme.
    if 'CCTV16-4K' in demo_channels and len(remapped_programmes['CCTV16-4K']) == 0:
        if len(remapped_programmes['CCTV-16']) > 0:
            remapped_programmes['CCTV16-4K'] = [deepcopy(prog) for prog in remapped_programmes['CCTV-16']]

    # Keep both CCTV-16 and CCTV16-4K in final output when configured in demo.txt.
    for fixed_name in ('CCTV-16', 'CCTV16-4K'):
        if fixed_name in demo_channels and fixed_name not in remapped_channel_ids:
            remapped_channel_ids.append(fixed_name)
            remapped_channel_names[fixed_name] = [[fixed_name, 'zh']]

    # Keep final order as in demo.txt
    ordered_ids = [name for name in demo_channels if name in remapped_channel_ids]
    return ordered_ids, remapped_channel_names, remapped_programmes


async def main():
    urls = get_urls()
    tasks = [fetch_epg(url) for url in urls]
    print("Fetching EPG data...")
    epg_contents = await tqdm_asyncio.gather(*tasks, desc="Fetching URLs")
    all_channels_map = {}
    all_channel_id = set()
    all_channel_names = defaultdict(list)
    all_programmes = defaultdict(list)
    print("Finished.")
    i = 0
    for epg_content in epg_contents:
        i += 1
        print(f"Processing EPG source...{i}/{len(epg_contents)}")
        if epg_content is None:
            continue
        print("Parsing EPG data...")
        channels, programmes = parse_epg(epg_content)
        print("Finished.")
        with tqdm(total=len(channels), desc="Merging EPG", unit="file") as pbar:
            for channel_id, display_names in channels.items():
                if len(programmes[channel_id]) == 0:
                    continue
                is_in_map = channel_id in all_channels_map
                map_id = channel_id
                for display_name_node in display_names:
                    display_name = display_name_node[0]
                    if is_in_map:
                        break
                    is_in_map = is_in_map or (display_name  in all_channels_map)
                    map_id = display_name
                map_id = all_channels_map.get(map_id, channel_id)
                if not is_in_map:
                    all_channel_id.add(channel_id)
                    all_channel_names[channel_id] = display_names
                    all_programmes[display_name] = programmes[channel_id]
                    all_channels_map[channel_id] = channel_id
                    for display_name_node in display_names:
                        display_name = display_name_node[0]
                        all_channels_map[display_name] = channel_id
                elif len(all_programmes[map_id]) < len(programmes[channel_id]):
                    all_programmes[map_id] = programmes[channel_id]
                    for display_name_node in display_names:
                        display_name = display_name_node[0]
                        if display_name not in all_channels_map:
                            all_channel_names[map_id].append(display_name_node)
                            all_channels_map[display_name] = map_id
                pbar.update(1)  # 更新进度条
    print("Writing to XML...")
    remapped_channel_ids, remapped_channel_names, remapped_programmes = remap_to_demo_channels(
        all_channel_id, all_channel_names, all_programmes)
    write_to_xml(remapped_channel_ids, remapped_channel_names,
                remapped_programmes, 'output/epg.xml')
    compress_to_gz('output/epg.xml', 'output/epg.gz')

if __name__ == '__main__':
    asyncio.run(main())
