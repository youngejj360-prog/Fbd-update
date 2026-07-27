import enum
import logging
from typing import TYPE_CHECKING, cast


import discord
from discord import Interaction
from discord import app_commands, File
from discord.ext import commands
from discord.ui import Button, View, button
from tortoise.exceptions import DoesNotExist
from tortoise.expressions import RawSQL
from tortoise.functions import Count
from collections import Counter, defaultdict
from tortoise.expressions import F
from datetime import datetime, timedelta
import random
from discord import Embed, Color
from pathlib import Path
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from ballsdex.core.models import (
    BallInstance,
    DonationPolicy,
    Player,
    Special,
    Trade,
    TradeObject,
    balls,
)
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.paginator import FieldPageSource, Pages
from ballsdex.core.utils.sorting import SortingChoices, sort_balls
from ballsdex.core.utils.transformers import (
    BallEnabledTransform,
    BallInstanceTransform,
    BallTransform,
    SpecialEnabledTransform,
    TradeCommandType,
    RegimeTransform,
)
from ballsdex.core.image_generator.image_gen import draw_card
from ballsdex.core.utils.utils import can_mention, inventory_privacy, is_staff
from ballsdex.packages.balls.countryballs_paginator import CountryballsViewer
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.countryballs")


class DonationRequest(View):
    def __init__(
        self,
        bot: "BallsDexBot",
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstance,
        new_player: Player,
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.original_interaction = interaction
        self.countryball = countryball
        self.new_player = new_player

    async def interaction_check(self, interaction: discord.Interaction["BallsDexBot"], /) -> bool:
        if interaction.user.id != self.new_player.discord_id:
            await interaction.response.send_message(
                "You are not allowed to interact with this menu.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True  # type: ignore
        try:
            await self.original_interaction.followup.edit_message(
                "@original", view=self  # type: ignore
            )
        except discord.NotFound:
            pass
        await self.countryball.unlock()

    @button(
        style=discord.ButtonStyle.success, emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}"
    )
    async def accept(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        self.stop()
        for item in self.children:
            item.disabled = True  # type: ignore
        self.countryball.favorite = False
        self.countryball.trade_player = self.countryball.player
        self.countryball.player = self.new_player
        await self.countryball.save()
        trade = await Trade.create(player1=self.countryball.trade_player, player2=self.new_player)
        await TradeObject.create(
            trade=trade, ballinstance=self.countryball, player=self.countryball.trade_player
        )
        await interaction.response.edit_message(
            content=interaction.message.content  # type: ignore
            + "\n\N{WHITE HEAVY CHECK MARK} The donation was accepted!",
            view=self,
        )
        await self.countryball.unlock()

    @button(
        style=discord.ButtonStyle.danger,
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
    )
    async def deny(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        self.stop()
        for item in self.children:
            item.disabled = True  # type: ignore
        await interaction.response.edit_message(
            content=interaction.message.content  # type: ignore
            + "\n\N{CROSS MARK} The donation was denied.",
            view=self,
        )
        await self.countryball.unlock()


class DuplicateType(enum.Enum):
    countryballs = "countryballs"
    specials = "specials"


class Balls(commands.GroupCog, group_name=settings.players_group_cog_name):
    """
    View and manage your countryballs collection.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        self.frame_memory = {}

    @app_commands.command()
    async def list(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
        sort: SortingChoices | None = None,
        reverse: bool = False,
        countryball: BallTransform | None = None,
        special: SpecialEnabledTransform | None = None,
        regime: RegimeTransform | None = None,
    ):
        """
        List your countryballs with optional filters and sorting.

        Parameters
        ----------
        user: discord.User
            The user whose collection you want to view, if not yours.
        sort: SortingChoices
            Choose how countryballs are sorted. Can be used to show duplicates.
        reverse: bool
            Reverse the output of the list.
        countryball: Ball
            Filter the list by a specific countryball.
        special: Special
            Filter the list by a specific special event.
        regime: Regime
            Filter the list by a specific regime.
        """
        user_obj = user or interaction.user
        await interaction.response.defer(thinking=True)

        # Fetch player
        try:
            player = await Player.get(discord_id=user_obj.id)
        except DoesNotExist:
            msg = (
                f"You don't have any {settings.plural_collectible_name} yet."
                if user_obj == interaction.user
                else f"{user_obj.name} doesn't have any {settings.plural_collectible_name} yet."
            )
            await interaction.followup.send(msg)
            return

        # Pricacy Checks
        interaction_player, _ = await Player.get_or_create(discord_id=interaction.user.id)

        # Viewing someone else's inventory
        if user is not None and user_obj.id != interaction.user.id:

            # 1) block check hard
            is_blocked = await player.is_blocked(interaction_player)

            if is_blocked and not is_staff(interaction):
                await interaction.followup.send(
                    "You cannot view this user's countryballs.", ephemeral=True
                )
                return

            # 2) pricaxy check very hard like mbappe
            allowed = await inventory_privacy(self.bot, interaction, player, user_obj)

            if not allowed:
                await interaction.followup.send(
                    "This user's inventory is private.", ephemeral=True
                )
                return

        # SQL Filter ONLY!
        query = player.balls.all()
        if countryball:
            query = query.filter(ball__id=countryball.pk)
        if special:
            query = query.filter(special=special)
        if regime:
            query = query.filter(ball__regime=regime)

        MAX_PAGES = 200
        ITEMS_PER_PAGE = 25
        MAX_ITEMS = MAX_PAGES * ITEMS_PER_PAGE

        total = await query.count()

        if not total:
            ball_txt = countryball.country if countryball else ""
            special_txt = special if special else ""
            regime_txt = regime if regime else ""

            if special_txt and ball_txt and regime_txt:
                combined = f"{special_txt} {ball_txt} {regime_txt}"
            elif special_txt:
                combined = special_txt
            elif ball_txt:
                combined = ball_txt
            elif regime_txt:
                combined = regime_txt
            else:
                combined = ""

            if user_obj == interaction.user:
                await interaction.followup.send(
                    f"You don't have any {combined} {settings.plural_collectible_name} yet.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"{user_obj.name} doesn't have any {combined} {settings.plural_collectible_name} yet."
                )
            return

        # Sortings
        countryballs_list = None

        if sort:
            if sort == SortingChoices.duplicates:
                all_balls = list(await query.prefetch_related("ball", "special").limit(MAX_ITEMS))

                count_map = Counter(bi.ball_id for bi in all_balls)
                grouped_map = defaultdict(list)
                for bi in all_balls:
                    grouped_map[bi.ball_id].append(bi)

                # Create list sorted by duplicate count, then by country
                countryballs_list = sorted(
                    all_balls,
                    key=lambda bi: (-bi.favorite, bi.special.name if bi.special else ""),
                    reverse=reverse,  # applies reverse if requested
                )
                for ball_id, _ in count_map.most_common():  # most duplicates first
                    countryballs_list.extend(grouped_map[ball_id])
            else:
                order_field = sort.value
                descending = order_field.startswith("-")
                field_name = order_field.lstrip("-")

                if reverse:
                    order_field = field_name if descending else f"-{field_name}"

                query = query.order_by(order_field, "ball__country")
                countryballs_list = list(await query.limit(MAX_ITEMS))
        else:
            query = query.order_by("-favorite", "id")
            countryballs_list = list(await query.limit(MAX_ITEMS))

        # info content
        info_content = (
            f"Showing first **{MAX_ITEMS} of {total} {settings.plural_collectible_name}.**\n\n*Use more specific filters to find what you’re looking for.*"
            if total > MAX_ITEMS
            else None
        )

        # Paginator
        paginator = CountryballsViewer(interaction, countryballs_list)
        content = (
            info_content
            if user_obj == interaction.user
            else f"Viewing {user_obj.name}'s {settings.plural_collectible_name}"
            + (f" - {info_content}" if info_content else "")
        )
        await paginator.start(content=content)

    @app_commands.command()
    async def completion(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
        special: SpecialEnabledTransform | None = None,
        regime: RegimeTransform | None = None,
    ):
        """
        Show your current completion of the BallsDex.

        Parameters
        ----------
        user: discord.User
            The user whose completion you want to view, if not yours.
        special: Special
            The special you want to see the completion of
        regime: Regime
            The regime you want to see the completion of
        """
        user_obj = user or interaction.user
        await interaction.response.defer(thinking=True)
        extra_text = f"{special.name} " if special else ""
        if user is not None:
            try:
                player = await Player.get(discord_id=user_obj.id)
            except DoesNotExist:
                await interaction.followup.send(
                    f"{user_obj.name} doesn't have any "
                    f"{extra_text}{settings.plural_collectible_name} yet."
                )
                return

            interaction_player, _ = await Player.get_or_create(discord_id=interaction.user.id)

            blocked = await player.is_blocked(interaction_player)
            if blocked and not is_staff(interaction):
                await interaction.followup.send(
                    "You cannot view the completion of a user that has blocked you.",
                    ephemeral=True,
                )
                return

            if await inventory_privacy(self.bot, interaction, player, user_obj) is False:
                return
        bot_countryballs = {}

        for x, y in balls.items():
            if not y.enabled and not regime:
                continue
            if special and special.end_date is not None and y.created_at >= special.end_date:
                continue
            if regime and y.regime_id != regime.id:
                continue
            bot_countryballs[x] = y.emoji_id

        filters = {"player__discord_id": user_obj.id}
        if regime:
            filters["ball__regime"] = regime
        else:
            filters["ball__enabled"] = True

        if special:
            filters["special"] = special

        if not bot_countryballs:
            if special and regime:
                msg = (
                    f"There are no {special.name} {regime.name} "
                    f"{settings.plural_collectible_name} registered on this bot yet."
                )
            elif regime:
                msg = (
                    f"There are no {regime.name} "
                    f"{settings.plural_collectible_name} registered on this bot yet."
                )
            elif special:
                msg = (
                    f"There are no {special.name} "
                    f"{settings.plural_collectible_name} registered on this bot yet."
                )
            else:
                msg = (
                    f"There are no {settings.plural_collectible_name} "
                    f"registered on this bot yet."
                )

            await interaction.followup.send(msg, ephemeral=True)
            return

        owned_countryballs = set(
            x[0] for x in await BallInstance.filter(**filters).distinct().values_list("ball_id")
        )

        entries: list[tuple[str, str]] = []

        def fill_fields(title: str, emoji_ids: set[int]):
            first_field_added = False
            buffer = ""

            for emoji_id in emoji_ids:
                emoji = self.bot.get_emoji(emoji_id)
                if not emoji:
                    continue

                text = f"{emoji} "
                if len(buffer) + len(text) > 1024:
                    if first_field_added:
                        entries.append(("\u200b", buffer))
                    else:
                        entries.append((f"__**{title}**__", buffer))
                        first_field_added = True
                    buffer = ""
                buffer += text

            if buffer:
                if first_field_added:
                    entries.append(("\u200b", buffer))
                else:
                    entries.append((f"__**{title}**__", buffer))

        if owned_countryballs:
            fill_fields(
                f"Owned {settings.plural_collectible_name}{f' ({regime.name})' if regime else ''}",
                set(bot_countryballs[x] for x in owned_countryballs),
            )
        else:
            entries.append((f"__**Owned {settings.plural_collectible_name}**__", "Nothing yet."))

        if missing := set(y for x, y in bot_countryballs.items() if x not in owned_countryballs):
            fill_fields(
                f"Missing {settings.plural_collectible_name}{f' ({regime.name})' if regime else ''}",
                missing,
            )
        else:
            entries.append(
                (
                    f"__**:tada: No missing {settings.plural_collectible_name}, "
                    "congratulations! :tada:**__",
                    "\u200b",
                )
            )

        source = FieldPageSource(entries, per_page=5, inline=False, clear_description=False)
        special_str = f" ({special.name})" if special else ""
        source.embed.description = (
            f"{settings.bot_name}{special_str} progression: "
            f"**{round(len(owned_countryballs) / len(bot_countryballs) * 100, 1)}%**"
        )
        source.embed.colour = discord.Colour.blurple()
        source.embed.set_author(name=user_obj.display_name, icon_url=user_obj.display_avatar.url)

        pages = Pages(source=source, interaction=interaction, compact=True)
        await pages.start()

    @app_commands.command()
    async def unobtainable_completion(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Show your current completion of unobtainable countryballs only.

        Parameters
        ----------
        user: discord.User
            The user whose unobtainable completion you want to view, if not yours.
        special: Special
            The special you want to see the unobtainable completion of
        """
        user_obj = user or interaction.user
        await interaction.response.defer(thinking=True)
        extra_text = f"{special.name} " if special else ""

        # Handle user parameter and privacy checks
        if user is not None:
            try:
                player = await Player.get(discord_id=user_obj.id)
            except DoesNotExist:
                await interaction.followup.send(
                    f"{user_obj.name} doesn't have any "
                    f"{extra_text}{settings.plural_collectible_name} yet."
                )
                return

            interaction_player, _ = await Player.get_or_create(discord_id=interaction.user.id)

            blocked = await player.is_blocked(interaction_player)
            if blocked and not is_staff(interaction):
                await interaction.followup.send(
                    "You cannot view the completion of a user that has blocked you.",
                    ephemeral=True,
                )
                return

            if await inventory_privacy(self.bot, interaction, player, user_obj) is False:
                return
        else:
            # Get or create player for the interaction user
            try:
                player = await Player.get(discord_id=user_obj.id)
            except DoesNotExist:
                await interaction.followup.send(
                    f"You don't have any {extra_text}{settings.plural_collectible_name} yet."
                )
                return

        # Filter for UNOBTAINABLE balls only (disabled balls)
        # These are balls that can no longer be obtained through normal gameplay
        unobtainable_balls = {x: y.emoji_id for x, y in balls.items() if not y.enabled}

        if not unobtainable_balls:
            await interaction.followup.send(
                f"There are no unobtainable {extra_text}{settings.plural_collectible_name} registered."
            )
            return

        # Set of unobtainable ball IDs owned by the player
        owned_unobtainable_ids = set()
        async for ball in player.balls.filter(ball_id__in=unobtainable_balls.keys()):
            if special:
                if ball.special == special:
                    owned_unobtainable_ids.add(ball.ball_id)
            else:
                owned_unobtainable_ids.add(ball.ball_id)

        if special:
            # Filter unobtainable balls by special
            unobtainable_balls = {
                x: y.emoji_id
                for x, y in balls.items()
                if not y.enabled and any(s == special for s in getattr(y, "specials", []))
            }

        total_unobtainable = len(unobtainable_balls)
        owned_unobtainable = len(owned_unobtainable_ids)

        if total_unobtainable == 0:
            await interaction.followup.send(
                f"There are no unobtainable {extra_text}{settings.plural_collectible_name} "
                f"{'for this special ' if special else ''}registered."
            )
            return

        percentage = (owned_unobtainable / total_unobtainable) * 100

        # Prepare completion fields
        fields = []

        # Add main stats
        fields.append(
            (
                "🔒 Unobtainable Progress",
                f"**{owned_unobtainable}/{total_unobtainable}** ({percentage:.1f}%)",
            )
        )

        if special:
            fields.append(("🎯 Special Event", f"{special.name}"))

        # Add explanation of what unobtainable means
        fields.append(
            (
                "ℹ️ About Unobtainable",
                "These are disabled balls that can no longer be obtained through normal gameplay.",
            )
        )

        # Show owned unobtainable balls
        owned_names = []
        for ball_id in owned_unobtainable_ids:
            ball = balls.get(ball_id)
            if ball:
                emoji = self.bot.get_emoji(ball.emoji_id)
                if emoji:
                    owned_names.append(f"{emoji} {ball.country}")
                else:
                    owned_names.append(f"❓ {ball.country}")

        if owned_names:
            # Split into chunks if too long
            chunk_size = 10
            chunks = [
                owned_names[i : i + chunk_size] for i in range(0, len(owned_names), chunk_size)
            ]

            for i, chunk in enumerate(chunks):
                field_name = "✅ Owned Unobtainable" if i == 0 else f"✅ Owned (cont. {i+1})"
                fields.append((field_name, "\n".join(chunk)))

        # Show missing unobtainable balls if not 100% complete
        if percentage < 100:
            missing_unobtainable_ids = set(unobtainable_balls.keys()) - owned_unobtainable_ids
            missing_names = []
            for ball_id in missing_unobtainable_ids:
                ball = balls.get(ball_id)
                if ball:
                    emoji = self.bot.get_emoji(ball.emoji_id)
                    if emoji:
                        missing_names.append(f"{emoji} {ball.country}")
                    else:
                        missing_names.append(f"❓ {ball.country}")

            if missing_names:
                # Split into chunks if too long
                chunk_size = 10
                chunks = [
                    missing_names[i : i + chunk_size]
                    for i in range(0, len(missing_names), chunk_size)
                ]

                for i, chunk in enumerate(chunks):
                    field_name = (
                        "❌ Missing Unobtainable" if i == 0 else f"❌ Missing (cont. {i+1})"
                    )
                    fields.append((field_name, "\n".join(chunk)))

        # Create embed with appropriate color
        embed_color = 0x9932CC if percentage == 100 else 0x8A2BE2 if percentage >= 75 else 0x4B0082
        embed = discord.Embed(
            title=f"{user_obj.display_name}'s {extra_text}Unobtainable Collection",
            color=embed_color,
        )

        # Add progress bar
        progress_length = 20
        filled_length = int(progress_length * percentage // 100)
        progress_bar = "█" * filled_length + "░" * (progress_length - filled_length)
        embed.description = f"```{progress_bar}``` {percentage:.1f}% Complete"

        # Use paginator if many fields
        if len(fields) <= 4:
            for name, value in fields:
                embed.add_field(name=name, value=value, inline=False)
            await interaction.followup.send(embed=embed)
        else:
            # Use paginator for many fields
            # FIXED: Added inline=False and clear_description=False
            paginator = FieldPageSource(fields, per_page=4, inline=False, clear_description=False)
            paginator.embed.title = embed.title
            paginator.embed.description = embed.description
            paginator.embed.color = embed.color

            pages = Pages(source=paginator, interaction=interaction, compact=True)
            await pages.start()

    @app_commands.command()
    @app_commands.checks.cooldown(1, 60, key=lambda i: i.user.id)
    async def rarity(
        self,
        interaction: discord.Interaction,
        reverse: bool = False,
    ):
        """
        Show the rarity list of players
        Parameters
        ----------
        reverse: bool
            Whether to show the rarity list in reverse
        """

        # Filter enabled collectibles
        enabledCollectibles = [x for x in balls.values() if x.enabled]

        # Group collectibles by rarity
        rarityToCollectibles = {}
        for collectible in enabledCollectibles:
            rarity = collectible.rarity
            if rarity not in rarityToCollectibles:
                rarityToCollectibles[rarity] = []
            rarityToCollectibles[rarity].append(collectible)

        # Sort the rarityToCollectibles dictionary by rarity
        sortedRarities = sorted(rarityToCollectibles.keys(), reverse=reverse)

        # Display collectibles grouped by rarity
        entries = []
        max_len = 1000
        for rarity in sortedRarities:
            collectible_names = "\n".join(
                [f"\u200b ⋄ {c.country}" for c in rarityToCollectibles[rarity]]
            )

            # Split into chunks
            start = 0
            while start < len(collectible_names):
                chunk = collectible_names[start : start + max_len]
                start += max_len
                entry = (f"★ Rarity: {rarity}", chunk)
                entries.append(entry)

        per_page = 2
        source = FieldPageSource(entries, per_page=per_page, inline=False, clear_description=False)
        source.embed.title = f"Rarity List:"
        discord.Colour.green()
        pages = Pages(source=source, interaction=interaction, compact=False)
        await pages.start()

    @app_commands.command()
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def info(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Display info from a specific countryball.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to inspect
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return
        await interaction.response.defer(thinking=True)
        content, file, view = await countryball.prepare_for_message(interaction)
        await interaction.followup.send(content=content, file=file, view=view)
        file.close()

    @app_commands.command()
    @app_commands.checks.cooldown(1, 60, key=lambda i: i.user.id)
    async def last(
        self, interaction: discord.Interaction["BallsDexBot"], user: discord.User | None = None
    ):
        """
        Display info of your or another users last caught countryball.

        Parameters
        ----------
        user: discord.Member
            The user you would like to see
        """
        user_obj = user if user else interaction.user
        await interaction.response.defer(thinking=True)
        try:
            player = await Player.get(discord_id=user_obj.id)
        except DoesNotExist:
            msg = f"{'You do' if user is None else f'{user_obj.display_name} does'}"
            await interaction.followup.send(
                f"{msg} not have any {settings.plural_collectible_name} yet.",
                ephemeral=True,
            )
            return

        if user is not None:
            if await inventory_privacy(self.bot, interaction, player, user_obj) is False:
                return

        interaction_player, _ = await Player.get_or_create(discord_id=interaction.user.id)

        blocked = await player.is_blocked(interaction_player)
        if blocked and not is_staff(interaction):
            await interaction.followup.send(
                f"You cannot view the last caught {settings.collectible_name} "
                "of a user that has blocked you.",
                ephemeral=True,
            )
            return

        countryball = await player.balls.all().order_by("-id").first().select_related("ball")
        if not countryball:
            msg = f"{'You do' if user is None else f'{user_obj.display_name} does'}"
            await interaction.followup.send(
                f"{msg} not have any {settings.plural_collectible_name} yet.",
                ephemeral=True,
            )
            return

        content, file, view = await countryball.prepare_for_message(interaction)
        if user is not None and user.id != interaction.user.id:
            content = (
                f"You are viewing {user.display_name}'s last caught {settings.collectible_name}.\n"
                + content
            )
        await interaction.followup.send(content=content, file=file, view=view)
        file.close()

    @app_commands.command()
    async def favorite(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Set favorite countryballs.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to set/unset as favorite
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return

        # Prevent "Unknown interaction"
        await interaction.response.defer(ephemeral=True)

        if settings.max_favorites == 0:
            await interaction.followup.send(
                f"You cannot set favorite {settings.plural_collectible_name} in this bot."
            )
            return

        if not countryball.favorite:
            try:
                # OPTIMIZATION: Removed .prefetch_related("balls") 
                # Fetching the player alone is very fast. Prefetching all balls was causing the massive lag.
                player = await Player.get(discord_id=interaction.user.id)
            except DoesNotExist:
                await interaction.followup.send(
                    f"You don't have any {settings.plural_collectible_name} yet."
                )
                return

            grammar = (
                f"{settings.collectible_name}"
                if settings.max_favorites == 1
                else f"{settings.plural_collectible_name}"
            )

            # This will now do a direct SQL COUNT() query instead of checking a massive prefetched list
            if await player.balls.filter(favorite=True).count() >= settings.max_favorites:
                await interaction.followup.send(
                    f"You cannot set more than {settings.max_favorites} favorite {grammar}."
                )
                return

            countryball.favorite = True
            await countryball.save()

            emoji = self.bot.get_emoji(countryball.countryball.emoji_id) or ""
            await interaction.followup.send(
                f"{emoji} `#{countryball.pk:0X}` {countryball.countryball.country} is now a favorite {settings.collectible_name}!"
            )

        else:
            countryball.favorite = False
            await countryball.save()

            emoji = self.bot.get_emoji(countryball.countryball.emoji_id) or ""
            await interaction.followup.send(
                f"{emoji} `#{countryball.pk:0X}` {countryball.countryball.country} isn't a favorite {settings.collectible_name} anymore."
            )
    @app_commands.command(extras={"trade": TradeCommandType.PICK})
    async def give(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User,
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Give a countryball to a user.

        Parameters
        ----------
        user: discord.User
            The user you want to give a countryball to
        countryball: BallInstance
            The countryball you're giving away
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return
        if not countryball.is_tradeable:
            await interaction.response.send_message(
                f"You cannot donate this {settings.collectible_name}.", ephemeral=True
            )
            return
        if user.bot:
            await interaction.response.send_message("You cannot donate to bots.", ephemeral=True)
            return
        if await countryball.is_locked():
            await interaction.response.send_message(
                f"This {settings.collectible_name} is currently locked for a trade. Please try again later.",
                ephemeral=True,
            )
            return
        favorite = countryball.favorite
        if favorite:
            view = ConfirmChoiceView(
                interaction,
                accept_message=f"{settings.collectible_name.title()} donated.",
                cancel_message="This request has been cancelled.",
            )
            await interaction.response.send_message(
                f"This {settings.collectible_name} is a favorite, are you sure you want to donate it?",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if not view.value:
                return
            interaction = view.interaction_response
        else:
            # instead of defer, send a tiny ephemeral "processing" message
            await interaction.response.send_message("Processing donation...", ephemeral=True)
        await countryball.lock_for_trade()
        new_player, _ = await Player.get_or_create(discord_id=user.id)
        old_player = countryball.player

        if new_player == old_player:
            await interaction.followup.send(
                f"You cannot give a {settings.collectible_name} to yourself.", ephemeral=True
            )
            await countryball.unlock()
            return
        if new_player.donation_policy == DonationPolicy.ALWAYS_DENY:
            await interaction.followup.send(
                "This player does not accept donations. You can use trades instead.",
                ephemeral=True,
            )
            await countryball.unlock()
            return

        friendship = await new_player.is_friend(old_player)
        if new_player.donation_policy == DonationPolicy.FRIENDS_ONLY:
            if not friendship:
                await interaction.followup.send(
                    "This player only accepts donations from friends, use trades instead.",
                    ephemeral=True,
                )
                await countryball.unlock()
                return
        blocked = await new_player.is_blocked(old_player)
        if blocked:
            await interaction.followup.send(
                "You cannot interact with a user that has blocked you.", ephemeral=True
            )
            await countryball.unlock()
            return
        if new_player.discord_id in self.bot.blacklist:
            await interaction.followup.send(
                "You cannot donate to a blacklisted user.", ephemeral=True
            )
            await countryball.unlock()
            return
        elif new_player.donation_policy == DonationPolicy.REQUEST_APPROVAL:
            await interaction.followup.send(
                f"Hey {user.mention}, {interaction.user.name} wants to give you "
                f"{countryball.description(include_emoji=True, bot=self.bot, is_trade=True)}!\n"
                "Do you accept this donation?",
                view=DonationRequest(self.bot, interaction, countryball, new_player),
                allowed_mentions=await can_mention([new_player, old_player]),
            )
            return

        countryball.player = new_player
        countryball.trade_player = old_player
        countryball.favorite = False
        await countryball.save()

        trade = await Trade.create(player1=old_player, player2=new_player)
        await TradeObject.create(trade=trade, ballinstance=countryball, player=old_player)

        cb_txt = (
            countryball.description(short=True, include_emoji=True, bot=self.bot, is_trade=True)
            + f" (`{countryball.attack_bonus:+}%/{countryball.health_bonus:+}%`)"
        )

        try:
            await interaction.followup.send(
                f"{interaction.user.mention}, you just gave the {settings.collectible_name} {cb_txt} to {user.mention}!",
                allowed_mentions=await can_mention([new_player, old_player]),
            )
        except discord.errors.InteractionResponded:
            await interaction.channel.send(
                f"{interaction.user.mention}, you just gave the {settings.collectible_name} {cb_txt} to {user.mention}!",
                allowed_mentions=await can_mention([new_player, old_player]),
            )
        await countryball.unlock()

    @app_commands.command()
    async def count(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallTransform | None = None,
        special: SpecialEnabledTransform | None = None,
        current_server: bool = False,
    ):
        """
        Count how many countryballs you have.

        Parameters
        ----------
        countryball: Ball
            The countryball you want to count
        special: Special
            The special you want to count
        current_server: bool
            Only count countryballs caught in the current server
        """
        if interaction.response.is_done():
            return

        assert interaction.guild
        filters = {}
        if countryball:
            filters["ball"] = countryball
        if special:
            filters["special"] = special
        if current_server:
            filters["server_id"] = interaction.guild.id
        filters["player__discord_id"] = interaction.user.id

        await interaction.response.defer(ephemeral=True, thinking=True)

        balls = await BallInstance.filter(**filters).count()
        country = f"{countryball.country} " if countryball else ""
        plural = "s" if balls > 1 or balls == 0 else ""
        special_str = f"{special.name} " if special else ""
        guild = f" caught in {interaction.guild.name}" if current_server else ""

        await interaction.followup.send(
            f"You have {balls} {special_str}"
            f"{country}{settings.collectible_name}{plural}{guild}."
        )

    @app_commands.command()
    async def compare(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Compare your countryballs with another user.

        Parameters
        ----------
        user: discord.User
            The user you want to compare with
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        await interaction.response.defer(thinking=True)
        if interaction.user == user:
            await interaction.followup.send("You cannot compare with yourself.", ephemeral=True)
            return

        try:
            player = await Player.get(discord_id=user.id)
        except DoesNotExist:
            await interaction.followup.send(
                f"{user.display_name} doesn't have any {settings.plural_collectible_name} yet."
            )
            return

        if await inventory_privacy(self.bot, interaction, player, user) is False:
            return

        bot_countryballs = {x: y.emoji_id for x, y in balls.items() if y.enabled}
        if special:
            bot_countryballs = {
                x: y.emoji_id
                for x, y in balls.items()
                if y.enabled and (special.end_date is None or y.created_at < special.end_date)
            }

        player1, _ = await Player.get_or_create(discord_id=interaction.user.id)
        player2, _ = await Player.get_or_create(discord_id=user.id)

        blocked = await player.is_blocked(player1)
        if blocked and not is_staff(interaction):
            await interaction.followup.send(
                "You cannot compare with a user that has you blocked.", ephemeral=True
            )
            return

        blocked = await player.is_blocked(player2)
        if blocked and not is_staff(interaction):
            await interaction.followup.send(
                "You cannot compare with a user that has you blocked.", ephemeral=True
            )
            return
        queryset = BallInstance.filter(ball__enabled=True).distinct()
        if special:
            queryset = queryset.filter(special=special)
        user1_balls = cast(
            list[int],
            await queryset.filter(player=player1).values_list("ball_id", flat=True),
        )
        user2_balls = cast(
            list[int],
            await queryset.filter(player=player2).values_list("ball_id", flat=True),
        )
        both = set(user1_balls) & set(user2_balls)
        user1_only = set(user1_balls) - set(user2_balls)
        user2_only = set(user2_balls) - set(user1_balls)
        neither = set(bot_countryballs.keys()) - both - user1_only - user2_only

        entries = []

        def fill_fields(title: str, ids: set[int]):
            first_field_added = False
            buffer = ""

            for ball_id in ids:
                emoji = self.bot.get_emoji(bot_countryballs[ball_id])
                if not emoji:
                    continue

                text = f"{emoji} "
                if len(buffer) + len(text) > 1024:
                    # hitting embed limits, adding an intermediate field
                    if first_field_added:
                        entries.append(("\u200b", buffer))
                    else:
                        entries.append((f"__**{title}**__", buffer))
                        first_field_added = True
                    buffer = ""
                buffer += text

            if buffer:  # add what's remaining
                if first_field_added:
                    entries.append(("\u200b", buffer))
                else:
                    entries.append((f"__**{title}**__", buffer))

        if both:
            fill_fields("Both have", both)
        else:
            entries.append(("__**Both have**__", "None"))
        fill_fields(f"{interaction.user.display_name} has", user1_only)
        fill_fields(f"{user.display_name} has", user2_only)
        fill_fields("Neither have", neither)

        source = FieldPageSource(entries, per_page=5, inline=False, clear_description=False)
        special_str = f" ({special.name})" if special else ""
        source.embed.title = (
            f"Comparison of {interaction.user.display_name} and {user.display_name}'s "
            f"{settings.plural_collectible_name}{special_str}"
        )
        source.embed.colour = discord.Colour.blurple()

        pages = Pages(source=source, interaction=interaction, compact=True)
        await pages.start()

    @app_commands.command()
    async def collection(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallTransform | None = None,
        ephemeral: bool = False,
    ):
        """
        Show the collection of a specific countryball.

        Parameters
        ----------
        countryball: Ball
            The countryball you want to see the collection of
        ephemeral: bool
            Whether or not to send the command ephemerally.
        """
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        player, _ = await Player.get_or_create(discord_id=interaction.user.id)

        query = (
            BallInstance.filter(player=player)
            .annotate(
                total=RawSQL("COUNT(*)"),
                traded=RawSQL("SUM(CASE WHEN trade_player_id IS NULL THEN 0 ELSE 1 END)"),
                specials=RawSQL("SUM(CASE WHEN special_id IS NULL THEN 0 ELSE 1 END)"),
            )
            .group_by("player_id")
        )
        specials = (
            BallInstance.filter(player=player)
            .exclude(special=None)
            .annotate(count=Count("id"))
            .group_by("special__name")
        )
        if countryball:
            query = query.filter(ball=countryball)
            specials = specials.filter(ball=countryball)
        counts_list = await query.values("player_id", "total", "traded", "specials")
        specials = await specials.values("special__name", "count")

        if not counts_list:
            if countryball:
                await interaction.followup.send(
                    f"You don't have any {countryball.country} "
                    f"{settings.plural_collectible_name} yet."
                )
            else:

                await interaction.followup.send(
                    f"You don't have any {settings.plural_collectible_name} yet."
                )
            return
        counts = counts_list[0]
        all_specials = await Special.filter(hidden=False)
        special_emojis = {x.name: x.emoji for x in all_specials}

        desc = (
            f"**Total**: {counts["total"]:,} ({counts["total"] - counts["traded"]:,} caught, "
            f"{counts['traded']:,} received from trade)\n"
            f"**Total Specials**: {counts['specials']:,}\n\n"
        )
        if counts["specials"]:
            desc += "**Specials**:\n"
        for special in sorted(specials, key=lambda x: x["count"], reverse=True):
            emoji = special_emojis.get(special["special__name"], "")
            desc += f"{emoji} {special['special__name']}: {special["count"]:,}\n"

        embed = discord.Embed(
            title=f"Collection of {countryball.country}" if countryball else "Total Collection",
            description=desc,
            color=discord.Color.blurple(),
        )
        embed.set_author(
            name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
        )
        if countryball:
            emoji = self.bot.get_emoji(countryball.emoji_id)
            if emoji:
                embed.set_thumbnail(url=emoji.url)
        await interaction.followup.send(embed=embed)