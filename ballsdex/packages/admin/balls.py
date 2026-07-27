import asyncio
import logging
import random
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast
from io import BytesIO
from discord.ui import View, Button, button

import discord
from discord import app_commands, ui, Embed, Interaction, ButtonStyle
from discord.utils import format_dt
from tortoise.exceptions import BaseORMException, DoesNotExist

from ballsdex.core.bot import BallsDexBot
from ballsdex.core.models import Ball, BallInstance, Player, Special, Trade, TradeObject
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.logging import log_action
from ballsdex.core.utils.transformers import (
    BallTransform,
    EconomyTransform,
    RegimeTransform,
    SpecialTransform,
    BallEnabledTransform,
    SpecialEnabledTransform,
)
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.packages.countryballs.cog import CountryBallsSpawner
    from ballsdex.packages.countryballs.countryball import BallSpawnView

log = logging.getLogger("ballsdex.packages.admin.balls")
FILENAME_RE = re.compile(r"^(.+)(\.\S+)$")


async def save_file(attachment: discord.Attachment) -> Path:
    path = Path(f"./admin_panel/media/{attachment.filename}")
    match = FILENAME_RE.match(attachment.filename)
    if not match:
        raise TypeError("The file you uploaded lacks an extension.")
    i = 1
    while path.exists():
        path = Path(f"./admin_panel/media/{match.group(1)}-{i}{match.group(2)}")
        i = i + 1
    await attachment.save(path)
    return path.relative_to("./admin_panel/media/")

class GDropView(ui.View):
    def __init__(self, ball_instance: BallInstance):
        super().__init__(timeout=None)
        self.ball_instance = ball_instance
        self.claimed = False

    @ui.button(label="Claim", style=discord.ButtonStyle.green, custom_id="gdrop_claim")
    async def claim(self, interaction: discord.Interaction, button: ui.Button):
        if self.claimed:
            await interaction.response.send_message(
                "This footballer has already been claimed.", ephemeral=True
            )
            return


        player, _ = await Player.get_or_create(discord_id=interaction.user.id)
        self.ball_instance.player = player
        await self.ball_instance.save()

        self.claimed = True

        await interaction.response.send_message(
            f"✅ You have claimed **{self.ball_instance}**!",
            ephemeral=True
        )

        # 🔽 INSERTED EMBED UPDATE BLOCK HERE
        embed = interaction.message.embeds[0]  # Get the original embed
        embed.description = f"**{interaction.user.mention} claimed {self.ball_instance}!**"
        embed.set_footer(text="Claimed!")

        button.disabled = True
        await interaction.message.edit(embed=embed, view=self)  # Edit message with new embed

        


async def give_to_user(self, user: discord.User):
    new_owner = await Player.get_or_create(discord_id=user.id)
    self.owner = new_owner[0]
    await self.save()

class Balls(app_commands.Group):
    """
    Countryballs management
    """

    async def _spawn_bomb(
        self,
        interaction: discord.Interaction[BallsDexBot],
        countryball_cls: type["BallSpawnView"],
        countryball: Ball | None,
        channel: discord.TextChannel,
        n: int,
        special: Special | None = None,
        atk_bonus: int | None = None,
        hp_bonus: int | None = None,
    ):
        spawned = 0

        async def update_message_loop():
            for i in range(5 * 12 * 10):  # timeout progress after 10 minutes
                await interaction.followup.edit_message(
                    "@original",  # type: ignore
                    content=f"Spawn bomb in progress in {channel.mention}, "
                    f"{settings.collectible_name.title()}: {countryball or 'Random'}\n"
                    f"{spawned}/{n} spawned ({round((spawned / n) * 100)}%)",
                )
                await asyncio.sleep(5)
            await interaction.followup.edit_message(
                "@original", content="Spawn bomb seems to have timed out."  # type: ignore
            )

        await interaction.response.send_message(
            f"Starting spawn bomb in {channel.mention}...", ephemeral=True
        )
        task = interaction.client.loop.create_task(update_message_loop())
        try:
            for i in range(n):
                if not countryball:
                    ball = await countryball_cls.get_random(interaction.client, respect_hidden_from_spawn=True)
                else:
                    ball = countryball_cls(interaction.client, countryball)
                ball.special = special
                ball.atk_bonus = atk_bonus
                ball.hp_bonus = hp_bonus
                result = await ball.spawn(channel)
                if not result:
                    task.cancel()
                    await interaction.followup.edit_message(
                        "@original",  # type: ignore
                        content=f"A {settings.collectible_name} failed to spawn, probably "
                        "indicating a lack of permissions to send messages "
                        f"or upload files in {channel.mention}.",
                    )
                    return
                spawned += 1
            task.cancel()
            await interaction.followup.edit_message(
                "@original",  # type: ignore
                content=f"Successfully spawned {spawned} {settings.plural_collectible_name} "
                f"in {channel.mention}!",
            )
        finally:
            task.cancel()

    @app_commands.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def spawn(
        self,
        interaction: discord.Interaction[BallsDexBot],
        countryball: BallTransform | None = None,
        channel: discord.TextChannel | None = None,
        n: app_commands.Range[int, 1, 100] = 1,
        special: SpecialTransform | None = None,
        atk_bonus: int | None = None,
        hp_bonus: int | None = None,
    ):
        """
        Force spawn a random or specified countryball.

        Parameters
        ----------
        countryball: Ball | None
            The countryball you want to spawn. Random according to rarities if not specified.
        channel: discord.TextChannel | None
            The channel you want to spawn the countryball in. Current channel if not specified.
        n: int
            The number of countryballs to spawn. If no countryball was specified, it's random
            every time.
        special: Special | None
            Force the countryball to have a special attribute when caught.
        atk_bonus: int | None
            Force the countryball to have a specific attack bonus when caught.
        hp_bonus: int | None
            Force the countryball to have a specific health bonus when caught.
        """
        # the transformer triggered a response, meaning user tried an incorrect input
        if interaction.response.is_done():
            return
        cog = cast("CountryBallsSpawner | None", interaction.client.get_cog("CountryBallsSpawner"))
        if not cog:
            prefix = (
                settings.prefix
                if interaction.client.intents.message_content or not interaction.client.user
                else f"{interaction.client.user.mention} "
            )
            # do not replace `countryballs` with `settings.collectible_name`, it is intended
            await interaction.response.send_message(
                "The `countryballs` package is not loaded, this command is unavailable.\n"
                "Please resolve the errors preventing this package from loading. Use "
                f'"{prefix}reload countryballs" to try reloading it.',
                ephemeral=True,
            )
            return

        if n > 1:
            await self._spawn_bomb(
                interaction,
                cog.countryball_cls,
                countryball,
                channel or interaction.channel,  # type: ignore
                n,
                special=special,
                atk_bonus=atk_bonus,
                hp_bonus=hp_bonus,
            )
            # include the special/bonus details in the log for clarity
            special_attrs = []
            if special is not None:
                special_attrs.append(f"special={special.name}")
            if atk_bonus is not None:
                special_attrs.append(f"atk={atk_bonus}")
            if hp_bonus is not None:
                special_attrs.append(f"hp={hp_bonus}")
            await log_action(
                f"{interaction.user} spawned {settings.collectible_name} "
                f"{countryball or 'random'} {n} times in {channel or interaction.channel}"
                f"{' (' + ', '.join(special_attrs) + ')' if special_attrs else ''}.",
                interaction.client,
            )

            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        if not countryball:
            ball = await cog.countryball_cls.get_random(interaction.client, respect_hidden_from_spawn=True)
        else:
            ball = cog.countryball_cls(interaction.client, countryball)
        ball.special = special
        ball.atk_bonus = atk_bonus
        ball.hp_bonus = hp_bonus
        result = await ball.spawn(channel or interaction.channel)  # type: ignore

        if result:
            await interaction.followup.send(
                f"{settings.collectible_name.title()} spawned.", ephemeral=True
            )
            special_attrs = []
            if special is not None:
                special_attrs.append(f"special={special.name}")
            if atk_bonus is not None:
                special_attrs.append(f"atk={atk_bonus}")
            if hp_bonus is not None:
                special_attrs.append(f"hp={hp_bonus}")
            await log_action(
                f"{interaction.user} spawned {settings.collectible_name} {ball.name} "
                f"in {channel or interaction.channel}"
                f"{' (' + ', '.join(special_attrs) + ')' if special_attrs else ''}.",
                interaction.client,
            )
            
    @app_commands.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def spawnrare(
        self,
        interaction: discord.Interaction[BallsDexBot],
        countryball: BallTransform | None = None,
        channel: discord.TextChannel | None = None,
        n: app_commands.Range[int, 1, 100] = 1,
        special: SpecialTransform | None = None,
        atk_bonus: int | None = None,
        hp_bonus: int | None = None,
    ):
        """
        Force spawn a random or specified rare countryball (rarity 0.01-2.5).

        Parameters
        ----------
        countryball: Ball | None
            The countryball you want to spawn. Random rare according to rarities if not specified.
        channel: discord.TextChannel | None
            The channel you want to spawn the countryball in. Current channel if not specified.
        n: int
            The number of rare countryballs to spawn. If no countryball was specified, it's random
            every time within the rare range.
        special: Special | None
            Force the countryball to have a special attribute when caught.
        atk_bonus: int | None
            Force the countryball to have a specific attack bonus when caught.
        hp_bonus: int | None
            Force the countryball to have a specific health bonus when caught.
        """
        # the transformer triggered a response, meaning user tried an incorrect input
        if interaction.response.is_done():
            return
        cog = cast("CountryBallsSpawner | None", interaction.client.get_cog("CountryBallsSpawner"))
        if not cog:
            prefix = (
                settings.prefix
                if interaction.client.intents.message_content or not interaction.client.user
                else f"{interaction.client.user.mention} "
            )
            # do not replace `countryballs` with `settings.collectible_name`, it is intended
            await interaction.response.send_message(
                "The `countryballs` package is not loaded, this command is unavailable.\n"
                "Please resolve the errors preventing this package from loading. Use "
                f'"{prefix}reload countryballs" to try reloading it.',
                ephemeral=True,
            )
            return

        # Validate countryball rarity if specified
        if countryball and (countryball.rarity < 0.01 or countryball.rarity > 2.4):
            await interaction.response.send_message(
                f"The specified {settings.collectible_name} has rarity {countryball.rarity}, "
                "which is outside the rare range (0.01-2.4). Please specify a rare "
                f"{settings.collectible_name} or omit this parameter for random rare selection.",
                ephemeral=True,
            )
            return

        if n > 1:
            # For bulk rare spawning, we need a modified approach
            spawned = 0
            await interaction.response.send_message(
                f"Starting rare spawn bomb in {(channel or interaction.channel).mention}...", ephemeral=True
            )
            
            for i in range(n):
                if not countryball:
                    # Get random rare ball for each spawn
                    rare_balls = await Ball.filter(
                        rarity__gte=0.01, rarity__lte=2.5, enabled=True, hidden_from_spawn=False
                    ).all()
                    if not rare_balls:
                        await interaction.followup.edit_message(
                            "@original",
                            content=f"No rare {settings.plural_collectible_name} (rarity 0.01-2.4) are available.",
                        )
                        return
                    weights = [ball.rarity for ball in rare_balls]
                    selected_ball = random.choices(rare_balls, weights=weights, k=1)[0]
                    ball = cog.countryball_cls(interaction.client, selected_ball)
                else:
                    ball = cog.countryball_cls(interaction.client, countryball)
                
                ball.special = special
                ball.atk_bonus = atk_bonus
                ball.hp_bonus = hp_bonus
                result = await ball.spawn(channel or interaction.channel)
                if not result:
                    await interaction.followup.edit_message(
                        "@original",
                        content=f"A rare {settings.collectible_name} failed to spawn, probably "
                        "indicating a lack of permissions to send messages "
                        f"or upload files in {(channel or interaction.channel).mention}.",
                    )
                    return
                spawned += 1
                
            await interaction.followup.edit_message(
                "@original",
                content=f"Successfully spawned {spawned} rare {settings.plural_collectible_name} "
                f"in {(channel or interaction.channel).mention}!",
            )
            await log_action(
                f"{interaction.user} spawned rare {settings.collectible_name}"
                f" {countryball or 'random rare'} {n} times in {channel or interaction.channel}.",
                interaction.client,
            )

            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        if not countryball:
            # Get random rare ball with rarity between 0.01 and 2.4
            rare_balls = await Ball.filter(
                rarity__gte=0.01, rarity__lte=2.4, enabled=True, hidden_from_spawn=False
            ).all()
            if not rare_balls:
                await interaction.followup.send(
                    f"No rare {settings.plural_collectible_name} (rarity 0.03-2.4) are available.",
                    ephemeral=True,
                )
                return
            
            # Select random rare ball weighted by rarity
            weights = [ball.rarity for ball in rare_balls]
            selected_ball = random.choices(rare_balls, weights=weights, k=1)[0]
            ball = cog.countryball_cls(interaction.client, selected_ball)
        else:
            ball = cog.countryball_cls(interaction.client, countryball)
        
        ball.special = special
        ball.atk_bonus = atk_bonus
        ball.hp_bonus = hp_bonus
        result = await ball.spawn(channel or interaction.channel)  # type: ignore

        if result:
            await interaction.followup.send(
                f"Rare {settings.collectible_name.title()} spawned.", ephemeral=True
            )
            special_attrs = []
            if special is not None:
                special_attrs.append(f"special={special.name}")
            if atk_bonus is not None:
                special_attrs.append(f"atk={atk_bonus}")
            if hp_bonus is not None:
                special_attrs.append(f"hp={hp_bonus}")
            special_str = f" ({', '.join(special_attrs)})" if special_attrs else ""
            await log_action(
                f"{interaction.user} spawned rare {settings.collectible_name} {ball.name} "
                f"in {channel or interaction.channel}{special_str}.",
                interaction.client,
            )

    @app_commands.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def spawnregime(
        self,
        interaction: discord.Interaction[BallsDexBot],
        regime: RegimeTransform,
        channel: discord.TextChannel | None = None,
        n: app_commands.Range[int, 1, 100] = 1,
        special: SpecialTransform | None = None,
        atk_bonus: int | None = None,
        hp_bonus: int | None = None,
    ):
        """
        Force spawn a random countryball from a specified political regime.

        Parameters
        ----------
        regime: Regime
            The political regime to spawn balls from.
        channel: discord.TextChannel | None
            The channel you want to spawn the countryball in. Current channel if not specified.
        n: int
            The number of countryballs to spawn from the specified regime.
        special: Special | None
            Force the countryball to have a special attribute when caught.
        atk_bonus: int | None
            Force the countryball to have a specific attack bonus when caught.
        hp_bonus: int | None
            Force the countryball to have a specific health bonus when caught.
        """
        # the transformer triggered a response, meaning user tried an incorrect input
        if interaction.response.is_done():
            return
        cog = cast("CountryBallsSpawner | None", interaction.client.get_cog("CountryBallsSpawner"))
        if not cog:
            prefix = (
                settings.prefix
                if interaction.client.intents.message_content or not interaction.client.user
                else f"{interaction.client.user.mention} "
            )
            # do not replace `countryballs` with `settings.collectible_name`, it is intended
            await interaction.response.send_message(
                "The `countryballs` package is not loaded, this command is unavailable.\n"
                "Please resolve the errors preventing this package from loading. Use "
                f'"{prefix}reload countryballs" to try reloading it.',
                ephemeral=True,
            )
            return

        # Check if there are any balls available for the specified regime
        regime_balls_count = await Ball.filter(
            regime_id=regime.pk, enabled=True, rarity__gt=0, hidden_from_spawn=False
        ).count()
        if regime_balls_count == 0:
            await interaction.response.send_message(
                f"No spawnable {settings.plural_collectible_name} found for regime **{regime.name}**. "
                f"Make sure there are enabled {settings.plural_collectible_name} with rarity > 0 for this regime.",
                ephemeral=True,
            )
            return

        if n > 1:
            spawned = 0
            await interaction.response.send_message(
                f"Starting regime spawn bomb in {(channel or interaction.channel).mention}...", ephemeral=True
            )
            
            for i in range(n):
                # Get random ball from specified regime
                regime_balls = await Ball.filter(
                    regime_id=regime.pk, enabled=True, rarity__gt=0, hidden_from_spawn=False
                ).all()
                if not regime_balls:
                    await interaction.followup.edit_message(
                        "@original",
                        content=f"No spawnable {settings.plural_collectible_name} found for regime **{regime.name}**.",
                    )
                    return
                
                # Weighted random selection based on rarity
                weights = [ball.rarity for ball in regime_balls]
                selected_ball = random.choices(regime_balls, weights=weights, k=1)[0]
                ball = cog.countryball_cls(interaction.client, selected_ball)
                
                ball.special = special
                ball.atk_bonus = atk_bonus
                ball.hp_bonus = hp_bonus
                result = await ball.spawn(channel or interaction.channel)
                if not result:
                    await interaction.followup.edit_message(
                        "@original",
                        content=f"A {settings.collectible_name} failed to spawn, probably "
                        "indicating a lack of permissions to send messages "
                        f"or upload files in {(channel or interaction.channel).mention}.",
                    )
                    return
                spawned += 1
                
            await interaction.followup.edit_message(
                "@original",
                content=f"Successfully spawned {spawned} {settings.plural_collectible_name} "
                f"from regime **{regime.name}** in {(channel or interaction.channel).mention}!",
            )
            await log_action(
                f"{interaction.user} spawned {n} {settings.plural_collectible_name} "
                f"from regime {regime.name} in {channel or interaction.channel}.",
                interaction.client,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        
        # Get a random ball from the specified regime
        regime_balls = await Ball.filter(
            regime_id=regime.pk, enabled=True, rarity__gt=0, hidden_from_spawn=False
        ).all()
        if not regime_balls:
            await interaction.followup.send(
                f"No spawnable {settings.plural_collectible_name} found for regime **{regime.name}**.",
                ephemeral=True,
            )
            return
        
        # Weighted random selection based on rarity
        weights = [ball.rarity for ball in regime_balls]
        selected_ball = random.choices(regime_balls, weights=weights, k=1)[0]
        ball = cog.countryball_cls(interaction.client, selected_ball)
        
        ball.special = special
        ball.atk_bonus = atk_bonus
        ball.hp_bonus = hp_bonus
        result = await ball.spawn(channel or interaction.channel)  # type: ignore

        if result:
            await interaction.followup.send(
                f"{settings.collectible_name.title()} from regime **{regime.name}** spawned.", 
                ephemeral=True
            )
            special_attrs = []
            if special is not None:
                special_attrs.append(f"special={special.name}")
            if atk_bonus is not None:
                special_attrs.append(f"atk={atk_bonus}")
            if hp_bonus is not None:
                special_attrs.append(f"hp={hp_bonus}")
            special_str = f" ({', '.join(special_attrs)})" if special_attrs else ""
            await log_action(
                f"{interaction.user} spawned {settings.collectible_name} {ball.name} "
                f"from regime {regime.name} in {channel or interaction.channel}{special_str}.",
                interaction.client,
            )
        else:
            await interaction.followup.send(
                f"Failed to spawn {settings.collectible_name} from regime **{regime.name}**.",
                ephemeral=True,
            )

    @app_commands.command()
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def give(
        self,
        interaction: discord.Interaction[BallsDexBot],
        countryball: BallTransform,
        user: discord.User,
        special: SpecialTransform | None = None,
        health_bonus: int | None = None,
        attack_bonus: int | None = None,
    ):
        """
        Give the specified countryball to a player.

        Parameters
        ----------
        countryball: Ball
        user: discord.User
        special: Special | None
        health_bonus: int | None
            Omit this to make it random.
        attack_bonus: int | None
            Omit this to make it random.
        """
        # the transformers triggered a response, meaning user tried an incorrect input
        if interaction.response.is_done():
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        player, created = await Player.get_or_create(discord_id=user.id)
        instance = await BallInstance.create(
            ball=countryball,
            player=player,
            attack_bonus=(
                attack_bonus
                if attack_bonus is not None
                else random.randint(-settings.max_attack_bonus, settings.max_attack_bonus)
            ),
            health_bonus=(
                health_bonus
                if health_bonus is not None
                else random.randint(-settings.max_health_bonus, settings.max_health_bonus)
            ),
            special=special,
        )

        # Create the embed
        cb_txt = (
            f"{countryball.country} {settings.collectible_name} was successfully given to "
            f"`{user}`.\nSpecial: `{special.name if special else None}` • ATK: "
            f"`{instance.attack_bonus:+d}` • HP: `{instance.health_bonus:+d}`"
        )

        embed = discord.Embed(
            title=f"{settings.collectible_name} Given",
            description=cb_txt,
            color=discord.Color.green()
        )

        # Send the message to the sender (interaction user)
        await interaction.followup.send(embed=embed)

        content, file, view = await instance.prepare_for_message(interaction)

        embed.set_image(url="attachment://" + file.filename)

        # Send the message to the user who received the ball
        await user.send(
            content=f"Hey {user.mention}, you've received a new {settings.collectible_name} "
                    f"from {interaction.user.mention}!",
            embed=embed,
            file=file,
            view=view
        )

        # Log the action
        await log_action(
            f"{interaction.user} gave {settings.collectible_name} "
            f"{countryball.country} to {user}. (Special={special.name if special else None} "
            f"ATK={instance.attack_bonus:+d} HP={instance.health_bonus:+d}).",
            interaction.client,
        )

    @app_commands.command(name="info")
    @app_commands.checks.has_any_role(*settings.root_role_ids, *settings.admin_role_ids)
    async def balls_info(self, interaction: discord.Interaction[BallsDexBot], countryball_id: str):
        """
        Show information about a countryball.

        Parameters
        ----------
        countryball_id: str
            The ID of the countryball you want to get information about.
        """
        try:
            pk = int(countryball_id, 16)
        except ValueError:
            await interaction.response.send_message(
                f"The {settings.collectible_name} ID you gave is not valid.", ephemeral=True
            )
            return
        try:
            ball = await BallInstance.get(id=pk).prefetch_related(
                "player", "trade_player", "special"
            )
        except DoesNotExist:
            await interaction.response.send_message(
                f"The {settings.collectible_name} ID you gave does not exist.", ephemeral=True
            )
            return
        spawned_time = format_dt(ball.spawned_time, style="R") if ball.spawned_time else "N/A"
        catch_time = (
            (ball.catch_date - ball.spawned_time).total_seconds()
            if ball.catch_date and ball.spawned_time
            else "N/A"
        )
        admin_url = (
            f"[View online](<{settings.admin_url}/bd_models/ballinstance/{ball.pk}/change/>)"
            if settings.admin_url
            else ""
        )
        await interaction.response.send_message(
            f"**{settings.collectible_name.title()} ID:** {ball.pk}\n"
            f"**Player:** {ball.player}\n"
            f"**Name:** {ball.countryball}\n"
            f"**Attack:** {ball.attack}\n"
            f"**Attack bonus:** {ball.attack_bonus}\n"
            f"**Health bonus:** {ball.health_bonus}\n"
            f"**Health:** {ball.health}\n"
            f"**Special:** {ball.special.name if ball.special else None}\n"
            f"**Caught at:** {format_dt(ball.catch_date, style='R')}\n"
            f"**Spawned at:** {spawned_time}\n"
            f"**Catch time:** {catch_time} seconds\n"
            f"**Caught in:** {ball.server_id if ball.server_id else 'N/A'}\n"
            f"**Traded:** {ball.trade_player}\n{admin_url}",
            ephemeral=True,
        )
        await log_action(f"{interaction.user} got info for {ball}({ball.pk}).", interaction.client)

    @app_commands.command(name="delete")
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def balls_delete(
        self, interaction: discord.Interaction[BallsDexBot], countryball_id: str
    ):
        """
        Delete a countryball.

        Parameters
        ----------
        countryball_id: str
            The ID of the countryball you want to delete.
        """
        try:
            ballIdConverted = int(countryball_id, 16)
        except ValueError:
            await interaction.response.send_message(
                f"The {settings.collectible_name} ID you gave is not valid.", ephemeral=True
            )
            return
        try:
            ball = await BallInstance.get(id=ballIdConverted)
        except DoesNotExist:
            await interaction.response.send_message(
                f"The {settings.collectible_name} ID you gave does not exist.", ephemeral=True
            )
            return
        await ball.delete()
        await interaction.response.send_message(
            f"{settings.collectible_name.title()} {countryball_id} deleted.", ephemeral=True
        )
        await log_action(f"{interaction.user} deleted {ball}({ball.pk}).", interaction.client)

    @app_commands.command(name="transfer")
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def balls_transfer(
        self,
        interaction: discord.Interaction[BallsDexBot],
        countryball_id: str,
        user: discord.User,
    ):
        """
        Transfer a countryball to another user.

        Parameters
        ----------
        countryball_id: str
            The ID of the countryball you want to transfer.
        user: discord.User
            The user you want to transfer the countryball to.
        """
        try:
            ballIdConverted = int(countryball_id, 16)
        except ValueError:
            await interaction.response.send_message(
                f"The {settings.collectible_name} ID you gave is not valid.", ephemeral=True
            )
            return
        try:
            ball = await BallInstance.get(id=ballIdConverted).prefetch_related("player")
            original_player = ball.player
        except DoesNotExist:
            await interaction.response.send_message(
                f"The {settings.collectible_name} ID you gave does not exist.", ephemeral=True
            )
            return
        player, _ = await Player.get_or_create(discord_id=user.id)
        ball.player = player
        await ball.save()

        trade = await Trade.create(player1=original_player, player2=player)
        await TradeObject.create(trade=trade, ballinstance=ball, player=original_player)
        await interaction.response.send_message(
            f"Transfered {ball}({ball.pk}) from {original_player} to {user}.",
            ephemeral=True,
        )
        await log_action(
            f"{interaction.user} transferred {ball}({ball.pk}) from {original_player} to {user}.",
            interaction.client,
        )

    @app_commands.command(name="transfer_inventory")
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def inventory_balls_transfer(
        self,
        interaction: discord.Interaction[BallsDexBot],
        from_user: discord.User,
        user: discord.User,
    ):
        """
        Transfer an entire inventory from one user to another.

        Parameters
        ----------
        from_user: discord.User
            The user whose inventory will be transferred.
        user: discord.User
            The user receiving the inventory.
        """

        await interaction.response.defer(ephemeral=True)

        if from_user.id == user.id:
            await interaction.response.send_message(
                "⚠️ You cannot transfer an inventory to the same user.",
                ephemeral=True,
            )
            return

        oldPlayer = await Player.get_or_none(discord_id=from_user.id)
        newPlayer, _ = await Player.get_or_create(discord_id=user.id)

        if not oldPlayer:
            await interaction.followup.send(
                "❌ Source player not found.",
                ephemeral=True,
            )
            return

        if not newPlayer:
            await interaction.followup.send(
                "❌ Target player not found.",
                ephemeral=True,
            )
            return

        balls = await BallInstance.filter(player=oldPlayer)

        if not balls:
            await interaction.followup.send(
                "⚠️ This player has no balls to transfer.",
                ephemeral=True,
            )
            return

        transferred = 0
        for ball in balls:
            ball.player = newPlayer
            await ball.save()
            transferred += 1

        await interaction.followup.send(
            f"✅ Transferred **{len(balls)} balls** "
            f"from `{oldPlayer.discord_id}` to `{newPlayer.discord_id}`.",
            ephemeral=True,
        )

        await log_action(
            f"{interaction.user} transferred {transferred} balls "
            f"from {from_user.id} to {user.id}.",
            interaction.client,
        )

    @app_commands.command(name="transfer_filtered")
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def transfer_filtered(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        from_user: discord.User,
        user: discord.User,
        footballer: BallEnabledTransform | None = None,
        special: SpecialEnabledTransform | None = None,
        only_non_specials: bool = False,
    ):
        """
        Transfer items from one user to another, with optional filters for dex and specials.

        Parameters
        ----------
        from_user: discord.User
            The user whose items will be transferred.
        user: discord.User
            The user receiving the items.
        footballer: BallEnabledTransform
            Optional. Only transfer this specific footballer (Dex).
        special: SpecialEnabledTransform
            Optional. Only transfer items with this specific special.
        only_non_specials: bool
            If True, only items WITHOUT a special will be transferred.
        """

        await interaction.response.defer(ephemeral=True)

        if from_user.id == user.id:
            await interaction.followup.send(
                "⚠️ You cannot transfer items to the same user.",
                ephemeral=True,
            )
            return

        oldPlayer = await Player.get_or_none(discord_id=from_user.id)
        newPlayer, _ = await Player.get_or_create(discord_id=user.id)

        if not oldPlayer:
            await interaction.followup.send(
                "❌ Source player not found in the database.",
                ephemeral=True,
            )
            return

        # Base query to target the old player's inventory
        query = BallInstance.filter(player=oldPlayer)

        # Filter by a specific footballer (Dex) if provided
        if footballer:
            query = query.filter(ball=footballer)

        # Filter by a specific special, or explicitly filter out all specials
        if special:
            query = query.filter(special=special)
        elif only_non_specials:
            query = query.filter(special_id__isnull=True)

        # Perform the bulk update query directly in the database
        transferred = await query.update(player=newPlayer)

        if not transferred:
            await interaction.followup.send(
                "⚠️ No items matching those filters were found in their inventory.",
                ephemeral=True,
            )
            return

        # Construct the success message dynamically based on applied filters
        msg_lines = [f"✅ Transferred **{transferred} items** from `{oldPlayer.discord_id}` to `{newPlayer.discord_id}`."]

        if footballer:
            # Depending on your base model name, this might be `footballer.country` or `footballer.name`
            msg_lines.append(f"• **Dex Filter:** {getattr(footballer, 'country', getattr(footballer, 'name', str(footballer)))}")
        if special:
            msg_lines.append(f"• **Special Filter:** {special.name}")
        if only_non_specials:
            msg_lines.append("• **Specials Excluded:** Only normal items transferred")

        await interaction.followup.send("\n".join(msg_lines), ephemeral=True)

        await log_action(
            f"{interaction.user} transferred {transferred} filtered items "
            f"(Dex: {footballer}, Special: {special}, Non-Special Only: {only_non_specials}) "
            f"from {from_user.id} to {user.id}.",
            interaction.client,
        )

    @app_commands.command(name="gdrop", description="Drop any footballer.")
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def gdrop(self, interaction: Interaction, footballer: BallEnabledTransform):
        await interaction.response.defer()

        player = await Player.get_or_none(discord_id=interaction.user.id)
        if not player:
            player = await Player.create(discord_id=interaction.user.id)

        ball_instance = await BallInstance.create(ball=footballer, player=player)

        view = GDropView(ball_instance)  # 👈 Pass BallInstance
    
        embed = Embed(
                title=f"{footballer} has been dropped!",
                description=(
                    f"⚽ Dropped by {interaction.user.mention}\n\n"
                    f"Click the button below to claim **{footballer}**!\n"
                    f"Rarity: **{footballer.rarity}**"
                ),
                color=0x2ECC71,
            )

        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="reset")
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def balls_reset(
        self,
        interaction: discord.Interaction[BallsDexBot],
        user: discord.User,
        percentage: int | None = None,
    ):
        """
        Reset a player's countryballs.

        Parameters
        ----------
        user: discord.User
            The user you want to reset the countryballs of.
        percentage: int | None
            The percentage of countryballs to delete, if not all. Used for sanctions.
        """
        player = await Player.get_or_none(discord_id=user.id)
        if not player:
            await interaction.response.send_message(
                "The user you gave does not exist.", ephemeral=True
            )
            return
        if percentage and not 0 < percentage < 100:
            await interaction.response.send_message(
                "The percentage must be between 1 and 99.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not percentage:
            text = f"Are you sure you want to delete {user}'s {settings.plural_collectible_name}?"
        else:
            text = (
                f"Are you sure you want to delete {percentage}% of "
                f"{user}'s {settings.plural_collectible_name}?"
            )
        view = ConfirmChoiceView(
            interaction,
            accept_message=f"Confirmed, deleting the {settings.plural_collectible_name}...",
            cancel_message="Request cancelled.",
        )
        await interaction.followup.send(
            text,
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.value:
            return
        if percentage:
            balls = await BallInstance.filter(player=player)
            to_delete = random.sample(balls, int(len(balls) * (percentage / 100)))
            for ball in to_delete:
                await ball.delete()
            count = len(to_delete)
        else:
            count = await BallInstance.filter(player=player).delete()
        await interaction.followup.send(
            f"{count} {settings.plural_collectible_name} from {user} have been deleted.",
            ephemeral=True,
        )
        await log_action(
            f"{interaction.user} deleted {percentage or 100}% of "
            f"{player}'s {settings.plural_collectible_name}.",
            interaction.client,
        )

    @app_commands.command(name="count")
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def balls_count(
        self,
        interaction: discord.Interaction[BallsDexBot],
        user: discord.User | None = None,
        countryball: BallTransform | None = None,
        special: SpecialTransform | None = None,
    ):
        """
        Count the number of countryballs that a player has or how many exist in total.

        Parameters
        ----------
        user: discord.User
            The user you want to count the countryballs of.
        countryball: Ball
        special: Special
        """
        if interaction.response.is_done():
            return
        filters = {}
        if countryball:
            filters["ball"] = countryball
        if special:
            filters["special"] = special
        if user:
            filters["player__discord_id"] = user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        balls = await BallInstance.filter(**filters).count()
        verb = "is" if balls == 1 else "are"
        country = f"{countryball.country} " if countryball else ""
        plural = "s" if balls > 1 or balls == 0 else ""
        special_str = f"{special.name} " if special else ""
        if user:
            await interaction.followup.send(
                f"{user} has {balls} {special_str}"
                f"{country}{settings.collectible_name}{plural}."
            )
        else:
            await interaction.followup.send(
                f"There {verb} {balls} {special_str}"
                f"{country}{settings.collectible_name}{plural}."
            )

    @app_commands.command(name="create")
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def balls_create(
        self,
        interaction: discord.Interaction[BallsDexBot],
        *,
        name: app_commands.Range[str, None, 48],
        regime: RegimeTransform,
        health: int,
        attack: int,
        emoji_id: app_commands.Range[str, 17, 21],
        capacity_name: app_commands.Range[str, None, 64],
        capacity_description: app_commands.Range[str, None, 256],
        collection_card: discord.Attachment,
        image_credits: str,
        economy: EconomyTransform | None = None,
        rarity: float = 0.0,
        enabled: bool = False,
        tradeable: bool = False,
        wild_card: discord.Attachment | None = None,
    ):
        """
        Shortcut command for creating countryballs. They are disabled by default.

        Parameters
        ----------
        name: str
        regime: Regime
        economy: Economy | None
        health: int
        attack: int
        emoji_id: str
            An emoji ID, the bot will check if it can access the custom emote
        capacity_name: str
        capacity_description: str
        collection_card: discord.Attachment
        image_credits: str
        rarity: float
            Value defining the rarity of this countryball, if enabled
        enabled: bool
            If true, the countryball can spawn and will show up in global completion
        tradeable: bool
            If false, all instances are untradeable
        wild_card: discord.Attachment
            Artwork used to spawn the countryball, with a default
        """
        if regime is None or interaction.response.is_done():  # economy autocomplete failed
            return

        if not emoji_id.isnumeric():
            await interaction.response.send_message(
                "`emoji_id` is not a valid number.", ephemeral=True
            )
            return
        emoji = interaction.client.get_emoji(int(emoji_id))
        if not emoji:
            await interaction.response.send_message(
                "The bot does not have access to the given emoji.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        default_path = Path("./ballsdex/core/image_generator/src/default.png")
        missing_default = ""
        if not wild_card and not default_path.exists():
            missing_default = (
                "**Warning:** The default spawn image is not set. This will result in errors when "
                f"attempting to spawn this {settings.collectible_name}. You can edit this on the "
                "web panel or add an image at `./ballsdex/core/image_generator/src/default.png`.\n"
            )

        try:
            collection_card_path = await save_file(collection_card)
        except Exception as e:
            log.exception("Failed saving file when creating countryball", exc_info=True)
            await interaction.followup.send(
                f"Failed saving the attached file: {collection_card.url}.\n"
                f"Partial error: {', '.join(str(x) for x in e.args)}\n"
                "The full error is in the bot logs."
            )
            return
        try:
            wild_card_path = await save_file(wild_card) if wild_card else default_path
        except Exception as e:
            log.exception("Failed saving file when creating countryball", exc_info=True)
            await interaction.followup.send(
                f"Failed saving the attached file: {collection_card.url}.\n"
                f"Partial error: {', '.join(str(x) for x in e.args)}\n"
                "The full error is in the bot logs."
            )
            return

        try:
            ball = await Ball.create(
                country=name,
                regime=regime,
                economy=economy,
                health=health,
                attack=attack,
                rarity=rarity,
                enabled=enabled,
                tradeable=tradeable,
                emoji_id=emoji_id,
                wild_card="/" + str(wild_card_path),
                collection_card="/" + str(collection_card_path),
                credits=image_credits,
                capacity_name=capacity_name,
                capacity_description=capacity_description,
            )
        except BaseORMException as e:
            log.exception("Failed creating countryball with admin command", exc_info=True)
            await interaction.followup.send(
                f"Failed creating the {settings.collectible_name}.\n"
                f"Partial error: {', '.join(str(x) for x in e.args)}\n"
                "The full error is in the bot logs."
            )
        else:
            files = [await collection_card.to_file()]
            if wild_card:
                files.append(await wild_card.to_file())
            await interaction.client.load_cache()
            admin_url = (
                f"[View online](<{settings.admin_url}/bd_models/ball/{ball.pk}/change/>)\n"
                if settings.admin_url
                else ""
            )
            await interaction.followup.send(
                f"Successfully created a {settings.collectible_name} with ID {ball.pk}! "
                f"The internal cache was reloaded.\n{admin_url}"
                f"{missing_default}\n"
                f"{name=} regime={regime.name} economy={economy.name if economy else None} "
                f"{health=} {attack=} {rarity=} {enabled=} {tradeable=} emoji={emoji}",
                files=files,
            )
