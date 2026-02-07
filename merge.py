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
import difflib

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
            # Update programme's channel attribute with the proper channel_id
            prog.set('channel', channel_id)
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


def get_demo_channels():
    """读取 demo.txt 获取普通节目频道列表"""
    channels = set()
    with open('demo.txt', 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith('#'):
                channels.add(line)
    return channels


def get_alias_mapping():
    """读取 alias.txt 获取频道别名映射关系"""
    alias_map = {}
    reverse_alias_map = {}
    with open('alias.txt', 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith('#'):
                parts = line.split(',')
                if len(parts) >= 2:
                    original_name = parts[0]
                    aliases = parts[1:]
                    alias_map[original_name] = aliases
                    for alias in aliases:
                        reverse_alias_map[alias] = original_name
    return alias_map, reverse_alias_map


def get_4k_channels():
    """读取 4k.txt 获取4K频道列表"""
    channels = set()
    with open('4k.txt', 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith('#'):
                channels.add(line)
    return channels


def get_urls():
    urls = []
    with open('config.txt', 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith('#'):
                urls.append(line)
    return urls


def fuzzy_match_channel(channel_name, target_channels, threshold=0.6):
    """
    模糊匹配频道名称
    频道名称使用别名映射后的结果<节目源先使用原名匹配如果没有就使用别名>
    """
    for target in target_channels:
        # 直接匹配原名
        if channel_name == target:
            return target
        # 使用 difflib 进行模糊匹配
        similarity = difflib.SequenceMatcher(None, channel_name, target).ratio()
        if similarity >= threshold:
            return target
    return None


def apply_alias_mapping(display_names, alias_map, reverse_alias_map):
    """
    应用别名映射到频道显示名称
    频道名称使用别名映射后的结果<节目源先使用原名匹配如果没有就使用别名>
    """
    mapped_names = []
    for name_node in display_names:
        original_name = name_node[0]
        lang = name_node[1]
        
        # 检查是否有别名映射
        if original_name in alias_map:
            # 添加原名
            mapped_names.append([original_name, lang])
            # 添加所有别名
            for alias in alias_map[original_name]:
                mapped_names.append([alias, lang])
        # 检查是否是别名，需要映射回原名
        elif original_name in reverse_alias_map:
            mapped_name = reverse_alias_map[original_name]
            mapped_names.append([mapped_name, lang])
        else:
            mapped_names.append([original_name, lang])
    
    return mapped_names


async def main():
    # 读取三个输入文件
    demo_channels = get_demo_channels()  # 普通节目频道列表
    alias_map, reverse_alias_map = get_alias_mapping()  # 频道别名映射
    k4_channels = get_4k_channels()  # 4K频道列表
    
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
                
                # 获取频道显示名称列表
                channel_display_names = [name_node[0] for name_node in display_names]
                
                # 检查是否是4K频道（模糊匹配）
                is_4k_channel = False
                matched_4k_channel = None
                for display_name in channel_display_names:
                    matched_4k_channel = fuzzy_match_channel(display_name, k4_channels)  # 使用显示名称进行匹配
                    if matched_4k_channel:
                        is_4k_channel = True
                        break
                
                # 检查是否是普通频道（模糊匹配）
                is_demo_channel = False
                matched_demo_channel = None
                for display_name in channel_display_names:
                    matched_demo_channel = fuzzy_match_channel(display_name, demo_channels)  # 使用显示名称进行匹配
                    if matched_demo_channel:
                        is_demo_channel = True
                        break
                
                # 检查是否是普通频道（模糊匹配）
                is_demo_channel = False
                matched_demo_channel = None
                for display_name in channel_display_names:
                    matched_demo_channel = fuzzy_match_channel(display_name, demo_channels)
                    if matched_demo_channel:
                        is_demo_channel = True
                        break
                
                # 只处理4K频道或经过过滤后的普通频道
                # 对于4K频道，即使原始名称不匹配，也应保留其节目信息
                if not (is_4k_channel or is_demo_channel):
                    pbar.update(1)
                    continue
                
                # 如果是4K频道，需要特殊处理 - 先尝试映射到非4K版本
                processed_display_names = []
                is_4k_adjusted = False
                
                if is_4k_channel:
                    # 尝试找到对应的非4K频道名称
                    for name_node in display_names:
                        original_name = name_node[0]
                        lang = name_node[1]
                        
                        # 如果频道名以4K结尾，尝试去掉4K后缀进行匹配
                        if original_name.endswith('4K') or original_name.endswith('4k'):
                            non_4k_name = original_name[:-2]  # 去掉最后的4K
                            # 检查去掉4K后的名称是否在普通频道列表中
                            if fuzzy_match_channel(non_4k_name, demo_channels):
                                processed_display_names.append([non_4k_name, lang])
                                is_4k_adjusted = True
                            else:
                                processed_display_names.append([original_name, lang])
                        else:
                            processed_display_names.append([original_name, lang])
                    
                    if not is_4k_adjusted:
                        processed_display_names = display_names
                else:
                    processed_display_names = display_names
                
                # 应用别名映射
                mapped_display_names = apply_alias_mapping(processed_display_names, alias_map, reverse_alias_map)
                
                is_in_map = channel_id in all_channels_map
                map_id = channel_id
                for display_name_node in mapped_display_names:
                    display_name = display_name_node[0]
                    if is_in_map:
                        break
                    is_in_map = is_in_map or (display_name in all_channels_map)
                    map_id = display_name
                map_id = all_channels_map.get(map_id, channel_id)
                
                if not is_in_map:
                    all_channel_id.add(channel_id)
                    all_channel_names[channel_id] = mapped_display_names
                    all_programmes[channel_id] = programmes[channel_id]
                    all_channels_map[channel_id] = channel_id
                    for display_name_node in mapped_display_names:
                        display_name = display_name_node[0]
                        all_channels_map[display_name] = channel_id
                elif len(all_programmes[map_id]) < len(programmes[channel_id]):
                    all_programmes[map_id] = programmes[channel_id]
                    for display_name_node in mapped_display_names:
                        display_name = display_name_node[0]
                        if display_name not in all_channels_map:
                            all_channel_names[map_id].append(display_name_node)
                            all_channels_map[display_name] = map_id
                pbar.update(1)  # 更新进度条
    
    print("Writing to XML...")
    write_to_xml(all_channel_id, all_channel_names,
                all_programmes, 'output/epg.xml')
    compress_to_gz('output/epg.xml', 'output/epg.gz')

if __name__ == '__main__':
    asyncio.run(main())