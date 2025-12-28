# -*- coding: utf-8 -*-
import os
import signal
import json
from datetime import datetime, timedelta, timezone
import asyncio
import aiohttp
import feedparser
import discord
from discord import Embed
from discord.ext import tasks, commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
import logging
from dotenv import load_dotenv
import urllib.parse

# 配置日志记录器
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 加载环境变量
load_dotenv()

# 定义条目保留时间为7天
ENTRY_LIFETIME = timedelta(days=7)

# 从环境变量中获取必要的Token
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GOOGLE_TRANSLATE_API_KEY = os.getenv('GOOGLE_TRANSLATE_API_KEY')
DEEPL_API_KEY = os.getenv('DEEPL_API_KEY')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

loop = None
scheduler = None

# 检查所有必要的Token是否存在
if not all([GOOGLE_TRANSLATE_API_KEY, DEEPL_API_KEY, DISCORD_TOKEN]):
    raise ValueError("一个或多个环境变量丢失或无效")

# 默认RSS源列表
DEFAULT_RSS_FEEDS = [
    'https://feeds.a.dj.com/rss/RSSOpinion.xml',
    'https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml',
    'https://www.foreignaffairs.com/rss.xml',
    'https://www.ft.com/opinion?format=rss',
    'https://www.ft.com/emerging-markets?format=rss',
    'https://www.ft.com/myft/following/83f62cc4-55d5-4efb-94d0-cd2680322216.rss',
    'https://www.reutersagency.com/feed/?best-types=reuters-news-first&post_type=best',
    'https://www.reutersagency.com/feed/?best-types=the-big-picture&post_type=best',
    'https://www.theatlantic.com/feed/all/',
    'https://www.economist.com/leaders/rss.xml',
    'https://www.economist.com/special-report/rss.xml',
    'https://www.economist.com/the-economist-explains/rss.xml',
    'https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml',
    'https://rss.nytimes.com/services/xml/rss/nyt/Lens.xml',
    'https://rss.nytimes.com/services/xml/rss/nyt/World.xml',
    'https://feeds.bloomberg.com/economics/news.rss',
    'https://feeds.bloomberg.com/bview/news.rss',
    'https://feeds.bloomberg.com/industries/news.rss',
    'https://theconversation.com/global/home-page.atom',
    'https://nautil.us/feed/',
    'https://longreads.com/feed',
    'https://blog.cloudflare.com/rss',
    'https://www.eff.org/rss/updates.xml'
]

# 配置文件路径
CONFIG_FILE = 'config.json'

BOT_PREFIX = '!'

# 初始化Discord Bot
intents = discord.Intents.default()
intents.message_content = True  # 确保机器人能够读取消息内容
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# 序列化 datetime 对象为字符串
def datetime_serializer(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"类型 {type(obj)} 无法序列化")

# 反序列化字符串为 datetime 对象
def datetime_deserializer(dct):
    for key, value in dct.items():
        try:
            dct[key] = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            pass
    return dct

# 读取配置文件
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as file:
                return json.load(file, object_hook=datetime_deserializer)
        except json.JSONDecodeError:
            logger.error("配置文件JSON解码失败，使用默认配置启动。")
            return {"rss_feeds": DEFAULT_RSS_FEEDS, "language": "zh-CN", "processed_entries": {}, "interval": 720, "channel_id": None}
    else:
        config = {"rss_feeds": DEFAULT_RSS_FEEDS, "language": "zh-CN", "processed_entries": {}, "interval": 720, "channel_id": None}
        save_config(config)
        return config

# 保存配置文件
def save_config(config):
    with open(CONFIG_FILE, 'w') as file:
        json.dump(config, file, default=datetime_serializer)

# 清理过期条目
async def cleanup_processed_entries():
    config = load_config()
    processed_entries = config.get('processed_entries', {})
    now = datetime.now()
    to_remove = [link for link, timestamp in processed_entries.items() if now - timestamp > ENTRY_LIFETIME]
    for link in to_remove:
        processed_entries.pop(link, None)
    config['processed_entries'] = processed_entries
    save_config(config)

# 域名与来源的映射
DOMAIN_TO_SOURCE_MAPPING = {
    'cnn.com': '有线电视新闻网',
    'bbc.com': '英国广播公司',
    'wsj.com': '华尔街日报',
    'foreignaffairs.com': '外交事务',
    'ft.com': '金融时报',
    'reuters.com': '路透社',
    'theatlantic.com': '大西洋月刊',
    'economist.com': '经济学人',
    'nytimes.com': '纽约时报',
    'bloomberg.com': '彭博社',
    'theconversation.com': '对话',
    'nautil.us': '鹦鹉螺',
    'longreads.com': '长读',
    'nature.com': '《自然》',
    'science.org': '《科学》',
    'eff.org': '电子前哨基金会',
    'ieee.org': '电气和电子工程师协会',
    'brookings.edu': '布鲁金斯学会',
}

# 定义信号处理函数
def signal_handler(sig, frame):
    global loop
    logger.info("收到终止信号。清理中...")
    if loop is not None:
        loop.stop()

# 解析并格式化RSS条目的发布时间，统一转换为UTC时间。
def parse_published_time(entry):
    if 'published_parsed' in entry and entry.published_parsed:
        published_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    elif 'published' in entry:
        try:
            published_time = feedparser.parse(entry.published).astimezone(timezone.utc)
        except (ValueError, TypeError):
            published_time = None
    else:
        published_time = None
    
    if published_time:
        return published_time.strftime('%Y-%m-%d %H:%M:%S %Z')
    else:
        return 'No date'

# 异步翻译文本
async def translate_text_async(text, language='zh-CN', use_google=True, retries=3, delay=2):
    url = ""
    params = {}

    if use_google:
        url = "https://translation.googleapis.com/language/translate/v2"
        params = {
            'q': text,
            'target': language,
            'key': GOOGLE_TRANSLATE_API_KEY
        }
    else:
        url = "https://api-free.deepl.com/v2/translate"
        params = {
            'auth_key': DEEPL_API_KEY,
            'text': text,
            'target_lang': language.upper()
        }

    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    response.raise_for_status()
                    data = await response.json()

            if use_google and 'error' in data:
                print('Google Translate API 出现错误，切换到 DeepL')
                return await translate_text_async(text, language, use_google=False)

            return data['data']['translations'][0]['translatedText'] if use_google else data['translations'][0]['text']
        except aiohttp.ClientError as e:
            if attempt < retries - 1:
                print(f"翻译文本时发生网络错误: {e}。{delay} 秒后重试...")
                await asyncio.sleep(delay)
            else:
                return f"在 {retries} 次尝试后翻译文本失败: {e}"

# 清理HTML标签并提取图片
def clean_html_and_extract_images(raw_html):
    if not raw_html.strip().startswith('<'):
        return raw_html, []
    
    soup = BeautifulSoup(raw_html, 'html.parser')
    text = soup.get_text()
    images = [img['src'] for img in soup.find_all('img') if 'src' in img.attrs]
    return text, images

# 获取RSS源
async def fetch_rss_feed_async(url, session):
    try:
        async with session.get(url) as response:
            if response.status != 200:
                logger.error(f"获取RSS源失败: {url}, 状态码: {response.status}")
                return []
            content = await response.text()
            feed = feedparser.parse(content)
            if not feed.entries:
                logger.warning(f"RSS源中未找到条目: {url}")
                return []

            articles = []
            for entry in feed.entries:
                entry_id = entry.get('id') or entry.get('guid') or entry.get('link') or f"{entry.title}-{entry.published}"
                title = entry.title if 'title' in entry else '无标题'
                summary, images = clean_html_and_extract_images(entry.get('summary') or entry.get('description') or entry.get('content', [{}])[0].get('value', 'No summary'))

                articles.append({
                    'id': entry_id,
                    'title': title,
                    'link': entry.link,
                    'source': entry.get('source', 'Unknown source'),
                    'summary': summary,
                    'images': images
                })
            return articles
    except Exception as e:
        logger.exception(f"从 {url} 获取RSS源时出错: {e}")
        return []

# 验证RSS源是否有效
async def is_valid_rss_feed(rss_url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(rss_url) as response:
                if response.status != 200:
                    return False
                content = await response.text()
                feed = feedparser.parse(content)
                return bool(feed.entries)
    except Exception as e:
        logger.error(f"验证RSS源 {rss_url} 时出错: {e}")
        return False

# Discord bot类
class MyClient(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduler = None

    @commands.Cog.listener()
    async def on_ready(self):
        print(f'已登录为 {self.bot.user}')
        channel = self.get_default_channel()
        if channel:
            await channel.send("您好！此频道已设置为RSS源更新。")

    @bot.command(name='add_rss')
    async def add_rss(ctx, rss_url):
        if ctx.guild is None:
            await ctx.send('This command can only be used in a server.')
            return
        if config_handler.add_rss_source(ctx.guild.id, rss_url):
            await ctx.send(f'RSS feed {rss_url} added')
        else:
            await ctx.send('The RSS feed is invalid or already exists')

    @bot.command(name='remove_rss')
    async def remove_rss(ctx, rss_url):
        if ctx.guild is None:
            await ctx.send('This command can only be used in a server.')
            return
        if config_handler.remove_rss_source(ctx.guild.id, rss_url):
            await ctx.send(f'RSS feed {rss_url} Removed')
        else:
            await ctx.send('RSS feed not found.')

    @bot.command(name='set_channel')
    async def set_channel(ctx, channel: discord.TextChannel = None):
        if ctx.guild is None:
            await ctx.send('This command can only be used in a server.')
            return

        if channel is None:
            await ctx.send('You must specify a channel.')
            return

        if config_handler.set_channel(ctx.guild.id, channel.id):
            await ctx.send(f'Channel is set to {channel.mention}')
        else:
            await ctx.send('Failed to set channel.')

    @bot.command(name='list_rss')
    async def list_rss(ctx):
        if ctx.guild is None:
            await ctx.send('This command can only be used in a server.')
            return
        rss_sources = config_handler.get_rss_sources(ctx.guild.id)
        if rss_sources:
            await ctx.send('Current RSS feed list:\n' + '\n'.join(rss_sources))
        else:
            await ctx.send('No RSS feeds.')

    @bot.command(name='set_interval')  # 更改RSS处理间隔时间
    async def set_interval(ctx, interval: int = None):
        if ctx.guild is None:
            await ctx.send('This command can only be used in a server.')
            return

        if interval is None:
            await ctx.send('You must specify an interval in minutes.')
            return

        if interval <= 0:
            await ctx.send('The interval must be greater than 0 minutes')
            return

        config_handler.set_interval(ctx.guild.id, interval)
        job_id = f'process_rss_{ctx.guild.id}'
        if scheduler.get_job(job_id):
            scheduler.reschedule_job(job_id, trigger='interval', minutes=interval)
        else:
            scheduler.add_job(process_and_send, 'interval', minutes=interval, args=[ctx.guild.id], id=job_id)

        await ctx.send(f'RSS processing interval has been changed to {interval} minutes')

    def update_scheduler_interval(self, minutes):
        if self.scheduler:
            self.scheduler.reschedule_job('rss_fetch_job', trigger='interval', minutes=minutes)

    async def send_discord_message(self, embed):
        config = load_config()
        channel_id = config.get('channel_id', None)
        if channel_id:
            channel = self.bot.get_channel(channel_id)
            if channel:
                await channel.send(embed=embed)
            else:
                print(f"频道ID {channel_id} 无效或机器人缺少权限")
        else:
            print(f"未设置频道")

    async def close(self):
        await self.bot.close()

# 配置Discord bot
async def setup_discord_bot():
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN 未设置")
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix='!', intents=intents)
    client = MyClient(bot)
    client.scheduler = scheduler
    await bot.add_cog(client)
    await bot.start(DISCORD_TOKEN)
    return client

# 格式化Discord消息
def format_discord_message(article, translated_title, translated_summary):
    source = article.get('source', '未知来源')
    link = article.get('link', '无链接')
    published_time = article.get('published') or article.get('pubDate') or article.get('updated', 'No date')
    images = article.get('images', [])
    
    # 提取并映射域名，目标语言为中文显示中文名，否则显示英文名
    parsed_url = urllib.parse.urlparse(link)
    domain = parsed_url.netloc
    source = DOMAIN_TO_SOURCE_MAPPING.get(domain, '未知来源')

    # 如果有发布时间，则解析它
    if published_time != 'No date':
        published_time = parse_published_time({'published': published_time})
    
    # discord限制单个嵌入消息字段超过1024字符
    translated_summary = article['summary']
    if len(translated_summary) > 1024:
        translated_summary = translated_summary[:1021] + '...'

    embed = discord.Embed(description=f"[{translated_title}]({link})")
    embed.add_field(name="Details", value=f"```fix\n{translated_summary}\n\nSource: {source}\nTime: {published_time}\n```", inline=False)

    if images:
        embed.set_image(url=images[0])

    return embed

# 处理RSS推送
async def process_article(article):
    link = article['link']
    config = load_config()
    processed_entries = config.get('processed_entries', {})

    if link in processed_entries:
        logger.info(f"文章已处理: {link}")
        return None

    processed_entries[link] = datetime.now()
    config['processed_entries'] = processed_entries
    save_config(config)

    language = config.get('language', 'zh-CN')

    try:
        translated_title = await translate_text_async(article['title'], language)
        translated_summary = await translate_text_async(article['summary'], language)
        embed_message = format_discord_message(article, translated_title, translated_summary)
        await discord_bot.send_discord_message(embed_message)
    except Exception as e:
        print(f"处理文章 {article.get('title', '无标题')} 时出错: {e}")

# 获取并发送RSS信息
async def fetch_and_send():
    config = load_config()
    rss_feeds = config['rss_feeds']

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_rss_feed_async(feed_url, session) for feed_url in rss_feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                print(f"获取RSS源时出错: {result}")
                continue

            article_tasks = [process_article(article) for article in result]
            await asyncio.gather(*article_tasks)

# 主函数
async def main():
    global loop  # 使用全局变量 loop
    loop = asyncio.get_event_loop()
    global discord_bot
    discord_bot = await setup_discord_bot()

    scheduler = AsyncIOScheduler()
    interval = load_config().get('interval', 720)
    scheduler.add_job(fetch_and_send, 'interval', minutes=interval, id='rss_fetch_job')
    scheduler.add_job(cleanup_processed_entries, 'interval', hours=24)

    scheduler.start()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    loop = asyncio.get_event_loop()
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(discord_bot.close())
        loop.close()

if __name__ == '__main__':
    asyncio.run(main())



