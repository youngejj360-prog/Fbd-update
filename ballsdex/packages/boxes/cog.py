import discord
from discord.ext import commands
from discord import app_commands
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import io
from tortoise.expressions import RawSQL, Q
import random
from discord import Embed, Color, File
from tortoise import models, fields
from PIL import Image, ImageFilter, ImageEnhance, ImageDraw, ImageFont
from discord.ui import View
import asyncio
from tortoise.exceptions import DoesNotExist
import logging

logger = logging.getLogger(__name__)

from ballsdex.core.utils.transformers import (
    BallTransform,
    SpecialTransform,
    BallEnabledTransform,
    BallInstanceTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)
from ballsdex.core.models import (
    Ball,
    balls,
    BallInstance,
    BlacklistedGuild,
    BlacklistedID,
    GuildConfig,
    Player,
    Trade,
    TradeObject,
    Special,
)
from ballsdex.settings import settings
from ballsdex.core.bot import BallsDexBot
import ballsdex.packages.config.components as Components
from ballsdex.core.image_generator.image_gen import draw_card
from io import BytesIO

# Credits
# -------
# - crashtestalex
# - hippopotis
# - dot_zz
# -------

# Track last claim times
last_daily_times = {}
last_weekly_times = {}
wallet_balance = defaultdict(int)
packly_pool = defaultdict(int)
active_multipackly: set[str] = set()
multipackly_locks: dict[str, asyncio.Lock] = {}

# Custom daily usage tracking for the /daily command (3 uses per day)
daily_usage_tracking = {}

# Dynamic Pack System Tracking
pack_cooldown_tracking = {}      # Stores {user_id: datetime}
pack_daily_limit_tracking = {}   # Stores {user_id: {"count": int, "reset_time": datetime}}

# Role Configuration Map
ROLE_CONFIG = {
    "premiumPatreon": {
        "dailyLimit": 1000000,
        "cooldownSeconds": 6,
        "roleIds": [1514426022429458663]
    },
    "paidAmbassador": {
        "dailyLimit": 450000,
        "cooldownSeconds": 14,
        "roleIds": [1514424682084831364]
    },
    "ambassador": {
        "dailyLimit": 250000,
        "cooldownSeconds": 26,
        "roleIds": [1379106307319136266]
    },
    "booster": {
        "dailyLimit": 150000,
        "cooldownSeconds": 43,
        "roleIds": [1478300538020958249, 1263953046573158462]
    },
    "default": {
        "dailyLimit": 50000,
        "cooldownSeconds": 120,
        "roleIds": []
    }
}

# Owners who can give packs
ownersid = {
    1096501882224136222,
    837377495530602516,
    784414771993903125,
    749658746535280771,
    1231339940382638080,
    1184739489315299339,
    257972292645027841,
    1250399605733195906,
    596428982694707240,
}

# Cooldowns
DAILY_COOLDOWN = timedelta(hours=24)
WEEKLY_COOLDOWN = timedelta(days=7)
gamble_cooldowns = {}


class SkipView(View):
    """View for skip button during multipack opening"""

    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.skipped = False
        self.user_id = user_id
        self.skip_event = asyncio.Event()

    @discord.ui.button(label="Skip Animation", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Only the person opening the packs can skip!", ephemeral=True
            )
            return

        self.skipped = True
        self.skip_event.set()
        button.label = "Skipped ✓"
        button.disabled = True
        button.style = discord.ButtonStyle.success

        current_embed = interaction.message.embeds[0]
        current_embed.description = "⏩ **Animation skipped! Showing final results...**"
        current_embed.color = Color.green()

        await interaction.response.edit_message(embed=current_embed, view=self)
        self.stop()


class Claim(commands.GroupCog, name="packs"):
    """
    A little simple daily pack!
    """

    def __init__(self, bot: BallsDexBot):
        self.bot = bot
        self.bot_tutorial_seen = set()
        self.bot_walletturorial_seen = set()
        self.active_users = set()
        super().__init__()

    owners = app_commands.Group(name="owners", description="Owner-only commands")

    def get_user_pack_tier(self, user: discord.User | discord.Member) -> dict:
        """Determines the user's tier based on their highest qualifying Discord role."""
        if not isinstance(user, discord.Member):
            return ROLE_CONFIG["default"]
        
        user_role_ids = {role.id for role in user.roles}
        for tier_name in ["premiumPatreon", "paidAmbassador", "ambassador", "booster"]:
            tier = ROLE_CONFIG[tier_name]
            if any(role_id in user_role_ids for role_id in tier["roleIds"]):
                return tier
        return ROLE_CONFIG["default"]

    async def get_random_special(self) -> Special | None:
        now = datetime.now(timezone.utc)
        try:
            active_specials = await Special.filter(
                Q(start_date__isnull=True) | Q(start_date__lte=now),
                Q(end_date__isnull=True) | Q(end_date__gte=now),
                hidden=False,
            ).all()
        except:
            active_specials = await Special.all()

        if not active_specials:
            return None

        for special in active_specials:
            if random.random() < special.rarity:
                return special
        return None

    async def _start_worker_manager(self):
        while True:
            user_id, packs, interaction = await self.pack_queue.get()
            asyncio.create_task(
                self._process_multipackly_and_clean_up(user_id, packs, interaction)
            )
            self.pack_queue.task_done()

    async def _process_multipackly(self, user_id, packs, interaction):
        return await self._process_multipackly_and_clean_up(user_id, packs, interaction)

    async def _process_multipackly_and_clean_up(self, user_id, packs, interaction):
        try:
            await self._process_multipackly(user_id, packs, interaction)
        except Exception as e:
            logger.error(f"Error processing multipackly for user {user_id}: {e}")
            try:
                await interaction.followup.send(
                    f"❌ An unexpected error occurred while opening your packs.", ephemeral=True
                )
            except discord.NotFound:
                logger.warning(f"Could not send error message for user {user_id}")
        finally:
            self.active_users.discard(user_id)

    async def get_random_ball(self, player: Player) -> Ball | None:
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(rarity__gte=0.03, rarity__lte=30.0, enabled=True).all()

        if not all_balls:
            return None

        weighted_choices = []
        for ball in all_balls:
            base_weight = 1

            if 5.0 <= ball.rarity <= 30.0:
                rarity_weight = 1600 
            elif 2.5 <= ball.rarity < 5.0:
                rarity_weight = 600
            elif 1.5 <= ball.rarity < 2.5:
                rarity_weight = 300 
            elif 0.5 <= ball.rarity < 1.5:
                rarity_weight = 100 
            elif 0.1 < ball.rarity < 0.5:
                rarity_weight = 30 
            elif 0.01 <= ball.rarity <= 0.1:
                rarity_weight = 20 
            else:
                rarity_weight = 1

            final_weight = base_weight * rarity_weight
            weighted_choices.append((ball, final_weight))

        choices = []
        for ball, weight in weighted_choices:
            choices.extend([ball] * int(weight))

        if not choices:
            return None
        return random.choice(choices)

    async def safe_send_pinged_embed(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        embed: discord.Embed,
        *,
        content_mention: str | None = None,
    ):
        allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
        content = content_mention or user.mention

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    content=content, embed=embed, allowed_mentions=allowed
                )
                return
        except discord.InteractionResponded:
            pass
        except Exception:
            pass

        try:
            await interaction.followup.send(content=content, embed=embed, allowed_mentions=allowed)
            return
        except discord.NotFound:
            pass
        except Exception:
            pass

        try:
            if interaction.channel:
                await interaction.channel.send(
                    content=content, embed=embed, allowed_mentions=allowed
                )
                return
        except Exception:
            pass

    def check_daily_usage(self, user_id: str) -> tuple[bool, int]:
        now = datetime.now(timezone.utc)

        if user_id not in daily_usage_tracking:
            daily_usage_tracking[user_id] = {"count": 0, "first_use": now}
            return True, 3

        user_data = daily_usage_tracking[user_id]
        time_since_first_use = now - user_data["first_use"]

        if time_since_first_use >= DAILY_COOLDOWN:
            daily_usage_tracking[user_id] = {"count": 0, "first_use": now}
            return True, 3

        if user_data["count"] >= 3:
            return False, 0

        remaining = 3 - user_data["count"]
        return True, remaining

    def increment_daily_usage(self, user_id: str):
        if user_id in daily_usage_tracking:
            daily_usage_tracking[user_id]["count"] += 1

    def get_daily_cooldown_remaining(self, user_id: str) -> timedelta | None:
        if user_id not in daily_usage_tracking:
            return None

        user_data = daily_usage_tracking[user_id]
        if user_data["count"] < 3:
            return None

        now = datetime.now(timezone.utc)
        cooldown_end = user_data["first_use"] + DAILY_COOLDOWN

        if now >= cooldown_end:
            return None
        return cooldown_end - now

    async def getdasigmaballmate(self, player: Player) -> Ball | None:
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(rarity__gte=0.03, rarity__lte=5.0, enabled=True).all()

        if not all_balls:
            return None

        weighted_choices = []
        for ball in all_balls:
            base_weight = 1

            if ball.rarity >= 4.5: 
                rarity_weight = 900
            elif ball.rarity >= 1.5:
                rarity_weight = 500
            elif ball.rarity >= 0.5: 
                rarity_weight = 200
            else: 
                rarity_weight = 20

            final_weight = base_weight * rarity_weight
            weighted_choices.append((ball, final_weight))

        choices = []
        for ball, weight in weighted_choices:
            choices.extend([ball] * int(weight))

        if not choices:
            return None
        return random.choice(choices)

    def format_special_emoji(self, special: Special | None) -> str:
        if not special:
            return ""

        if special.emoji:
            try:
                emoji_id = int(special.emoji)
                emoji = self.bot.get_emoji(emoji_id) or "⚡"
                return str(emoji)
            except ValueError:
                return special.emoji
        return "⚡"

    @app_commands.command(
        name="daily", description="Claim your daily Footballer! (3 uses per day)"
    )
    async def daily(self, interaction: discord.Interaction["BallsDexBot"]):
        user_id = str(interaction.user.id)

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.", ephemeral=True
            )
            return

        can_use, remaining_uses = self.check_daily_usage(user_id)

        if not can_use:
            cooldown_remaining = self.get_daily_cooldown_remaining(user_id)
            if cooldown_remaining:
                hours = int(cooldown_remaining.total_seconds() // 3600)
                minutes = int((cooldown_remaining.total_seconds() % 3600) // 60)
                await interaction.response.send_message(
                    f"⏰ You've used all 3 daily packs! Come back in {hours}h {minutes}m for your next set of daily packs.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()
        self.increment_daily_usage(user_id)
        _, new_remaining = self.check_daily_usage(user_id)
        player, _ = await Player.get_or_create(discord_id=str(user_id))
        ball = await self.get_random_ball(player)

        if not ball:
            await interaction.followup.send("No balls are available.", ephemeral=True)
            return

        special = await self.get_random_special()
        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
            special=special,
        )

        walkout_embed = Embed(title="🎉 Daily Pack Opening...", color=Color.dark_gray())
        remaining_text = (
            f"Remaining daily uses: {new_remaining}/3"
            if new_remaining > 0
            else "All daily uses consumed! Come back tomorrow."
        )
        walkout_embed.set_footer(text=remaining_text)
        msg = await interaction.followup.send(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        if special:
            await asyncio.sleep(1.5)
            special_emoji = ""
            if special.emoji:
                try:
                    emoji_id = int(special.emoji)
                    special_emoji = self.bot.get_emoji(emoji_id) or "⚡"
                except ValueError:
                    special_emoji = special.emoji
            else:
                special_emoji = "⚡"

            walkout_embed.description += f"\n{special_emoji} **Special:** **{special.name}**"
            await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description += (
            f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        )
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        special_text = f" with **{special.name}** special!" if special else "!"
        walkout_embed.title = f"🎁 You got **{ball.country}**{special_text}"
        walkout_embed.color = Color.gold()

        content, file, view = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(
            name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
        )

        await msg.edit(embed=walkout_embed, attachments=[file], view=view)
        file.close()

        log_channel_id = 1361522228021297404 
        log_channel = self.bot.get_channel(log_channel_id)
        account_created = interaction.user.created_at.strftime("%Y-%m-%d %H:%M:%S")
        special_info = f" | Special: {special.name}" if special else ""

        if log_channel:
            await log_channel.send(
                f"**{interaction.user.mention}** claimed a Daily pack and got **{ball.country}** (Use {3-new_remaining}/3){special_info}\n"
                f"• Rarity: `{ball.rarity}` 💖 `{instance.attack_bonus}` ⚽ `{instance.health_bonus}`\n"
                f"• Footballer ID: `#{ball.pk:0X}`\n"
                f"• Account created: `{account_created}`"
            )

        logger.info(
            f"[DAILY PACK] {interaction.user} ({interaction.user.id}) received {ball.country} "
            f"(Rarity: {ball.rarity}) | Account created: {account_created} | "
            f"Daily use {3-new_remaining}/3 | Footballer ID: `#{ball.pk:0X}`{special_info}"
        )

    @app_commands.command(name="weekly", description="Claim your weekly Footballer!")
    @app_commands.checks.cooldown(1, 604800, key=lambda i: i.user.id)
    async def weekly(self, interaction: discord.Interaction["BallsDexBot"]):
        user_id = str(interaction.user.id)

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.", ephemeral=True
            )
            return

        player, _ = await Player.get_or_create(discord_id=str(interaction.user.id))
        ball = await self.getdasigmaballmate(player)

        if not ball:
            await interaction.response.send_message("No balls are available.", ephemeral=True)
            return

        special = await self.get_random_special()
        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
            special=special,
        )

        walkout_embed = discord.Embed(
            title="🎉 Weekly Pack Opening...", color=discord.Color.dark_gray()
        )
        walkout_embed.set_footer(text="Come back in 7 days for your next claim!")
        await interaction.response.defer()
        msg = await interaction.followup.send(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        if special:
            await asyncio.sleep(1.5)
            special_emoji = ""
            if special.emoji:
                try:
                    emoji_id = int(special.emoji)
                    special_emoji = self.bot.get_emoji(emoji_id) or "⚡"
                except ValueError:
                    special_emoji = special.emoji
            else:
                special_emoji = "⚡"

            walkout_embed.description += f"\n{special_emoji} **Special:** **{special.name}**"
            await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description += (
            f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        )
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        special_text = f" with **{special.name}** special!" if special else "!"
        walkout_embed.title = f"🎁 You got **{ball.country}**{special_text}"
        walkout_embed.color = discord.Color.from_rgb(229, 255, 0) 

        content, file, view = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(
            name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
        )

        await msg.edit(embed=walkout_embed, attachments=[file], view=view)
        file.close()

        log_channel_id = 1361522228021297404 
        log_channel = self.bot.get_channel(log_channel_id)
        account_created = interaction.user.created_at.strftime("%Y-%m-%d %H:%M:%S")
        special_info = f" | Special: {special.name}" if special else ""

        if log_channel:
            await log_channel.send(
                f"**{interaction.user.mention}** claimed a Weekly pack and got **{ball.country}**{special_info}\n"
                f"• Rarity: `{ball.rarity}` 💖 `{instance.attack_bonus}` ⚽ `{instance.health_bonus}`\n"
                f"Footballer ID: `#{ball.pk:0X}`\n"
                f"• Account created: `{account_created}`"
            )

        logger.info(
            f"[WEEKLY PACK] {interaction.user} ({interaction.user.id}) received {ball.country} "
            f"(Rarity: {ball.rarity}) | Account created: {account_created} | "
            f"Footballer ID: `#{ball.pk:0X}`{special_info}"
        )

    @app_commands.command(name="packly", description="Claim your footballer from the packly!")
    async def packly(self, interaction: discord.Interaction["BallsDexBot"]):
        user_id = str(interaction.user.id)
        u_id_int = interaction.user.id

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.", ephemeral=True
            )
            return

        if user_id not in wallet_balance:
            wallet_balance[user_id] = 1 

        if wallet_balance[user_id] < 1:
            await interaction.response.send_message("You don't have enough packs!", ephemeral=True)
            return

        # --- DYNAMIC COOLDOWN & LIMIT VERIFICATION ---
        now = datetime.now(timezone.utc)
        tier = self.get_user_pack_tier(interaction.user)

        last_open = pack_cooldown_tracking.get(u_id_int)
        if last_open:
            elapsed = (now - last_open).total_seconds()
            if elapsed < tier["cooldownSeconds"]:
                remaining = tier["cooldownSeconds"] - elapsed
                await interaction.response.send_message(
                    f"⏰ Cooldown active! Please wait {remaining:.1f}s before opening another pack.", ephemeral=True
                )
                return

        daily_data = pack_daily_limit_tracking.get(u_id_int)
        if daily_data and now < daily_data["reset_time"]:
            if daily_data["count"] + 1 > tier["dailyLimit"]:
                time_until_reset = daily_data["reset_time"] - now
                hours = int(time_until_reset.total_seconds() // 3600)
                minutes = int((time_until_reset.total_seconds() % 3600) // 60)
                await interaction.response.send_message(
                    f"🛑 Daily pack opening limit reached ({tier['dailyLimit']:,} max). Cycle resets in {hours}h {minutes}m.", ephemeral=True
                )
                return

        # Commit Tracking Update
        if u_id_int not in pack_daily_limit_tracking or now >= pack_daily_limit_tracking[u_id_int]["reset_time"]:
            pack_daily_limit_tracking[u_id_int] = {"count": 0, "reset_time": now + timedelta(days=1)}
        pack_daily_limit_tracking[u_id_int]["count"] += 1
        pack_cooldown_tracking[u_id_int] = now
        # ----------------------------------------------

        wallet_balance[user_id] -= 1

        player, _ = await Player.get_or_create(discord_id=str(interaction.user.id))
        ball = await self.get_random_ball(player)

        if not ball:
            await interaction.response.send_message(
                "No footballers are available.", ephemeral=True
            )
            return

        special = await self.get_random_special()
        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
            special=special,
        )

        walkout_embed = discord.Embed(
            title="🎁 Opening Packly...", color=discord.Color.dark_gray()
        )
        current_opened = pack_daily_limit_tracking[u_id_int]["count"]
        walkout_embed.set_footer(text=f"FootballDex Packly | Daily Packs: {current_opened:,}/{tier['dailyLimit']:,}")
        await interaction.response.defer()
        msg = await interaction.followup.send(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        if special:
            await asyncio.sleep(1.5)
            special_emoji = ""
            if special.emoji:
                try:
                    emoji_id = int(special.emoji)
                    special_emoji = self.bot.get_emoji(emoji_id) or "⚡"
                except ValueError:
                    special_emoji = special.emoji
            else:
                special_emoji = "⚡"

            walkout_embed.description += f"\n{special_emoji} **Special:** **{special.name}**"
            await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description += (
            f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        )
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        special_text = f" with **{special.name}** special!" if special else "!"
        walkout_embed.title = f"🎉 You claimed **{ball.country}** from Packly{special_text}"
        walkout_embed.color = discord.Color.gold()

        content, file, view = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(
            name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
        )

        await msg.edit(embed=walkout_embed, attachments=[file], view=view)
        file.close()

    @app_commands.command(
        name="multipackly", description="Claim multiple footballers from the multipackly!"
    )
    @app_commands.describe(
        packs="Number of packs to open (1-75)",
        fast_open="Set to True to skip all animations and instantly open all packs"
    )
    async def multipackly(self, interaction: discord.Interaction["BallsDexBot"], packs: int, fast_open: bool = False):
        user_id = str(interaction.user.id)
        u_id_int = interaction.user.id

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.", ephemeral=True
            )
            return

        if user_id not in wallet_balance:
            wallet_balance[user_id] = 1

        if packs < 1 or packs > 75:
            await interaction.response.send_message(
                "You can only open between 1 and 75 packs!", ephemeral=True
            )
            return

        if wallet_balance[user_id] < packs:
            await interaction.response.send_message("You don't have enough packs!", ephemeral=True)
            return

        # --- DYNAMIC COOLDOWN & LIMIT VERIFICATION ---
        now = datetime.now(timezone.utc)
        tier = self.get_user_pack_tier(interaction.user)

        last_open = pack_cooldown_tracking.get(u_id_int)
        if last_open:
            elapsed = (now - last_open).total_seconds()
            if elapsed < tier["cooldownSeconds"]:
                remaining = tier["cooldownSeconds"] - elapsed
                await interaction.response.send_message(
                    f"⏰ Cooldown active! Please wait {remaining:.1f}s before opening more packs.", ephemeral=True
                )
                return

        daily_data = pack_daily_limit_tracking.get(u_id_int)
        if daily_data and now < daily_data["reset_time"]:
            if daily_data["count"] + packs > tier["dailyLimit"]:
                allowed_packs = tier["dailyLimit"] - daily_data["count"]
                time_until_reset = daily_data["reset_time"] - now
                hours = int(time_until_reset.total_seconds() // 3600)
                minutes = int((time_until_reset.total_seconds() % 3600) // 60)
                await interaction.response.send_message(
                    f"🛑 Opening {packs} packs would exceed your remaining daily limit ({allowed_packs:,} left). "
                    f"Resets in {hours}h {minutes}m.", ephemeral=True
                )
                return

        # Commit Tracking Update
        if u_id_int not in pack_daily_limit_tracking or now >= pack_daily_limit_tracking[u_id_int]["reset_time"]:
            pack_daily_limit_tracking[u_id_int] = {"count": 0, "reset_time": now + timedelta(days=1)}
        pack_daily_limit_tracking[u_id_int]["count"] += packs
        pack_cooldown_tracking[u_id_int] = now
        # ----------------------------------------------

        # Deduct packs
        wallet_balance[user_id] -= packs
        view = None

        if fast_open:
            await interaction.response.defer()
        else:
            first_embed = discord.Embed(
                title="🎁 Opening Multipackly...",
                description="Get ready to reveal your footballers!",
                color=discord.Color.gold(),
            )
            first_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            first_embed.set_footer(text="FootballDex MultiPacklys")

            view = SkipView(interaction.user.id)
            await interaction.response.send_message(embed=first_embed, view=view)
            message = await interaction.original_response()

            try:
                await asyncio.wait_for(view.skip_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

        player, _ = await Player.get_or_create(discord_id=user_id)
        all_balls = await Ball.filter(
            rarity__gte=0.03, rarity__lte=30.0, enabled=True, hidden_from_packs=False
        ).all()
        
        if not all_balls:
            if fast_open:
                await interaction.followup.send("No footballers are available.", ephemeral=True)
            else:
                await message.edit(content="No footballers are available.", embed=None, view=None)
            return

        weighted_choices = []
        for b in all_balls:
            if 5.0 <= b.rarity <= 30.0: rarity_weight = 1600
            elif 2.5 <= b.rarity < 5.0: rarity_weight = 600
            elif 1.5 <= b.rarity < 2.5: rarity_weight = 300
            elif 0.5 < b.rarity < 1.5: rarity_weight = 100
            elif 0.1 < b.rarity < 0.5: rarity_weight = 30
            elif 0.01 <= b.rarity <= 0.1: rarity_weight = 20
            else: rarity_weight = 1
            weighted_choices.extend([b] * rarity_weight)

        active_specials = await Special.filter(
            Q(start_date__isnull=True) | Q(start_date__lte=now),
            Q(end_date__isnull=True) | Q(end_date__gte=now),
            hidden=False,
        ).all()

        pulled_balls = []
        special_counts = {}
        instances_to_create = []

        for _ in range(packs):
            ball = random.choice(weighted_choices)
            special = None
            if active_specials:
                for sp in active_specials:
                    if random.random() < sp.rarity:
                        special = sp
                        break

            if special:
                special_counts[special.name] = special_counts.get(special.name, 0) + 1

            instances_to_create.append(
                BallInstance(
                    ball=ball,
                    player=player,
                    attack_bonus=random.randint(-20, 20),
                    health_bonus=random.randint(-20, 20),
                    special=special,
                )
            )
            pulled_balls.append(ball)

        await BallInstance.bulk_create(instances_to_create)
        balance = wallet_balance.get(user_id, 0)

        if not fast_open and view is not None and not view.skipped:
            cards_to_animate = min(packs, 3) 
            for i in range(cards_to_animate):
                if view.skipped:
                    break 
                
                ball = pulled_balls[i]
                special = instances_to_create[i].special
                
                special_info = ""
                if special:
                    special_emoji = "⚡"
                    if special.emoji:
                        try:
                            emoji_id = int(special.emoji)
                            special_emoji = self.bot.get_emoji(emoji_id) or "⚡"
                        except ValueError:
                            special_emoji = special.emoji
                    special_info = f"\n{special_emoji} **Special:** {special.name}"

                walkout_embed = discord.Embed(
                    title=f"🏆 You pulled {ball.country}!",
                    description=f"**Rarity:** {ball.rarity}\n⚽ **Attack:** {ball.attack}\n❤️ **Health:** {ball.health}{special_info}",
                    color=discord.Color.random(),
                )
                walkout_embed.set_thumbnail(url=interaction.user.display_avatar.url)
                walkout_embed.set_footer(text=f"FootballDex Pack Opening ({i+1}/{packs})")

                await message.edit(embed=walkout_embed, view=view)

                try:
                    await asyncio.wait_for(view.skip_event.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass

        special_summary = ""
        if special_counts:
            special_summary = "\n\n**Specials Pulled:**\n" + "\n".join(
                f"**{name}** ({count})" for name, count in special_counts.items()
            )

        top_5 = sorted(pulled_balls, key=lambda b: b.rarity, reverse=False)[:5]
        top_5_summary = "\n**Top 5 Pulls:**\n" + ", ".join(f"**{b.country}**" for b in top_5)

        current_opened = pack_daily_limit_tracking[u_id_int]["count"]
        final_embed = discord.Embed(
            title="🎉 All Footballers Revealed!",
            description=(
                f"Your Multi-Packly has been done!\n\n"
                f"*Here is what you got in your multipackly:*\n"
                f"**{', '.join(b.country for b in pulled_balls)}!**\n"
                f"{top_5_summary}\n"
                f"{special_summary}\n\n"
                f"🪙 **New Packly Balance: {balance}**"
            ),
            color=discord.Color.green(),
        )
        final_embed.set_footer(text=f"FootballDex MultiPacklys | Daily Packs: {current_opened:,}/{tier['dailyLimit']:,}")
        
        if fast_open:
            await interaction.followup.send(embed=final_embed)
        else:
            await message.edit(embed=final_embed, view=None)

    @owners.command(name="add")
    async def ownerspacklyadd(
        self, interaction: discord.Interaction["BallsDexBot"], user: discord.User, packs: int
    ):
        """Add packs from a user's wallet (owners only)."""
        user_id = str(interaction.user.id)
        username = interaction.user.name

        if interaction.user.id not in ownersid:
            await interaction.response.send_message(
                "You are not allowed to add packly's to other people or youself ❌", ephemeral=True
            )
            return

        target_user_id = str(user.id)
        if target_user_id not in wallet_balance:
            wallet_balance[target_user_id] = 1 

        wallet_balance[target_user_id] += packs

        embed = discord.Embed(
            title="📦 Pack Added Successfully!",
            description=(
                f"**{interaction.user.display_name}** has added you **{packs}** pack(s)! 🎁\n\n"
                f"🪙 **Your New Balance:** `{wallet_balance[target_user_id]} packs`"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="FootballDex Wallet System")
        embed.set_thumbnail(url=user.display_avatar.url) 

        await self.safe_send_pinged_embed(
            interaction, user, embed, content_mention=f"{user.mention}"
        )

    @owners.command(name="remove")
    async def owners_remove(
        self, interaction: discord.Interaction["BallsDexBot"], user: discord.User, packs: int
    ):
        """Remove packs from a user's wallet (owners only)."""
        user_id = str(interaction.user.id)
        username = interaction.user.name

        if interaction.user.id not in ownersid:
            await interaction.response.send_message(
                "You are not allowed to remove packly's from other people or youself ❌",
                ephemeral=True,
            )
            return

        target_user_id = str(user.id)
        if target_user_id not in wallet_balance:
            wallet_balance[target_user_id] = 0 

        wallet_balance[target_user_id] = max(0, wallet_balance[target_user_id] - packs)

        embed = discord.Embed(
            title="FootballDex Packs Removed!",
            description=(
                f"{interaction.user.mention} has removed **{packs}** pack(s) from {user.mention}'s wallet.\n"
                f"🪙 **{user.name}'s New Balance**: `{wallet_balance[target_user_id]} packs`"
            ),
            color=discord.Color.red(),
        )
        embed.set_footer(text="Packly System")
        embed.set_thumbnail(url=user.display_avatar.url)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="gamblepack",
        description="Gamble your packlys for a chance to win double – or lose it all!",
    )
    @app_commands.describe(amount="How many packs to gamble (fixed 50/50 chance)")
    async def gamblepack(self, interaction: discord.Interaction["BallsDexBot"], amount: int = 1):
        user_id = str(interaction.user.id)

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.", ephemeral=True
            )
            return

        now = datetime.utcnow()

        if amount < 1:
            await interaction.response.send_message(
                "You must gamble at least 1 pack.", ephemeral=True
            )
            return

        if amount > 10000:
            await interaction.response.send_message(
                "❌ You can only gamble up to 10000 packlys at once.", ephemeral=True
            )
            return

        if user_id not in wallet_balance:
            wallet_balance[user_id] = 0

        if wallet_balance[user_id] < amount:
            await interaction.response.send_message(
                "❌ You don't have enough packlys to gamble that many.", ephemeral=True
            )
            return

        wallet_balance[user_id] -= amount

        await interaction.response.defer()

        suspense = discord.Embed(
            title=f"🎲 Gambling {amount} packly{'s' if amount > 1 else ''}...",
            description="Rolling the dice...",
            color=discord.Color.dark_grey(),
        )
        suspense.set_footer(text="Good luck...")
        msg = await interaction.followup.send(embed=suspense)

        await asyncio.sleep(2)

        result = "win" if random.choice([True, False]) else "lose"

        if result == "win":
            reward = amount * 2
            wallet_balance[user_id] += reward
            suspense.title = f"🎉 You WON {reward} packlys!"
            suspense.color = discord.Color.green()
            suspense.description = f"Luck is on your side. You risked {amount}, and won {reward}!"
        else:
            suspense.title = f"💀 You LOST your {amount} packly{'s' if amount > 1 else ''}!"
            suspense.color = discord.Color.red()
            suspense.description = "Bad luck... you lost it all."

        await msg.edit(embed=suspense)

        log_channel_id = 1341228457417248940
        log_channel = self.bot.get_channel(log_channel_id)
        if log_channel:
            await log_channel.send(
                f"🎲 **{interaction.user.mention}** gambled `{amount}` packlys and **{result.upper()}**.\n"
                f"🎯 Win chance: `50%`\n"
                f"📦 New balance: `{wallet_balance[user_id]}`"
            )

    @app_commands.command(name="give")
    @app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
    async def give(
        self, interaction: discord.Interaction["BallsDexBot"], member: discord.Member, packs: int
    ):
        """Give pack(s) to a user or a friend!"""

        sender_id = str(interaction.user.id)
        receiver_id = str(member.id)
        PACKLY_LOG_CHANNEL_ID = 1341228457417248940

        if interaction.user.id == member.id:
            await interaction.response.send_message(
                "You cannot give packlys to yourself.", ephemeral=True
            )
            return

        if packs <= 0:
            await interaction.response.send_message(
                "Amount must be greater than 0.", ephemeral=True
            )
            return

        sender_balance = wallet_balance.get(sender_id, 0)

        if sender_balance < packs:
            await interaction.response.send_message(
                f"You don't have enough packlys. You currently have **{sender_balance}**.",
                ephemeral=True,
            )
            return

        wallet_balance[sender_id] = sender_balance - packs
        wallet_balance[receiver_id] = wallet_balance.get(receiver_id, 0) + packs
        log_channel = interaction.client.get_channel(PACKLY_LOG_CHANNEL_ID)

        if log_channel:
            log_embed = discord.Embed(
                title="Packlys Give Log",
                description=(
                    f"Sender: {interaction.user.mention}\n"
                    f"Receiver: {member.mention}\n"
                    f"Amount: **{packs}** packly(s)\n\n"
                    f"Sender New Balance: **{wallet_balance[sender_id]}**\n"
                    f"Receiver New Balance: **{wallet_balance[receiver_id]}**"
                ),
                color=discord.Color.orange(),
            )

            log_embed.set_footer(text="FootballDex Packlys Logs")
            await log_channel.send(embed=log_embed)

        embed = discord.Embed(
            title="Packlys Given!",
            description=(
                f"{interaction.user.mention} gave **{packs}** packly(s) to {member.mention}.\n\n"
                f"**{interaction.user.name}** now has **{wallet_balance[sender_id]}** packly(s).\n"
                f"**{member.name}** now has **{wallet_balance[receiver_id]}** packly(s)."
            ),
            color=discord.Color.green(),
        )

        embed.set_footer(text="FootballDex Packlys")
        await interaction.response.send_message(
            content=f"{interaction.user.mention} {member.mention}", embed=embed
        )

    @app_commands.command(name="leaderboard")
    async def leaderboard(self, interaction: discord.Interaction["BallsDexBot"]):
        """See the top 10 richest users with the most amount of packs!"""

        if not wallet_balance:
            await interaction.response.send_message("No one has any packlys yet.", ephemeral=True)
            return

        sorted_users = sorted(wallet_balance.items(), key=lambda x: x[1], reverse=True)[:10]
        embed = discord.Embed(title="🤑 Packlys Leaderboard", color=discord.Color.gold())
        description = ""

        for index, (user_id, balance) in enumerate(sorted_users, start=1):
            user = interaction.guild.get_member(int(user_id))
            username = user.name if user else f"<@{user_id}>"
            description += f"**#{index}** — {username} • **{balance}** packly(s)\n"

        embed.description = description
        embed.set_footer(text="FootballDex Packlys")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="wallet", description="Check your wallet balance")
    @app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
    async def wallet(self, interaction: discord.Interaction["BallsDexBot"]):
        user_id = str(interaction.user.id)
        username = interaction.user.name

        if user_id not in self.bot_walletturorial_seen:
            tutorial_embed = discord.Embed(
                title="Welcome To The Packlys Wallet Command!",
                description=(
                    "Use `/packs wallet` to check your packlys balance.\n"
                    "- You start with 0 Packlys.\n"
                    "- To get more packlys, you have to ask the owners of FootballDex to add them!\n"
                    "- Join **[FootballDex](https://discord.gg/footballdex) to get free packlys!**\n"
                    "- These packlys can be used for `/packs packlys` `/packs multipackly` and `/packs gamblepack`\n"
                    "Enjoy!"
                ),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=tutorial_embed, ephemeral=True)
            self.bot_walletturorial_seen.add(user_id)
            return 

        balance = wallet_balance.get(user_id, 0)
        embed = discord.Embed(
            title=f"{username}'s Wallet",
            description=f"You currently have **{balance}** packly(s).",
            color=discord.Color.green(),
        )
        embed.set_footer(text="FootballDex Wallet")
        await interaction.response.send_message(embed=embed, ephemeral=False)