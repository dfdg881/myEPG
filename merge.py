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


# ----------------------
# 辅助：从文本文件读取节目条目
def load_lines(path):
    lines = []
    if not os.path.exists(path):
        return lines
    with open(path, 'r', encoding='utf-8') as f:
        for raw in f:
            s = raw.rstrip('\n').strip()
            if not s:
                continue
            if s.startswith('#'):
                continue
            lines.append(raw.rstrip('\n'))  # 保留原始文本格式用于输出
    return lines


def parse_aliases(path):
    mapping = {}
    if not os.path.exists(path):
        return mapping
    with open(path, 'r', encoding='utf-8') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            # 支持多种分隔符：|、:、=
            for sep in ['|', ':', '=']:
                if sep in line:
                    left, right = line.split(sep, 1)
                    key = left.strip().lower()
                    val = right.strip()
                    if key:
                        mapping[key] = val
                    break
            # 未找到分隔符的行忽略
    return mapping


def normalize_name(name, alias_map):
    if name is None:
        return ''
    s = name.strip()
    s = s.lower()
    if s in alias_map:
        s = alias_map[s]
    return s


def _make_programme(channel_id, title, start_dt):
    """Create a minimal programme element for local sources.
    start_dt should be a timezone-aware datetime in TZ_UTC_PLUS_8.
    Returns an xml.etree.ElementTree.Element representing a <programme>.
    """
    start = start_dt.strftime("%Y%m%d%H%M%S %z")
    stop_dt = start_dt + timedelta(minutes=30)
    stop = stop_dt.strftime("%Y%m%d%H%M%S %z")
    p = ET.Element('programme', attrib={'channel': channel_id, 'start': start, 'stop': stop})
    t = ET.SubElement(p, 'title')
    t.text = title
    return p


def _load_local_sources(alias_map):
    """Load local channel lists from demo.txt (normal) and 4k.txt (4K).
    Deduplicate between sources (prefer normal/demo entries).
    Returns:
      channels: dict[channel_id] -> display_names_list
      programmes: dict[channel_id] -> list[programme_elements]
    """
    channels = {}
    programmes = {}

    today = datetime.now(TZ_UTC_PLUS_8).date()
    # Base start times for local programmes
    base_normal = datetime.now(TZ_UTC_PLUS_8).replace(year=today.year, month=today.month, day=today.day, hour=10, minute=0, second=0, microsecond=0)
    base_fourk  = datetime.now(TZ_UTC_PLUS_8).replace(year=today.year, month=today.month, day=today.day, hour=16, minute=0, second=0, microsecond=0)

    # Load lists
    normal_lines = load_lines('demo.txt')
    k_lines = load_lines('4k.txt')

    normal_map = {}
    fourk_map = {}
    # Normalize and prepare channels for normal (demo.txt)
    for raw in normal_lines:
        name = raw.strip()
        if not name:
            continue
        cid = normalize_name(name, alias_map)
        if not cid:
            cid = name
        if cid in normal_map:
            continue
        normal_map[cid] = name

    # Normalize and prepare channels for 4K (4k.txt)
    for raw in k_lines:
        name = raw.strip()
        if not name:
            continue
        cid = normalize_name(name, alias_map)
        if not cid:
            cid = name
        if cid in fourk_map:
            continue
        fourk_map[cid] = name

    # Deduplicate: prefer normal/demo, ignore duplicates found in 4K
    all_ids = {}
    for cid, disp in normal_map.items():
        all_ids[cid] = {'display': disp, 'source': 'normal'}
    for cid, disp in fourk_map.items():
        if cid in all_ids:
            # duplicate between normal and 4K, skip 4K entry
            continue
        all_ids[cid] = {'display': disp, 'source': '4k'}

    # Build channels and fake programmes for local sources
    for cid, info in all_ids.items():
        display = info['display']
        channels[cid] = [[display, 'zh']]
        # Create a single placeholder programme per channel
        if info['source'] == 'normal':
            title = f"普通节目：{display}"
            p = _make_programme(cid, title, base_normal)
        else:
            title = f"4K节目：{display}"
            p = _make_programme(cid, title, base_fourk)
        programmes[cid] = [p]

    return channels, programmes

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


async def main():
    # Load alias map for name normalization
    alias_map = parse_aliases('alias.txt')
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
                    all_programmes[channel_id] = programmes[channel_id]
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
    # Merge local sources (demo.txt and 4k.txt) into the final epg data
    local_channels, local_programmes = _load_local_sources(alias_map)
    # Simple merge: add local channels if not present, else keep the larger set of programmes
    for cid, disp_names in local_channels.items():
        is_in_map = cid in all_channels_map
        map_id = cid
        if not is_in_map:
            all_channel_id.add(cid)
            all_channel_names[cid] = disp_names
            # attach local programmes under the local channel id
            all_programmes[cid] = local_programmes.get(cid, [])
            all_channels_map[cid] = cid
            for display_node in disp_names:
                all_channels_map[display_node[0]] = cid
        else:
            # If already present, decide whether to replace with local data based on length
            # Identify map target from existing mappings
            for display_node in disp_names:
                display_name = display_node[0]
                if display_name in all_channels_map:
                    map_id = all_channels_map[display_name]
                    break
            if cid in local_programmes and len(local_programmes[cid]) > len(all_programmes.get(map_id, [])):
                all_programmes[map_id] = local_programmes[cid]
                for display_node in disp_names:
                    display_name = display_node[0]
                    if display_name not in all_channels_map:
                        all_channel_names[map_id].append(display_node)
                        all_channels_map[display_name] = map_id
    print("Writing to XML...")
    write_to_xml(all_channel_id, all_channel_names,
                all_programmes, 'output/epg.xml')
    compress_to_gz('output/epg.xml', 'output/epg.gz')

if __name__ == '__main__':
    asyncio.run(main())
