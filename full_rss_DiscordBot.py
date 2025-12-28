import os
import signal
import json
from datetime import datetime, timedelta, timezone
import asyncio
import logging
import urllib.parse

import aiohttp
import feedparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from dateutil import parser
from dotenv import load_dotenv
import requests
import discord
from discord import Embed
from discord.ext import tasks, commands
from google.cloud import translate_v2 as translate
from deep_translator import DeeplTranslator
from tenacity import retry, stop_after_attempt, wait_fixed


DOMAIN_TO_SOURCE_MAPPING = {
    'cnn.com': {'zh': '有线电视新闻网', 'en': 'CNN'},
    'bbc.com': {'zh': '英国广播公司', 'en': 'BBC'},
    'wsj.com': {'zh': '华尔街日报', 'en': 'Wall Street Journal'},
    'foreignaffairs.com': {'zh': '外交事务', 'en': 'Foreign Affairs'},
    'ft.com': {'zh': '金融时报', 'en': 'Financial Times'},
    'reuters.com': {'zh': '路透社', 'en': 'Reuters'},
    'theatlantic.com': {'zh': '大西洋月刊', 'en': 'The Atlantic'},
    'economist.com': {'zh': '经济学人', 'en': 'The Economist'},
    'nytimes.com': {'zh': '纽约时报', 'en': 'The New York Times'},
    'bloomberg.com': {'zh': '彭博社', 'en': 'Bloomberg'},
    'theconversation.com': {'zh': '对话', 'en': 'The Conversation'},
    'nautil.us': {'zh': '鹦鹉螺', 'en': 'Nautil'},
    'longreads.com': {'zh': '长读', 'en': 'Longreads'},
    'nature.com': {'zh': '《自然》', 'en': 'Nature'},
    'science.org': {'zh': '《科学》', 'en': 'Science'},
    'eff.org': {'zh': '电子前哨基金会', 'en': 'EFF'},
    'ieee.org': {'zh': '电气和电子工程师协会', 'en': 'IEEE'},
    'brookings.edu': {'zh': '布鲁金斯学会', 'en': 'Brookings Institution'},
}

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

# 实例化一个 AsyncIOScheduler 对象，用于调度和执行异步任务。
scheduler = AsyncIOScheduler()
# 控制程序是否正在运行的标志，初始值为True
running = True

# 配置日志记录器
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 加载环境变量
load_dotenv()

# 读取环境变量
GOOGLE_APPLICATION_CREDENTIALS = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
GOOGLE_TRANSLATE_API_KEY = os.getenv('GOOGLE_TRANSLATE_API_KEY')
DEEPL_API_KEY = os.getenv('DEEPL_API_KEY')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

if not all([GOOGLE_APPLICATION_CREDENTIALS, GOOGLE_TRANSLATE_API_KEY, DEEPL_API_KEY, DISCORD_TOKEN]):
    raise ValueError("缺少必要的环境变量")

def signal_handler(signal, frame):
    logger.info("Received termination signal. Shutting down...")
    loop = asyncio.get_running_loop()
    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.stop()

# 配置常量
CONFIG_DIR = 'config'
ENTRY_LIFETIME = timedelta(days=7)
BOT_PREFIX = '!'
FEED_CHECK_INTERVAL = 60  # 默认每小时检查一次RSS源

# 创建配置文件夹
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR)

# 配置管理
class ConfigHandler:
    # 初始化配置管理器
    def __init__(self):
        self.configs = {}
        self.dirty_flags = {} # 记录配置是否发生变化
        self.load_all_configs()

    # 从配置目录加载所有服务器的配置文件，每个配置文件对应一个服务器，以服务器ID命名。
    def load_all_configs(self):
        for filename in os.listdir(CONFIG_DIR):
            if filename.endswith('.json'):
                with open(os.path.join(CONFIG_DIR, filename), 'r') as f:
                    try:
                        server_id = int(filename[:-5])
                        config = json.load(f)
                        if self.validate_config(config):
                            self.configs[server_id] = config
                            self.dirty_flags[server_id] = False
                        else:
                            logger.error(f"Invalid configuration in {filename}")
                    except Exception as e:
                        logger.error(f"Failed to load configuration from {filename}: {e}")

    def validate_config(self, config):
        required_keys = {'rss_sources', 'channel_id', 'processed_entries', 'target_language', 'interval'}
        return all(key in config for key in required_keys)
    
    # 保存指定服务器的配置到对应的JSON文件。
    def save_config(self, guild_id):  
        if self.dirty_flags.get(guild_id, False):  # 只有在配置发生变化时才保存
            with open(os.path.join(CONFIG_DIR, f'{guild_id}.json'), 'w') as f:
                json.dump(self.configs[guild_id], f, indent=4)
            self.dirty_flags[guild_id] = False

    # 获取指定服务器的配置,如果配置不存在，创建一个默认配置
    def get_config(self, guild_id):
        if guild_id not in self.configs:
            self.configs[guild_id] = self.create_default_config()
            self.dirty_flags[guild_id] = True
        return self.configs[guild_id]

    def create_default_config(self):
        return {
            'rss_sources': DEFAULT_RSS_FEEDS.copy(),
            'channel_id': None,
            'processed_entries': [],
            'target_language': 'zh',
            'interval': FEED_CHECK_INTERVAL,  # 以分钟为单位
            'etag': None,
            'last_modified': None
        }

    def add_rss_source(self, guild_id, rss_url):
        config = self.get_config(guild_id)
        if rss_url not in config['rss_sources']:
            config['rss_sources'].append(rss_url)
            self.dirty_flags[guild_id] = True
            self.save_config(guild_id)
            return True
        return False

    def remove_rss_source(self, guild_id, rss_url):
        config = self.get_config(guild_id)
        if rss_url in config['rss_sources']:
            config['rss_sources'].remove(rss_url)
            self.dirty_flags[guild_id] = True
            self.save_config(guild_id)
            return True
        return False

    def set_channel(self, guild_id, channel_id):
        config = self.get_config(guild_id)
        config['channel_id'] = channel_id
        self.dirty_flags[guild_id] = True
        self.save_config(guild_id)
        return True

    def get_channel(self, guild_id):
        config = self.get_config(guild_id)
        channel_id = config.get('channel_id')
        return int(channel_id) if channel_id else None

    def get_rss_sources(self, guild_id):
        config = self.get_config(guild_id)
        return config.get('rss_sources', [])
    
    def set_target_language(self, guild_id, language):
        config = self.get_config(guild_id)
        config['target_language'] = language
        self.dirty_flags[guild_id] = True
        self.save_config(guild_id)
        return True

    def get_target_language(self, guild_id):
        config = self.get_config(guild_id)
        return config.get('target_language', 'zh')  # 默认翻译到中文
    
    # 设置指定服务器的RSS处理间隔时间
    def set_interval(self, guild_id, interval):
        config = self.get_config(guild_id)
        config['interval'] = interval
        self.dirty_flags[guild_id] = True
        self.save_config(guild_id)
        return True

    def get_interval(self, guild_id):
        config = self.get_config(guild_id)
        return config.get('interval', 60)  # 默认间隔时间为60分钟

# 解析并格式化RSS条目的发布时间，统一转换为UTC时间。
def parse_published_time(entry):
    if 'published_parsed' in entry and entry.published_parsed:
        published_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    elif 'published' in entry:
        try:
            published_time = parser.parse(entry.published).astimezone(timezone.utc)
        except (ValueError, TypeError):
            published_time = None
    else:
        published_time = None
    
    if published_time:
        return published_time.strftime('%Y-%m-%d %H:%M:%S %Z')
    else:
        return 'No date'

# 清理旧条目
def clean_old_entries(config, guild_id):
    now = datetime.now(timezone.utc)
    config['processed_entries'] = [
        entry for entry in config['processed_entries'] if now - datetime.fromisoformat(entry['timestamp']) < ENTRY_LIFETIME
    ]
    config_handler.save_config(guild_id)  # 清理后立即保存配置

# Google Translation语言代码
valid_google_languages = [
    "af", "sq", "am", "ar", "hy", "az", "eu", "be", "bn", "bs", "bg", "ca", "ceb",
    "zh", "zh-CN", "zh-TW", "co", "hr", "cs", "da", "nl", "en", "eo", "et", "fi", "fr",
    "fy", "gl", "ka", "de", "el", "gu", "ht", "ha", "haw", "he", "hi", "hmn", "hu",
    "is", "ig", "id", "ga", "it", "ja", "jv", "kn", "kk", "km", "rw", "ko", "ku",
    "ky", "lo", "la", "lv", "lt", "lb", "mk", "mg", "ms", "ml", "mt", "mi", "mr",
    "mn", "my", "ne", "no", "ny", "or", "ps", "fa", "pl", "pt", "pa", "ro", "ru",
    "sm", "gd", "sr", "st", "sn", "sd", "si", "sk", "sl", "so", "es", "su", "sw",
    "sv", "tl", "tg", "ta", "tt", "te", "th", "tr", "tk", "uk", "ur", "ug", "uz",
    "vi", "cy", "xh", "yi", "yo", "zu"
]
# 翻译类管理
class TranslationService:
    def __init__(self, google_application_credentials, deepl_api_key):
        # 初始化翻译服务，加载Google和DeepL的API凭证
        self.deepl_api_key = deepl_api_key
        self.client = translate.Client.from_service_account_json(google_application_credentials)
        self.session = None

    async def init_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) # 取消SSL验证

    async def translate(self, text, language='zh-CN', use_google=True, retries=3, delay=2):
        await self.init_session() # 确保aiohttp客户端会话已初始化

        try:
            if use_google:
                return self.translate_with_google(text, language) # 使用Google翻译
            else:
                return await self.translate_with_deepl(text, language, retries, delay) # 使用DeepL翻译
        except Exception as e:
            logger.error(f"Error while translating text: {e}")
            logger.debug(f"Detailed error info: {e}", exc_info=True)  # 添加详细错误信息
            return f"Translation failed: {e}"
        
    # 使用Google翻译API翻译文本
    def translate_with_google(self, text, language):
        try:
            logger.info(f"Translating with Google: {text} to {language}")
            result = self.client.translate(text, target_language=language)
            return result['translatedText']
        except Exception as e:
            logger.error(f"Google Translation error: {e}")
            logger.debug(f"Detailed error info: {e}", exc_info=True)  # 添加详细错误信息
            raise
    # 使用DeepL翻译API翻译文本，带有重试机制
    async def translate_with_deepl(self, text, language, retries, delay):
        url, params = self.get_translation_params(text, language, use_google=False)

        for attempt in range(retries):
            try:
                async with self.session.get(url, params=params) as response:
                    response.raise_for_status()
                    data = await response.json()
                    return self.extract_translation(data, use_google=False)
            except aiohttp.ClientError as e:
                if attempt < retries - 1:
                    logger.warning(f"Network error during translation: {e}. Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Failed to translate text after {retries} attempts: {e}")
                    logger.debug(f"Request URL: {url}")
                    logger.debug(f"Request Params: {params}")
                    logger.debug(f"Detailed error info: {e}", exc_info=True)  # 添加详细错误信息
                    return f"Failed to translate text after {retries} attempts: {e}"
    # 根据使用的翻译服务生成请求参数
    def get_translation_params(self, text, language, use_google):
        if use_google:
            url = "https://translation.googleapis.com/language/translate/v2"
            params = {
                'q': text,
                'target': language,
                # 'key': self.google_api_key
            }
        else:
            url = "https://api-free.deepl.com/v2/translate"
            params = {
                'auth_key': self.deepl_api_key,
                'text': text,
                'target_lang': language.upper()
            }
        return url, params
    # 提取翻译结果
    def extract_translation(self, data, use_google):
        if use_google:
            return data['data']['translations'][0]['translatedText']
        else:
            return data['translations'][0]['text']
    # 关闭aiohttp客户端会话
    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

# 清理HTML标签并提取图片
def clean_html_and_extract_images(raw_html):
    if not raw_html.strip().startswith('<'):
        return raw_html, []
    
    soup = BeautifulSoup(raw_html, 'html.parser')
    text = soup.get_text()
    images = [img['src'] for img in soup.find_all('img') if 'src' in img.attrs]
    return text, images

# 格式化Discord消息
def format_discord_message(article, translated_title, translated_summary, target_language):
    source = article.get('source', 'Unknown source')  # 获取RSS推送中的原始source名称
    link = article.get('link', 'No link')
    images = article.get('images', [])  # 确保images从article中获取
    published_time = article.get('published') or article.get('pubDate') or article.get('updated', 'No date')

    # 如果有发布时间，则解析它
    if published_time != 'No date':
        published_time = parse_published_time({'published': published_time})

    # 提取并映射域名，目标语言为中文显示中文名，否则显示英文名
    parsed_url = urllib.parse.urlparse(link)
    domain = parsed_url.netloc
    source_info = DOMAIN_TO_SOURCE_MAPPING.get(domain, {'zh': source, 'en': source})
    source = source_info['zh'] if target_language == 'zh' else source_info['en']


    # discord限制单个嵌入消息字段超过1024字符
    translated_summary = article['summary']
    if len(translated_summary) > 1024:
        translated_summary = translated_summary[:1021] + '...'

    embed = discord.Embed(description=f"[{translated_title}]({link})")
    embed.add_field(name="Details", value=f"```fix\n{translated_summary}\n\nSource: {source}\nTime: {published_time}\n```", inline=False)

    if images:
        embed.set_image(url=images[0])

    return embed

# 获取RSS源并返回解析后的条目和元数据（etag和last_modified）
async def fetch_rss_feed(session, url, etag=None, last_modified=None):
    headers = {'User-Agent': 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124'}
    if etag:
        headers['If-None-Match'] = etag # 设置etag头用于缓存控制
    if last_modified:
        headers['If-Modified-Since'] = last_modified # 设置最后修改时间头用于缓存控制

    async with session.get(url, headers=headers, timeout=20) as response:
        if response.status == 304:   # 如果状态码是304，表示内容没有更新
            return [], etag, last_modified

        response.raise_for_status()  # 检查响应状态码，如果不是成功状态码，则抛出异常
        content = await response.text()  # 获取响应内容
        feed = feedparser.parse(content)  # 解析RSS内容

        etag = response.headers.get('ETag')  # 获取响应头中的etag值
        last_modified = response.headers.get('Last-Modified')  # 获取响应头中的last_modified值

        return feed.entries, etag, last_modified  # 返回解析后的条目和新的etag、last_modified
    
# 翻译RSS条目中的标题和摘要
async def translate_rss_entries(entries, target_language):
    translated_entries = []
    for entry in entries:
        translated_title = await translator.translate(entry.title, target_language)
        summary, images = clean_html_and_extract_images(entry.get('summary') or entry.get('description') or entry.get('content', [{}])[0].get('value', 'No summary'))
        translated_summary = await translator.translate(summary, target_language)
        entry_id = entry.get('id') or entry.get('guid') or entry.get('link') or f"{entry.title}-{entry.published}"
        # 构建翻译后的条目字典
        translated_entries.append({
            'id': entry_id,
            'title': translated_title,
            'summary': translated_summary,
            'link': entry.link,
            'images': images,
            'source': entry.get('source', 'Unknown source'),
            'published': entry.get('published') or entry.get('pubDate') or entry.get('updated', 'No date')
        })
    return translated_entries

# 处理并发送翻译后的条目到指定的Discord频道
async def process_and_send(guild_id):
    logger.info(f"Starting process_and_send for guild {guild_id}")
    # 获取当前配置
    config = config_handler.get_config(guild_id)
    # 获取频道ID
    channel_id = config_handler.get_channel(guild_id)
    if not channel_id:
        logger.warning(f"Channel ID is not set for guild {guild_id}. Skipping RSS processing.")
        return
    
    # 获取频道对象
    channel = bot.get_channel(channel_id)
    if not channel:
        logger.warning(f"Channel with ID {channel_id} not found for guild {guild_id}.")
        return

    clean_old_entries(config, guild_id)
    target_language = config_handler.get_target_language(guild_id)
    rss_urls = config['rss_sources']  # 获取RSS源URL列表

    # 创建异步HTTP会话
    async with aiohttp.ClientSession() as session:
        # 为每个RSS URL创建任务
        tasks = [fetch_rss_feed(session, url, config.get('etag'), config.get('last_modified')) for url in rss_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)  # 并发执行任务，获取结果

    all_entries = []
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Error in fetching results: {result}")
            continue
        entries, etag, last_modified = result
        if entries:
            config['etag'] = etag
            config['last_modified'] = last_modified
        all_entries.extend(entries)

    # 并发翻译所有获取到的条目
    translated_entries = await translate_rss_entries(all_entries, target_language)

    # 过滤掉已经处理过的条目
    new_entries = [entry for entry in translated_entries if entry['id'] not in [e['id'] for e in config_handler.get_config(guild_id)['processed_entries']]]

    for entry in new_entries:
        # 格式化Discord消息
        embed_message = format_discord_message(entry, entry['title'], entry['summary'], target_language)
        try:
            await bot.send_discord_message(bot, channel, embed_message)  # 发送消息到指定频道
            # 添加到已处理条目列表
            config_handler.get_config(guild_id)['processed_entries'].append({
                'id': entry['id'],
                'link': entry['link'],
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
        except Exception as e:
            logger.error(f"Failed to send message for entry {entry['link']}: {e}")

    logger.info(f"Completed process_and_send for guild {guild_id}")


# Discord bot类
class MyClient(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduler = None

    @commands.command(name='add_rss')
    async def add_rss(self, ctx, rss_url):
        if self.is_guild(ctx):
            if config_handler.add_rss_source(ctx.guild.id, rss_url):
                await ctx.send(f'RSS feed {rss_url} added')
            else:
                await ctx.send('The RSS feed is invalid or already exists')

    @commands.command(name='remove_rss')
    async def remove_rss(self, ctx, rss_url):
        if self.is_guild(ctx):
            if config_handler.remove_rss_source(ctx.guild.id, rss_url):
                await ctx.send(f'RSS feed {rss_url} Removed')
            else:
                await ctx.send('RSS feed not found.')

    @commands.command(name='set_channel')
    async def set_channel(self, ctx, channel: discord.TextChannel = None):
        if self.is_guild(ctx):
            if channel is None:
                await ctx.send('You must specify a channel.')
                return

            if config_handler.set_channel(ctx.guild.id, channel.id):
                await ctx.send(f'Channel is set to {channel.mention}')
            else:
                await ctx.send('Failed to set channel.')

    @commands.command(name='list_rss')
    async def list_rss(self, ctx):
        if self.is_guild(ctx):
            rss_sources = config_handler.get_rss_sources(ctx.guild.id)
            if rss_sources:
                await ctx.send('Current RSS feed list:\n' + '\n'.join(rss_sources))
            else:
                await ctx.send('No RSS feeds.')

    @commands.command(name='set_interval')
    async def set_interval(self, ctx, interval: int = None):
        if self.is_guild(ctx):
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
        config = config_handler.get_config()
        channel_id = config.get('channel_id', None)
        if channel_id:
            channel = bot.get_channel(channel_id)
            if channel:
                await channel.send(embed=embed)
            else:
                print(f"频道ID {channel_id} 无效或机器人缺少权限")
        else:
            print(f"未设置频道")

    async def close(self):
        await self.bot.close()

    def is_guild(self, ctx):
        if ctx.guild is None:
            ctx.send('This command can only be used in a server.')
            return False
        return True

# 设置调度器
def setup_scheduler():
    # 检查调度器是否已经在运行，如果没有，则启动调度器
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")
    else:
        logger.info("Scheduler is already running")
    # 遍历所有的 Discord 服务器，为每个服务器（guild）设置定时任务
    for guild in bot.guilds:
        interval = config_handler.get_interval(guild.id)
        job_id = f'process_rss_{guild.id}' # 为每个服务器创建唯一的任务 ID
        existing_job = scheduler.get_job(job_id)  # 检查调度器中是否已经存在该任务
        if existing_job:
            # 如果任务已经存在，并且其间隔时间与当前配置不同，则重新调度任务
            if existing_job.trigger.interval.total_seconds() != interval * 60:
                logger.info(f"Rescheduling job for guild {guild.id} with new interval {interval} minutes")
                scheduler.reschedule_job(job_id, trigger='interval', minutes=interval)  # 重新设置任务间隔时间
            else:
                logger.info(f"Job for guild {guild.id} already exists with the correct interval")
        else:
            # 如果任务不存在，则为该服务器添加一个新的定时任务
            logger.info(f"Adding job for guild {guild.id} with interval {interval} minutes")
            # 添加任务：调用函数 process_and_send，并传递服务器ID作为参数，设置任务的间隔时间
            scheduler.add_job(process_and_send, 'interval', minutes=interval, args=[guild.id], id=job_id)


# 实例化配置类
config_handler = ConfigHandler()
# 实例化翻译服务
translator = TranslationService(GOOGLE_APPLICATION_CREDENTIALS, DEEPL_API_KEY)
# 配置Discord bot的意图
intents = discord.Intents.default()
intents.message_content = True  # 确保机器人能够读取消息内容
# 实例化并配置一个 commands.Bot 对象，表示Discord机器人。
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# 初始化Discord bot
async def setup_discord_bot():
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN 未设置")
    client = MyClient(bot)
    client.scheduler = scheduler  # 将全局调度器scheduler赋值给 client.scheduler
    await bot.add_cog(client)
    await bot.start(DISCORD_TOKEN)
    return client

# 启动bot并开始处理RSS
async def run_bot():
    await translator.init_session()  # 初始化翻译服务的客户端会话
    setup_scheduler()  # 启动调度器
    await bot.start(DISCORD_TOKEN)  # 启动并登录bot

async def main():
    try:
        await setup_discord_bot()  # 初始化并设置bot
        await run_bot()  # 运行bot
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        await shutdown()

# 关闭脚本
async def shutdown():
    logger.info("Received termination signal. Shutting down...")
    tasks = [task for task in asyncio.all_tasks() if not task.done()]
    for task in tasks:
        task.cancel()  # 取消所有任务
    await asyncio.gather(*tasks, return_exceptions=True)
    await translator.close()  # 关闭翻译服务
    await bot.close()  # 确保bot安全退出

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass