import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

import discord
from discord import app_commands
from discord.ext import tasks

# Ensure we're in the right directory
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

LOG_DIR = BASE_DIR / "data"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "discord_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("manga_bot")

from tracker import MangaTracker
from scraper import scrape, get_session

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN environment variable not set. Exiting.")
    sys.exit(1)

class MangaBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.tracker = MangaTracker()

    async def setup_hook(self):
        # Start the background task
        self.poll_manga.start()
        # Sync slash commands globally
        await self.tree.sync()
        logger.info("Slash commands synced.")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")

    @tasks.loop(minutes=3)
    async def poll_manga(self):
        entries = self.tracker.get_all()
        if not entries:
            logger.debug("No titles being tracked. Skipping poll.")
            return

        logger.info(f"Polling {len(entries)} tracked titles...")

        semaphore = asyncio.Semaphore(5)

        # Scrape concurrently using asyncio.gather and to_thread
        async def fetch_and_process(entry):
            session = get_session()
            try:
                # Run synchronous scrape function in a separate thread
                async with semaphore:
                    info = await asyncio.to_thread(scrape, entry.url, session)
                if info.latest_chapter is None:
                    self.tracker.record_error(entry.url)
                    return

                is_new = self.tracker.update_chapter(
                    entry.url,
                    info.latest_chapter,
                    info.title,
                    info.cover_url,
                )

                if is_new:
                    logger.info(f"NEW chapter for {info.title}: Ch.{info.latest_chapter.number}")
                    import re
                    from datetime import datetime
                    
                    display = entry.display_name or info.title
                    series_id = "Unknown"
                    chapter_id = "Unknown"
                    bot_username = "Manga Notifier"
                    bot_avatar = None
                    embed_color = discord.Color.green()
                    date_str = datetime.now().strftime("%Y.%m.%d")
                    
                    if "kuaikanmanhua.com" in entry.url:
                        bot_username = "Kuaikan Notifier"
                        bot_avatar = "https://t2.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=http://www.kuaikanmanhua.com&size=256"
                        embed_color = discord.Color.from_rgb(51, 127, 213)
                        
                        m_series = re.search(r'/topic/(\d+)', entry.url)
                        if m_series: series_id = m_series.group(1)
                        
                        m_ch = re.search(r'/comic/(\d+)', info.latest_chapter.url)
                        if m_ch: chapter_id = m_ch.group(1)
                        
                        date_str = datetime.now().strftime("%m.%d")
                        
                    elif "ac.qq.com" in entry.url:
                        bot_username = "Tencent Notifier"
                        bot_avatar = "https://t2.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=http://ac.qq.com&size=256"
                        embed_color = discord.Color.orange()
                        
                        m_series = re.search(r'/id/(\d+)', entry.url)
                        if m_series: series_id = m_series.group(1)
                        
                        m_ch = re.search(r'/cid/(\d+)', info.latest_chapter.url)
                        if m_ch: chapter_id = m_ch.group(1)

                    embed = discord.Embed(
                        title=f"New Chapter of {display}",
                        description=f"{info.latest_chapter.title}",
                        url=info.latest_chapter.url,
                        color=embed_color
                    )
                    embed.set_footer(text=f"Series ID: {series_id} | Chapter ID: {chapter_id} | Date: {date_str}")
                    
                    if info.cover_url:
                        embed.set_thumbnail(url=info.cover_url)
                        
                    if entry.subscribers:
                        for ch_id_str, ping_id in list(entry.subscribers.items()):
                            try:
                                target_channel = self.get_channel(int(ch_id_str))
                                if target_channel:
                                    content = ping_id if ping_id else None
                                    
                                    channel_alias = entry.aliases.get(ch_id_str)
                                    if channel_alias:
                                        custom_embed = embed.copy()
                                        custom_embed.title = f"New Chapter of {channel_alias}"
                                        await target_channel.send(content=content, embed=custom_embed)
                                    else:
                                        await target_channel.send(content=content, embed=embed)
                            except discord.Forbidden:
                                logger.error(f"Missing permissions for channel {ch_id_str}. Removing subscriber.")
                                self.tracker.remove_subscriber(entry.url, int(ch_id_str))
                            except discord.NotFound:
                                logger.error(f"Channel {ch_id_str} deleted. Removing subscriber.")
                                self.tracker.remove_subscriber(entry.url, int(ch_id_str))
                            except Exception as e:
                                logger.error(f"Failed to send specific channel notification for {entry.url} to {ch_id_str}: {e}")
                            
                            # Tiny delay to prevent triggering Discord's anti-spam rate limits
                            await asyncio.sleep(0.5)
                        
            except Exception as e:
                logger.error(f"Error scraping {entry.url}: {e}")
                self.tracker.record_error(entry.url)

        await asyncio.gather(*(fetch_and_process(entry) for entry in entries))
        logger.info("Polling cycle completed.")

    @poll_manga.before_loop
    async def before_poll(self):
        await self.wait_until_ready()


bot = MangaBot()

@bot.tree.command(name="track", description="Track a new manga")
@app_commands.describe(url="URL of the manga", display_name="Optional custom name", role_to_ping="Optional role to notify", user_to_ping="Optional user to notify")
async def track(interaction: discord.Interaction, url: str, display_name: str = "", role_to_ping: discord.Role = None, user_to_ping: discord.Member = None):
    already_tracked = bot.tracker.get(url) is not None

    await interaction.response.defer()
    
    if not already_tracked:
        from scraper import scrape, get_session
        session = get_session()
        try:
            info = await asyncio.to_thread(scrape, url, session)
            if not info or not info.latest_chapter:
                await interaction.followup.send("⚠️ I couldn't track that manga. Make sure the URL is valid and the site is supported!")
                return
        except Exception as e:
            logger.error(f"Initial scrape failed for {url}: {e}")
            await interaction.followup.send("⚠️ An error occurred while trying to verify that URL. Please check the URL and try again.")
            return

    ping_str = ""
    if role_to_ping:
        ping_str += role_to_ping.mention + " "
    if user_to_ping:
        ping_str += user_to_ping.mention + " "
    ping_str = ping_str.strip()

    entry = bot.tracker.add(url, display_name, interaction.channel_id, ping_str)
    
    if already_tracked:
        if entry.last_chapter_num > -1:
            embed = discord.Embed(
                title=entry.display_name,
                description=f"**Latest Chapter:** {entry.last_chapter_title}\n**Chapter Number:** {entry.last_chapter_num:g}",
                color=discord.Color.blurple(),
                url=entry.url
            )
            if entry.cover_url:
                embed.set_thumbnail(url=entry.cover_url)
            await interaction.followup.send(f"✅ Subscribed this channel to **{entry.display_name}**!", embed=embed)
        else:
            await interaction.followup.send(f"✅ Subscribed this channel to **{entry.display_name}**!")
        return
        
    await interaction.followup.send(f"✅ Now tracking: **{entry.display_name}**\n<{url}>")
    
    bot.tracker.update_chapter(url, info.latest_chapter, info.title, info.cover_url)
    embed = discord.Embed(
        title=info.title,
        description=f"**Latest Chapter:** {info.latest_chapter.title}\n**Chapter Number:** {info.latest_chapter.number:g}",
        color=discord.Color.blurple(),
        url=url
    )
    if info.cover_url:
        embed.set_thumbnail(url=info.cover_url)
        
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="untrack", description="Stop tracking a manga in this specific channel")
@app_commands.describe(url="URL of the manga to stop tracking")
async def untrack(interaction: discord.Interaction, url: str):
    entry = bot.tracker.get(url)
    if entry:
        name = entry.display_name
        ch_str = str(interaction.channel_id)
        if ch_str in entry.subscribers:
            bot.tracker.remove_subscriber(url, interaction.channel_id)
            
            # Check if any channels are still listening
            updated_entry = bot.tracker.get(url)
            if updated_entry and len(updated_entry.subscribers) == 0:
                bot.tracker.remove(url)
                await interaction.response.send_message(f"❌ Unsubscribed **{name}** from this channel. (Zero channels listening, completely deleted to save resources)")
            else:
                await interaction.response.send_message(f"❌ Unsubscribed **{name}** from this channel.")
        else:
            await interaction.response.send_message("⚠️ This channel is not subscribed to that manga.", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ That URL is not currently being tracked.", ephemeral=True)

OWNER_IDS = [881105899224186890, 1255605799422787738]

@bot.tree.command(name="global_untrack", description="Completely delete a manga from the bot's memory globally")
@app_commands.describe(url="URL of the manga to delete completely")
async def global_untrack(interaction: discord.Interaction, url: str):
    if interaction.user.id not in OWNER_IDS:
        await interaction.response.send_message("⛔ **Access Denied.** Only the bot owners can use this command.", ephemeral=True)
        return
        
    entry = bot.tracker.get(url)
    if entry:
        name = entry.display_name
        bot.tracker.remove(url)
        await interaction.response.send_message(f"🗑️ Permanently stopped tracking **{name}** everywhere.")
    else:
        await interaction.response.send_message("⚠️ That URL is not currently being tracked.", ephemeral=True)

@bot.tree.command(name="add_ping", description="Stack an additional role or user ping onto an existing tracked manga")
@app_commands.describe(url="URL of the tracked manga", role_to_ping="Role to add", user_to_ping="User to add")
async def add_ping(interaction: discord.Interaction, url: str, role_to_ping: discord.Role = None, user_to_ping: discord.Member = None):
    entry = bot.tracker.get(url)
    if not entry:
        await interaction.response.send_message("⚠️ That URL is not currently being tracked. Use `/track` first!", ephemeral=True)
        return
        
    ping_str = ""
    if role_to_ping:
        ping_str += role_to_ping.mention + " "
    if user_to_ping:
        ping_str += user_to_ping.mention + " "
    ping_str = ping_str.strip()
    
    if not ping_str:
        await interaction.response.send_message("⚠️ You must specify at least one role or user to add.", ephemeral=True)
        return
        
    bot.tracker.add_ping(url, interaction.channel_id, ping_str)
    await interaction.response.send_message(f"✅ Added pings to **{entry.display_name}** for this channel!")

@bot.tree.command(name="list", description="List all tracked manga for this channel categorized by site")
async def list_manga(interaction: discord.Interaction):
    all_entries = bot.tracker.get_all()
    ch_str = str(interaction.channel_id)
    entries = [e for e in all_entries if ch_str in e.subscribers]
    
    if not entries:
        await interaction.response.send_message("No manga are currently being tracked in this channel.")
        return
        
    tencent_entries = [e for e in entries if "ac.qq.com" in e.url]
    kuaikan_entries = [e for e in entries if "kuaikanmanhua.com" in e.url]
    other_entries = [e for e in entries if e not in tencent_entries and e not in kuaikan_entries]
    
    categories = [
        ("Tencent AC / QQ", tencent_entries, discord.Color.red()),
        ("Kuaikan", kuaikan_entries, discord.Color.orange()),
        ("Other Platforms", other_entries, discord.Color.blue())
    ]
    
    embeds = []
    
    for cat_name, cat_entries, color in categories:
        if not cat_entries:
            continue
            
        current_embed = discord.Embed(title=f"Tracked Manga: {cat_name}", color=color)
        
        for idx, entry in enumerate(cat_entries):
            if len(current_embed.fields) == 25:
                embeds.append(current_embed)
                current_embed = discord.Embed(title=f"Tracked Manga: {cat_name} (Cont.)", color=color)
                
            ch_text = f"Ch. {entry.last_chapter_num:g}" if entry.last_chapter_num > 0 else "Pending"
            status = entry.user_status.capitalize()
            current_embed.add_field(name=f"{idx+1}. {entry.display_name}", value=f"Latest: {ch_text} | Status: {status}\n[Link]({entry.url})", inline=False)
            
        if len(current_embed.fields) > 0:
            embeds.append(current_embed)
            
    # Discord allows up to 10 embeds per message.
    await interaction.response.send_message(embeds=embeds[:10])

@bot.tree.command(name="check", description="Force the bot to immediately check all series for new chapters")
async def force_check(interaction: discord.Interaction):
    await interaction.response.send_message("🔍 Manually starting a check cycle for all tracked series...")
    try:
        # Execute the underlying coroutine manually
        await bot.poll_manga.coro(bot)
        await interaction.followup.send("✅ Manual check cycle complete!")
    except Exception as e:
        logger.error(f"Manual check failed: {e}")
        await interaction.followup.send(f"❌ Error during manual check: {e}")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
