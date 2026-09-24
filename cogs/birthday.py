import os
import asyncio
import calendar
import sqlite3
from datetime import datetime, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks
import gspread

# ---------- Settings ----------
DEFAULT_TZ = 'America/Los_Angeles'   # used when a member's Timezones cell is blank
ANNOUNCE_HOUR = 8                    # announce at 8:40 AM in each member's own time
ANNOUNCE_MINUTE = 40
SHEET_TAB = 'Birthdays'

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'c4k3.db')

# Labels people tend to type, mapped to real zone names.
# Real names like 'Australia/Adelaide' always work and are preferred.
TZ_ALIASES = {
    'PST': 'America/Los_Angeles', 'PDT': 'America/Los_Angeles',
    'MST': 'America/Denver', 'MDT': 'America/Denver',
    'CST': 'America/Chicago', 'CDT': 'America/Chicago',
    'EST': 'America/New_York', 'EDT': 'America/New_York',
    'GERMANY': 'Europe/Berlin', 'UK': 'Europe/London', 'UTC': 'UTC',
    'AUSTRALIAN CENTRAL STANDARD TIME': 'Australia/Adelaide',
    'COSTA RICA': 'America/Costa_Rica',
}

DM_TEMPLATE = (
    "Happy birthday, {name}! 🎂\n\n"
    "The whole crew wanted to make sure you heard it from us today. "
    "We hope your day is full of good company, great food, and everything that makes you smile. "
    "Enjoy your special day! 🎁🎉\n\n"
    "🎈 C4-K3, delivering birthday wishes on behalf of the community"
)


class BirthdayCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sheet_id = os.getenv('BIRTHDAY_SHEET_ID')
        self.channel_id = int(os.getenv('ANNOUNCE_CHANNEL_ID', '0'))
        key_file = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', 'service_account.json')
        if not os.path.isabs(key_file):
            key_file = os.path.join(BASE_DIR, key_file)
        self.gc = gspread.service_account(filename=key_file)
        self.rows = []       # last good read of the Sheet
        self.warnings = []
        self.init_db()
        self.check_birthdays.start()

    # ---------- Storage ----------
    def init_db(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''CREATE TABLE IF NOT EXISTS announced
                        (user_id TEXT NOT NULL,
                         year INTEGER NOT NULL,
                         PRIMARY KEY (user_id, year))''')
        conn.commit()
        conn.close()

    # ---------- Sheet ----------
    def _read_sheet(self):
        ws = self.gc.open_by_key(self.sheet_id).worksheet(SHEET_TAB)
        return ws.get_all_records(numericise_ignore=['all'])

    def resolve_tz(self, raw):
        label = (raw or '').strip()
        if not label:
            return ZoneInfo(DEFAULT_TZ), None
        name = TZ_ALIASES.get(label.upper(), label)
        try:
            return ZoneInfo(name), None
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(DEFAULT_TZ), f"unknown timezone '{label}', using {DEFAULT_TZ}"

    async def refresh_rows(self):
        """Read the Sheet. On failure, keep the last good copy."""
        try:
            records = await asyncio.to_thread(self._read_sheet)
        except Exception as e:
            print(f"C4-K3: could not read the Sheet, keeping last copy ({e!r})")
            return False

        rows, warnings = [], []
        for i, rec in enumerate(records, start=2):
            name = str(rec.get('Name', '')).strip()
            uid = str(rec.get('Discord ID', '')).strip()
            bday = str(rec.get('Birthday', '')).strip()
            tz_raw = str(rec.get('Timezones', rec.get('Timezone', '')))
            dm = str(rec.get('DM', '')).strip().lower() in ('yes', 'y', 'true')

            if not (name or uid or bday):
                continue
            if not uid.isdigit():
                warnings.append(f"Row {i} ({name}): Discord ID missing or not a number")
                continue
            try:
                datetime.strptime(f"2000-{bday}", '%Y-%m-%d')   # 2000 is a leap year, so 02-29 passes
            except ValueError:
                warnings.append(f"Row {i} ({name}): birthday '{bday}' should be MM-DD")
                continue

            tz, tz_warning = self.resolve_tz(tz_raw)
            if tz_warning:
                warnings.append(f"Row {i} ({name}): {tz_warning}")

            rows.append({'name': name or 'someone special', 'id': uid,
                         'birthday': bday, 'tz': tz, 'dm': dm})

        self.rows, self.warnings = rows, warnings
        return True

    # ---------- Messages ----------
    @staticmethod
    def birthday_today(bday, local_now):
        today = local_now.strftime('%m-%d')
        if bday == '02-29' and not calendar.isleap(local_now.year):
            return today == '02-28'
        return bday == today

    @staticmethod
    def public_text(row):
        return (f"Hey there @everyone! I just wanted to remind you all that it's {row['name']}'s birthday today! "
                f"Let's make sure to wish them a happy one and make it a birthday to remember! 🎂🎈 "
                f"Have a great birthday <@{row['id']}>! Enjoy your special day! 🎁🎉")

    async def announce(self, channel, row):
        await channel.send(self.public_text(row),
                           allowed_mentions=discord.AllowedMentions(everyone=True, users=True))
        if row['dm']:
            try:
                uid = int(row['id'])
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                await user.send(DM_TEMPLATE.format(name=row['name']))
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                print(f"C4-K3: couldn't DM {row['name']} (DMs closed or user not found)")

    # ---------- Daily check ----------
    @tasks.loop(hours=1)
    async def check_birthdays(self):
        await self.refresh_rows()
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            print("C4-K3: announcement channel not found, check ANNOUNCE_CHANNEL_ID")
            return

        conn = sqlite3.connect(DB_PATH)
        try:
            for row in self.rows:
                now = datetime.now(row['tz'])
                if (now.hour, now.minute) < (ANNOUNCE_HOUR, ANNOUNCE_MINUTE):
                    continue
                if not self.birthday_today(row['birthday'], now):
                    continue
                if conn.execute('SELECT 1 FROM announced WHERE user_id = ? AND year = ?',
                                (row['id'], now.year)).fetchone():
                    continue
                try:
                    await self.announce(channel, row)
                except discord.HTTPException as e:
                    print(f"C4-K3: announcement for {row['name']} failed, will retry ({e})")
                    continue
                conn.execute('INSERT OR IGNORE INTO announced (user_id, year) VALUES (?, ?)',
                             (row['id'], now.year))
                conn.commit()
        finally:
            conn.close()

    @check_birthdays.before_loop
    async def before_check_birthdays(self):
        await self.bot.wait_until_ready()

    def cog_unload(self):
        self.check_birthdays.cancel()

    # ---------- Commands ----------
    @commands.command(name='upcoming')
    async def upcoming(self, ctx):
        """Show the next 10 birthdays"""
        await self.refresh_rows()
        today = datetime.now(ZoneInfo(DEFAULT_TZ)).date()
        items = []
        for row in self.rows:
            m, d = map(int, row['birthday'].split('-'))
            for year in (today.year, today.year + 1):
                try:
                    nxt = date(year, m, d)
                except ValueError:          # Feb 29 in a non-leap year
                    nxt = date(year, 2, 28)
                if nxt >= today:
                    break
            items.append(((nxt - today).days, nxt, row))
        items.sort(key=lambda x: x[0])

        embed = discord.Embed(title="🎂 Upcoming Birthdays", color=discord.Color.purple())
        for days, nxt, row in items[:10]:
            when = "**Today! 🎉**" if days == 0 else "Tomorrow" if days == 1 else f"in {days} days"
            embed.add_field(name=nxt.strftime('%B %d'), value=f"{row['name']} (<@{row['id']}>) {when}", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name='birthdaystatus')
    @commands.has_permissions(administrator=True)
    async def birthday_status(self, ctx):
        """Check the Sheet connection and any rows with problems (Admin only)"""
        ok = await self.refresh_rows()
        channel = self.bot.get_channel(self.channel_id)
        lines = [f"Sheet: {'✅ connected' if ok else '❌ could not read (using last copy)'}",
                 f"Birthdays loaded: {len(self.rows)}",
                 f"Announce channel: {channel.mention if channel else '❌ not found'}",
                 f"Default timezone: {DEFAULT_TZ}"]
        if self.warnings:
            lines.append("\n**Rows to fix:**")
            lines += [f"• {w}" for w in self.warnings]
        else:
            lines.append("No problems found.")
        await ctx.send("\n".join(lines))

    @commands.command(name='previewbirthday')
    @commands.has_permissions(administrator=True)
    async def preview_birthday(self, ctx, member: discord.User):
        """Preview someone's announcement and DM without pinging anyone (Admin only)"""
        await self.refresh_rows()
        row = next((r for r in self.rows if r['id'] == str(member.id)), None)
        if not row:
            await ctx.send("❌ That member isn't in the Birthdays sheet.")
            return
        none = discord.AllowedMentions.none()
        await ctx.send(f"**Public post** (8:40 AM, {row['tz'].key}, no pings in this preview):\n{self.public_text(row)}",
                       allowed_mentions=none)
        dm_note = DM_TEMPLATE.format(name=row['name']) if row['dm'] else "_DM is set to No_"
        await ctx.send(f"**DM:**\n{dm_note}", allowed_mentions=none)


async def setup(bot):
    await bot.add_cog(BirthdayCog(bot))
