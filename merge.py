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
            channel_display_names.append([transform2_zh_hans(name.text), name.get('lang', 'zh')])
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
    if name is None:
        return ''
    name = transform2_zh_hans(name)
    name = name.strip()
    name = re.sub(r'\s+', '', name)
    name = name.replace('－', '-')
    return name


def load_demo_channels(filename='demo.txt'):
    channels = []
    with open(filename, 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            channels.append(line)
    return channels


def load_alias_map(filename='alias.txt'):
    alias_map = {}
    reverse_map = {}
    with open(filename, 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.split(',') if p.strip()]
            if not parts:
                continue
            canonical = parts[0]
            aliases = parts[1:]
            alias_map[canonical] = aliases
            for alias in aliases:
                reverse_map[alias] = canonical
    return alias_map, reverse_map


def build_demo_matchers(demo_channels, alias_map):
    canonical_set = set()
    normalized_canonical = {}
    normalized_alias_to_canonical = {}
    for canonical in demo_channels:
        normalized = normalize_channel_name(canonical)
        canonical_set.add(canonical)
        normalized_canonical[normalized] = canonical
        aliases = alias_map.get(canonical, [])
        for alias in aliases:
            normalized_alias = normalize_channel_name(alias)
            if normalized_alias:
                normalized_alias_to_canonical[normalized_alias] = canonical
    return canonical_set, normalized_canonical, normalized_alias_to_canonical


def match_canonical_channel(name, normalized_canonical, normalized_alias_to_canonical):
    normalized = normalize_channel_name(name)
    if normalized in normalized_canonical:
        return normalized_canonical[normalized]
    if normalized in normalized_alias_to_canonical:
        return normalized_alias_to_canonical[normalized]
    for alias_norm, canonical in normalized_alias_to_canonical.items():
        if alias_norm and (alias_norm in normalized or normalized in alias_norm):
            return canonical
    return None


async def main():
    urls = get_urls()
    demo_channels = load_demo_channels('demo.txt')
    alias_map, alias_reverse = load_alias_map('alias.txt')
    canonical_set, normalized_canonical, normalized_alias_to_canonical = build_demo_matchers(
        demo_channels, alias_map
    )
    tasks = [fetch_epg(url) for url in urls]
    print("Fetching EPG data...")
    epg_contents = await tqdm_asyncio.gather(*tasks, desc="Fetching URLs")
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
                canonical = match_canonical_channel(
                    channel_id, normalized_canonical, normalized_alias_to_canonical
                )
                if canonical is None:
                    for display_name_node in display_names:
                        display_name = display_name_node[0]
                        canonical = match_canonical_channel(
                            display_name, normalized_canonical, normalized_alias_to_canonical
                        )
                        if canonical is not None:
                            break
                if canonical is None:
                    pbar.update(1)
                    continue
                if canonical not in canonical_set:
                    pbar.update(1)
                    continue
                all_channel_id.add(canonical)
                if canonical not in all_channel_names:
                    all_channel_names[canonical] = [[canonical, 'zh']]
                if canonical not in all_programmes or len(all_programmes[canonical]) < len(programmes[channel_id]):
                    all_programmes[canonical] = programmes[channel_id]
                pbar.update(1)  # 更新进度条
    print("Writing to XML...")
    write_to_xml(all_channel_id, all_channel_names,
                all_programmes, 'output/epg.xml')
    compress_to_gz('output/epg.xml', 'output/epg.gz')

if __name__ == '__main__':
    asyncio.run(main())
