import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='c!', intents=intents)


@bot.event
async def on_ready():
    print(f"✓ C4-K3 online as {bot.user}")


async def main():
    async with bot:
        await bot.load_extension('cogs.birthday')
        await bot.start(os.getenv('DISCORD_TOKEN'))

asyncio.run(main())
