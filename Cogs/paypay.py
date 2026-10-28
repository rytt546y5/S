from __future__ import annotations

import asyncio
import logging
import uuid

import discord
from discord import app_commands, ui
from discord.ext import commands

from services import paypayu
from storage import save_login

log = logging.getLogger(__name__)


class OTPModal(ui.Modal, title='PayPay OTP認証'):
    code = ui.TextInput(
        label='SMS認証コード', placeholder='PayPayから届いた認証コード',
        min_length=4, max_length=10,
    )

    def __init__(self, bot, owner_id, phone, password, device, reference, prefix):
        super().__init__(timeout=300)
        self.bot = bot
        self.owner_id = owner_id
        self.phone = phone
        self.password = password
        self.device = device
        self.reference = reference
        self.prefix = prefix

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id or interaction.user.id != self.bot.config.owner_id:
            return await interaction.response.send_message('権限がありません。', ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            response = await asyncio.wait_for(
                paypayu.login_otp(self.device, self.code.value, self.reference, self.prefix),
                timeout=30,
            )
            if response != 'OK':
                return await interaction.followup.send(
                    '認証コードを確認できませんでした。再度ログインしてください。', ephemeral=True,
                )
            save_login(self.owner_id, self.phone, self.password, self.device)
            await interaction.followup.send(
                embed=discord.Embed(
                    title='PayPayログイン',
                    description='OTP認証が完了しました。端末情報を保存しました。',
                    color=0x3498DB,
                ),
                ephemeral=True,
            )
        except Exception:
            log.exception('PayPay OTP authentication failed')
            await interaction.followup.send(
                'PayPayの認証に失敗しました。再度ログインしてください。', ephemeral=True,
            )
        finally:
            self.password = None


class OTPButtonView(ui.View):
    def __init__(self, bot, owner_id, phone, password, device, reference, prefix):
        super().__init__(timeout=300)
        self.bot = bot
        self.owner_id = owner_id
        self.phone = phone
        self.password = password
        self.device = device
        self.reference = reference
        self.prefix = prefix

    @ui.button(label='OTPを入力', style=discord.ButtonStyle.primary)
    async def open_otp(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.owner_id or interaction.user.id != self.bot.config.owner_id:
            return await interaction.response.send_message('権限がありません。', ephemeral=True)
        await interaction.response.send_modal(
            OTPModal(self.bot, self.owner_id, self.phone, self.password,
                     self.device, self.reference, self.prefix)
        )
        button.disabled = True
        self.stop()


class LoginModal(ui.Modal, title='PayPayログイン'):
    phone = ui.TextInput(label='電話番号', placeholder='09012345678', max_length=20)
    password = ui.TextInput(
        label='パスワード', style=discord.TextStyle.short, max_length=128,
    )

    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.bot.config.owner_id:
            return await interaction.response.send_message('権限がありません。', ephemeral=True)
        # Start a completely new login. Never load or reuse the previous device UUID.
        device = str(uuid.uuid4())
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            response = await asyncio.wait_for(
                paypayu.login(self.phone.value, self.password.value, device),
                timeout=30,
            )
            if isinstance(response, dict) and response.get('access_token'):
                save_login(interaction.user.id, self.phone.value, self.password.value,
                           device)
                return await interaction.followup.send(
                    'ログインが完了しました。端末情報を保存しました。', ephemeral=True,
                )
            if not isinstance(response, dict) or not response.get('otp_reference_id'):
                return await interaction.followup.send(
                    'ログインに失敗しました。電話番号・パスワード・認証状態をご確認ください。',
                    ephemeral=True,
                )
            # The modal is already submitted. A separate button opens the OTP modal.
            view = OTPButtonView(
                self.bot, interaction.user.id, self.phone.value,
                self.password.value, device,
                response['otp_reference_id'], response.get('otp_prefix', ''),
            )
            await interaction.followup.send(
                'PayPayからSMSが届いたら「OTPを入力」を押してください。',
                view=view, ephemeral=True,
            )
        except Exception:
            log.exception('PayPay fresh login failed')
            await interaction.followup.send(
                'PayPayとの通信に失敗しました。時間をおいて再度お試しください。',
                ephemeral=True,
            )


class PayPay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name='paypay_login', description='PayPayに新しくログインします')
    async def login(self, interaction: discord.Interaction):
        if interaction.user.id != self.bot.config.owner_id:
            return await interaction.response.send_message('権限がありません。', ephemeral=True)
        await interaction.response.send_modal(LoginModal(self.bot))


async def make(bot):
    return PayPay(bot)
