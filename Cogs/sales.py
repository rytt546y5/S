from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

from services import paypayu
from services.sms_api_base import APIError, validate_service
from services.sms_provider import sms_items, OutOfStockError, public_phone
from storage import (achievement_needs_review, achievement_posted, achievement_received,
                     claim_achievement_post, get_achievement_post,
                     active_orders, buyer_has_unfinished, claim_status, consume_approval_try, db,
                     get_order, get_setting, initialize, load_login, make_order, make_test_order,
                     mark_sms_received,
                     UnfinishedOrderError, mark_sms,
                     order_by_code, set_setting, unseen_sms, update_order)

log=logging.getLogger(__name__)
MAX_SWAPS=int(os.getenv('MAX_REPLACEMENTS','2'))
POLL_SECONDS=max(15,int(os.getenv('SMS_POLL_SECONDS','20')))
MIN_CANCEL_AGE_SECONDS=240
MIN_PRICE = 600
MAX_APPROVAL_ATTEMPTS = 3
APPROVAL_TIMEOUT = 600


def current_price() -> int:
    raw = get_setting('price_yen', os.getenv('PRICE_YEN', str(MIN_PRICE)))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = MIN_PRICE
    return max(MIN_PRICE, value)



def panel_embed(price: int, title: str = 'SMS認証番号'):
    return discord.Embed(title=title, description=f'**料金**\n```{price}円```\n購入する場合は下のボタンを押してください。', color=0x3498db)


def achievement_embed(order: dict, *, received: bool = False) -> discord.Embed:
    status = 'SMS受信済み' if received else '番号発行済み'
    kind = 'テスト注文（PayPay決済なし）' if order['source'] == 'test' else 'PayPay購入'
    description = f'**購入者**\n<@{int(order["buyer_id"])}>\n\n**区分**\n```{kind}```'
    if order['source'] != 'test':
        description += f'\n**購入金額**\n```{int(order["amount"])}円```'
    description += f'\n**状態**\n```{status}```'
    return discord.Embed(title='SMS認証 実績', description=description, color=0x3498DB)


def notify(title: str, description: str):
    return discord.Embed(title=title,description=description,color=0x3498db)


async def dm(bot, buyer_id: int, embed, view=None):
    user=bot.get_user(buyer_id) or await bot.fetch_user(buyer_id)
    return await user.send(embed=embed,view=view,allowed_mentions=discord.AllowedMentions.none())


class BuyModal(ui.Modal,title='SMS番号の購入'):
    link=ui.TextInput(label='PayPay送金リンク',placeholder='https://pay.paypay.ne.jp/...',max_length=200)

    def __init__(self, bot, service_key: str, country_id: int):
        super().__init__(title=f'SMS番号の注文（{current_price()}円）', timeout=180)
        self.bot = bot
        self.service_key = service_key
        self.country_id = country_id

    async def on_submit(self, interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True,thinking=True)
        service=self.bot.get_cog('Sales')
        if not service: return await interaction.followup.send('サービスを利用できません。',ephemeral=True)
        await service.purchase(interaction, self.link.value, self.service_key, self.country_id)


class SalesPanel(ui.View):
    def __init__(self, bot, service_key: str, country_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.service_key, self.country_id = validate_service(service_key, country_id)
        button = ui.Button(label='購入する', style=discord.ButtonStyle.primary,
                           custom_id=f'sms:buy:{service_key}:{country_id}')
        button.callback = self.buy
        self.add_item(button)

    async def buy(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message('サーバー内で操作してください。', ephemeral=True)
        await interaction.response.send_modal(BuyModal(self.bot, self.service_key, self.country_id))


async def service_autocomplete(interaction: discord.Interaction, current: str):
    try:
        country = getattr(interaction.namespace, 'country', None) or 182
        offers = await interaction.client.sms.available_services(int(country))
    except Exception:
        return []
    word = current.casefold()
    selected = [offer for offer in offers if word in (offer['service'] + ' ' + offer['name']).casefold()]
    return [app_commands.Choice(name=f"{offer['name'][:65]} ({offer['service']})"[:100],
                                value=offer['service']) for offer in selected[:25]]


class ReplacementView(ui.View):
    def __init__(self, bot, order_id: int, generation: int, disabled: bool = True):
        super().__init__(timeout=None)
        self.bot = bot
        self.order_id = int(order_id)
        self.generation = int(generation)
        button = ui.Button(label='番号を交換する', style=discord.ButtonStyle.secondary,
                           custom_id=f'sms:replace:{order_id}:{generation}', disabled=disabled)
        button.callback = self.replace
        self.add_item(button)

    async def replace(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        service = self.bot.get_cog('Sales')
        if not service:
            return await interaction.followup.send('サービスを利用できません。', ephemeral=True)
        await service.replace_latest(interaction, self.order_id, self.generation)


class HoldApprovalView(ui.View):
    """Approval attempts are persisted for ten minutes."""

    def __init__(self, bot, order_id: int, buyer_id: int):
        super().__init__(timeout=APPROVAL_TIMEOUT)
        self.bot = bot
        self.order_id = order_id
        self.buyer_id = buyer_id
        self.message = None
        self.confirm.label = f'承認しました（残り{MAX_APPROVAL_ATTEMPTS}回）'

    async def finish(self, label: str, description: str):
        self.confirm.disabled = True
        self.confirm.label = label
        self.stop()
        if self.message:
            try:
                await self.message.edit(embed=notify('PayPay承認確認', description), view=self)
            except discord.HTTPException:
                log.warning('Approval ephemeral panel update failed: %s', self.order_id)

    @ui.button(label='承認しました', style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.buyer_id:
            return await interaction.response.send_message('購入した本人のみ操作できます。', ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = self.bot.get_cog('Sales')
        if cog is None:
            return await interaction.followup.send('現在確認できません。', ephemeral=True)
        result, remaining = await cog.confirm_hold(self.order_id, self.buyer_id)
        if result == 'paid':
            await self.finish('購入完了', '入金を確認しました。番号発行の詳細はDMでご確認ください。')
            await interaction.followup.send('入金を確認しました。DMをご確認ください。', ephemeral=True)
        elif result == 'pending':
            self.confirm.label = f'承認しました（残り{remaining}回）'
            if self.message:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass
            await interaction.followup.send('まだ承認を確認できませんでした。PayPayアプリで承認してから再度確認してください。', ephemeral=True)
        elif result == 'review':
            await self.finish('要確認', 'リンクの状態が変化しました。入金先を確認できるまで番号は発行されません。管理者にお問い合わせください。')
            await interaction.followup.send('管理者による入金確認が必要です。', ephemeral=True)
        elif result == 'limit':
            await self.finish('確認上限', '確認は3回までです。承認済みの場合は管理者へお問い合わせください。')
            await interaction.followup.send('確認回数の上限に達しました。', ephemeral=True)
        elif result == 'expired':
            await self.finish('受付終了', '10分の確認期限が切れました。承認済みの場合は管理者へお問い合わせください。')
            await interaction.followup.send('確認期限が切れました。', ephemeral=True)
        else:
            await interaction.followup.send('確認できない状態です。管理者へお問い合わせください。', ephemeral=True)

    async def on_timeout(self):
        cog = self.bot.get_cog('Sales')
        if cog:
            lock = cog.hold_locks.setdefault(self.order_id, asyncio.Lock())
            async with lock:
                order = get_order(self.order_id)
                if order and order['status'] == 'hold':
                    update_order(self.order_id, status='needs_review', error='承認確認期限切れ：入金照合が必要')
        await self.finish('受付終了', '確認期限が切れました。承認済みの場合は管理者へお問い合わせください。')


class Sales(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.checkout_locks={}
        self.swap_locks={}
        self.hold_locks={}
        self.unlock_tasks = {}
        self.sms_lock_buttons_synced = set()
        self.poller.change_interval(seconds=POLL_SECONDS)
        self.poller.start()

    def cog_unload(self):
        self.poller.cancel()
        for task in self.unlock_tasks.values():
            task.cancel()

    def restore_replacement_views(self):
        """Register persistent per-order controls before the gateway starts."""
        for order in active_orders():
            if order['status'] != 'active' or not order.get('dm_message_id'):
                continue
            if order['swaps'] >= MAX_SWAPS:
                continue
            self.bot.add_view(ReplacementView(self.bot, order['id'], order['swaps']),
                              message_id=order['dm_message_id'])
            if order.get('sms_received_at'):
                asyncio.create_task(self.update_replacement_button(order['id']))
            else:
                self.schedule_unlock(order['id'])

    def schedule_unlock(self, order_id: int):
        old = self.unlock_tasks.pop(order_id, None)
        if old:
            old.cancel()
        order = get_order(order_id)
        if not order or order['status'] != 'active' or not order.get('dm_message_id') or order.get('sms_received_at'):
            return
        task = asyncio.create_task(self._unlock_when_ready(order_id, order['swaps'], order['issued_at']))
        self.unlock_tasks[order_id] = task
        task.add_done_callback(lambda t, oid=order_id: self.unlock_tasks.pop(oid, None) if self.unlock_tasks.get(oid) is t else None)

    async def _unlock_when_ready(self, order_id: int, generation: int, issued_at: int):
        await self.bot.wait_until_ready()
        if not issued_at:
            return
        await asyncio.sleep(max(0, issued_at + MIN_CANCEL_AGE_SECONDS - time.time()))
        order = get_order(order_id)
        if (order and order['status'] == 'active' and not order.get('sms_received_at')
                and order['swaps'] == generation and order['issued_at'] == issued_at):
            for attempt in range(5):
                try:
                    rows = [row for row in await self.bot.sms.active()
                            if str(row.get('id', row.get('activation_id', ''))) == order['sms_order_id']]
                    if len(rows) == 1:
                        if rows[0].get('received_sms'):
                            mark_sms_received(order_id, order['sms_order_id'])
                            await self.update_replacement_button(order_id)
                            return
                        if await self.update_replacement_button(order_id):
                            return
                except Exception:
                    log.warning('Unable to confirm SMS receipt before enabling exchange %s',
                                order_id, exc_info=True)
                await asyncio.sleep(min(15 * (attempt + 1), 60))

    async def update_replacement_button(self, order_id: int):
        order = get_order(order_id)
        if not order or not order.get('dm_message_id') or not order.get('dm_channel_id'):
            return False
        with db() as cx:
            received = cx.execute('SELECT 1 FROM seen_sms WHERE order_id=? LIMIT 1', (order_id,)).fetchone() is not None
        enabled = (order['status'] == 'active' and order['swaps'] < MAX_SWAPS and
                   order.get('issued_at') is not None and
                   time.time() >= order['issued_at'] + MIN_CANCEL_AGE_SECONDS and
                   (not order.get('expires_at') or time.time() < order['expires_at']) and
                   not received and not order.get('sms_received_at'))
        try:
            channel = self.bot.get_channel(order['dm_channel_id']) or await self.bot.fetch_channel(order['dm_channel_id'])
            msg = await channel.fetch_message(order['dm_message_id'])
            await msg.edit(view=ReplacementView(self.bot, order_id, order['swaps'], disabled=not enabled))
            if order.get('sms_received_at') and not enabled:
                self.sms_lock_buttons_synced.add((order_id, order['swaps']))
            return True
        except (discord.HTTPException, AttributeError, TypeError):
            log.warning('Cannot update number exchange button: order %s', order_id, exc_info=True)
            return False


    async def purchase(self, interaction, link, service_key: str, country_id: int):
        buyer=interaction.user.id
        lock=self.checkout_locks.setdefault(buyer,asyncio.Lock())
        async with lock:
            payment_notice = None
            local_id = None

            async def update_payment_notice(title: str, description: str):
                if payment_notice is None:
                    return
                try:
                    await payment_notice.edit(embed=notify(title, description))
                except discord.HTTPException:
                    log.warning('Could not update payment DM for order %s', local_id)

            code=paypayu.parse_link(link)
            if not code: return await interaction.followup.send('PayPayリンクが正しくありません。',ephemeral=True)
            if order_by_code(code): return await interaction.followup.send('この送金リンクは既に使用されています。',ephemeral=True)
            if buyer_has_unfinished(buyer):
                return await interaction.followup.send('処理中の注文があります。先に注文を完了してください。',ephemeral=True)
            price=current_price()
            try:
                # Do not accept PayPay money unless supplier stock and balance
                # are both verifiable for the requested item.
                await self.bot.sms.ensure_available(service_key, country_id)
                credentials = load_login(self.bot.config.owner_id)
                if not credentials or not all(credentials.get(field) for field in ('phone', 'password', 'uuid')):
                    return await interaction.followup.send(
                        '管理者のPayPayアカウントが登録されていません。', ephemeral=True)
                info=await paypayu.check_link(code)
                payload=info.get('payload') if isinstance(info,dict) else None
                pending=(payload or {}).get('pendingP2PInfo') or {}
                amount=pending.get('amount')
                if not isinstance(amount,int) or amount!=price:
                    return await interaction.followup.send(f'送金金額が一致しません。必要金額：`{price}円`',ephemeral=True)
                if pending.get('isSetPasscode'):
                    return await interaction.followup.send('パスコード付き送金リンクは利用できません。',ephemeral=True)
                order_id=pending.get('orderId') or ((payload or {}).get('message') or {}).get('data',{}).get('orderId')
                if not order_id:
                    return await interaction.followup.send('PayPayの注文番号を確認できません。',ephemeral=True)
                try:
                    payment_notice = await dm(self.bot,buyer,notify('注文受付','PayPayの受取処理を確認しています。'))
                except discord.Forbidden:
                    return await interaction.followup.send('DMを受信できるようにしてからもう一度お試しください。',ephemeral=True)
                try:
                    local_id=make_order(buyer,interaction.guild_id,code,price,str(order_id) if order_id else None, service_key, country_id)
                except sqlite3.IntegrityError:
                    return await interaction.followup.send('既に処理済みの送金リンクです。',ephemeral=True)
                try:
                    result=await paypayu.link_rev(cd=code, phoneNumber=credentials['phone'], password=credentials['password'], uuid=credentials['uuid'])
                except Exception:
                    log.exception('PayPay receive uncertainty, order %s',local_id)
                    update_order(local_id,status='payment_uncertain',error='PayPay結果不明')
                    await update_payment_notice('入金確認が必要です',
                        '受取結果を確認できませんでした。送金リンクを再送せず、管理者へお問い合わせください。')
                    return await interaction.followup.send('入金結果を確認できません。リンクを再送せず管理者へ連絡してください。',ephemeral=True)
                if result=='PENDING_HOLD':
                    update_order(local_id, status='hold', hold_deadline=int(time.time()) + APPROVAL_TIMEOUT,
                                 approval_tries=0)
                    await update_payment_notice('PayPay承認待ち',
                        'PayPayアプリで送金を承認してから、購入画面の「承認しました」を押してください。')
                    view = HoldApprovalView(self.bot, local_id, buyer)
                    description = ('PayPayで送金が一時保留されました。\n'
                                   'PayPayアプリで送金を承認してから、下のボタンを押してください。\n\n'
                                   f'**支払金額**\n```{price}円```\n'
                                   f'確認回数：{MAX_APPROVAL_ATTEMPTS}回 ／ 期限：10分')
                    view.message = await interaction.followup.send(
                        embed=notify('PayPay承認待ち', description), view=view,
                        ephemeral=True, wait=True)
                    try:
                        await dm(self.bot, buyer, notify('PayPay承認待ち',
                                 'PayPayで保留が発生しました。PayPayアプリで承認し、購入時の画面にある「承認しました」を押してください。'))
                    except discord.HTTPException:
                        log.warning('Could not notify held buyer %s', buyer)
                    return
                if result is not True:
                    if result == 'LOGINERR':
                        reason = 'PayPay再認証が必要です'
                        detail = '管理者のPayPayの再認証が必要です。送金リンクは再送せず、管理者へお問い合わせください。'
                    else:
                        reason = '入金確認が必要です'
                        detail = 'PayPayの受取結果が不明です。送金リンクを再送しないでください。管理者が入金を照合します。'
                    update_order(local_id, status='payment_uncertain', error=reason)
                    await update_payment_notice(reason, detail)
                    return await interaction.followup.send(detail, ephemeral=True)
                update_order(local_id,status='paid_unfulfilled')
                await update_payment_notice('PayPay入金確認済み', '入金を確認しました。電話番号を発行しています。')
                issue_result = await self.issue_number(local_id)
                if issue_result in ('active', 'active_dm_failed'):
                    await update_payment_notice('電話番号発行完了',
                        '入金と番号発行を確認しました。新しいDMをご確認ください。')
                else:
                    await update_payment_notice('番号発行の確認が必要です',
                        '入金を確認しましたが番号の発行が完了していません。管理者へお問い合わせください。')
                await interaction.followup.send('入金を確認しました。番号の発行状況をDMでご確認ください。',ephemeral=True)
            except OutOfStockError:
                await update_payment_notice('在庫がありません。', '番号の購入残高が不足しています。管理者へお問い合わせください。')
                await interaction.followup.send('在庫がありません。', ephemeral=True)
            except APIError:
                if local_id is not None and (get_order(local_id) or {}).get('status') == 'checking':
                    update_order(local_id, status='payment_uncertain', error='PayPay処理中のAPIエラー：入金照合が必要')
                await update_payment_notice('確認できませんでした', '処理結果を確認できませんでした。リンクを再送せず管理者へお問い合わせください。')
                await interaction.followup.send('番号を準備できませんでした。管理者へお問い合わせください。',ephemeral=True)
            except Exception:
                log.exception('Unexpected purchase error for order %s', local_id)
                if local_id is not None and (get_order(local_id) or {}).get('status') == 'checking':
                    update_order(local_id, status='payment_uncertain', error='受取中断：入金照合が必要')
                await update_payment_notice('入金確認が必要です', '処理中にエラーが発生しました。送金リンクを再送せず管理者へお問い合わせください。')
                await interaction.followup.send('処理中にエラーが発生しました。管理者にお問い合わせください。',ephemeral=True)

    async def confirm_hold(self, order_id: int, buyer_id: int) -> tuple[str, int]:
        """Internal operation."""
        lock = self.hold_locks.setdefault(order_id, asyncio.Lock())
        async with lock:
            order = get_order(order_id)
            if not order or order['buyer_id'] != buyer_id or order['status'] != 'hold':
                return 'closed', 0
            now = int(time.time())
            if not order['hold_deadline'] or now > order['hold_deadline']:
                update_order(order_id, status='needs_review', error='承認期限切れ：入金要確認')
                return 'expired', 0
            used = consume_approval_try(order_id, now, MAX_APPROVAL_ATTEMPTS)
            if used is None:
                return 'closed', 0
            remaining = MAX_APPROVAL_ATTEMPTS - used
            try:
                status = await paypayu.get_link_status(order['pay_code'])
            except Exception:
                log.exception('PayPay link status lookup failed: %s', order_id)
                status = None
            if status and status != 'PENDING':
                update_order(order_id, status='needs_review',
                             error='保留解除の可能性あり：受取先の入金証明が必要')
                return 'review', remaining
            if remaining == 0:
                update_order(order_id, status='needs_review', error='承認確認上限：入金要確認')
                return 'limit', 0
            return 'pending', remaining

    async def publish_achievement(self, local_id: int) -> None:
        """One non-sensitive channel post for a successfully issued order."""
        order = get_order(local_id)
        if not order or order['status'] != 'active' or not order.get('sms_order_id'):
            return
        setting = get_setting(f'achievement_channel:{order["guild_id"]}', '')
        if not setting or not setting.isdecimal():
            return
        channel_id = int(setting)
        if not claim_achievement_post(local_id, channel_id):
            return
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != order['guild_id']:
                raise ValueError('実績チャンネルが無効です')
            msg = await channel.send(
                embed=achievement_embed(order),
                allowed_mentions=discord.AllowedMentions.none())
            achievement_posted(local_id, msg.id)
        except Exception:
            # If the request reached Discord but its reply failed, retrying can duplicate posts.
            achievement_needs_review(local_id)
            log.exception('Achievement post requires review: order %s', local_id)

    async def refresh_achievement(self, local_id: int) -> None:
        """On first delivered SMS, edit the already-posted achievement (never reveal SMS)."""
        try:
            record = get_achievement_post(local_id)
            if not record or record['status'] != 'posted' or not record['message_id']:
                return
            order = get_order(local_id)
            if not order:
                return
            channel = self.bot.get_channel(record['channel_id']) or await self.bot.fetch_channel(record['channel_id'])
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != order['guild_id']:
                raise ValueError('実績チャンネルが無効です')
            message = await channel.fetch_message(record['message_id'])
            await message.edit(embed=achievement_embed(order, received=True),
                               allowed_mentions=discord.AllowedMentions.none())
            achievement_received(local_id)
        except Exception:
            # A failed edit is not allowed to interfere with private SMS delivery.
            log.exception('Achievement update failed: order %s', local_id)

    async def issue_number(self, local_id: int) -> str:
        """Send one activation request for a durable order, never auto-retry uncertainty."""
        order = get_order(local_id)
        if not order or order['status'] != 'paid_unfulfilled':
            return 'invalid'
        if not claim_status(local_id, 'paid_unfulfilled', 'issuing'):
            return 'invalid'
        is_test = order.get('source') == 'test'
        try:
            if not order.get('sms_service_key') or not order.get('sms_country_id'):
                raise APIError('注文にサービス指定がありません。')
            sms_order_id, number, expires_at = await self.bot.sms.buy(
                order['sms_service_key'], int(order['sms_country_id']))
        except OutOfStockError:
            update_order(local_id, status='failed_no_stock' if is_test else 'paid_unfulfilled',
                         error='在庫がありません。')
            if not is_test:
                try:
                    await dm(self.bot, order['buyer_id'], notify('在庫がありません。',
                             '入金は確認済みですが、番号を発行できませんでした。管理者へお問い合わせください。'))
                except discord.HTTPException:
                    pass
            return 'no_stock'
        except Exception:
            log.exception('SMS activation failed or outcome uncertain for order %s', local_id)
            update_order(local_id, status='needs_review',
                         error='番号発行の成否不明：管理者の手動照合が必要')
            try:
                text = ('テスト注文の発行結果を確認できません。管理用の有効注文一覧を調べ、'
                        '重複購入を避けてください。' if is_test else
                        '入金は確認されていますが番号の発行結果を確認できません。管理者が対応します。')
                await dm(self.bot, order['buyer_id'],
                         notify('番号発行の確認が必要です', text))
            except discord.HTTPException:
                pass
            return 'needs_review'
        update_order(local_id, status='active', sms_order_id=sms_order_id,
                     phone=number, expires_at=expires_at, issued_at=math.ceil(time.time()),
                     dm_message_id=None, dm_channel_id=None, sms_received_at=None, error=None)
        result = 'active'
        try:
            expiry_text = (f'\n有効期限：`{datetime.fromtimestamp(expires_at,timezone(timedelta(hours=9))):%Y-%m-%d %H:%M} JST`'
                           if expires_at else '')
            exchange_text = ('\n\n使えない場合は発行から4分後に「番号を交換する」ボタンが使えるようになります。'
                             if self.bot.sms.replacement_ready and order['swaps'] < MAX_SWAPS else
                             '\n\n番号交換の上限に達しました。' if order['swaps'] >= MAX_SWAPS else
                             '\n\n現在、自動番号交換は利用できません。問題がある場合は管理者へ連絡してください。')
            latest = get_order(local_id)
            view = ReplacementView(self.bot, local_id, latest['swaps'], disabled=True) if self.bot.sms.replacement_ready and latest['swaps'] < MAX_SWAPS else None
            title = 'テスト番号を発行しました' if is_test else '電話番号を発行しました'
            sent = await dm(self.bot, order['buyer_id'],
                     notify(title, f'**電話番号**\n```{public_phone(number)}```{expiry_text}\n'
                                   f'この番号を認証するサービスに入力し、SMSを送信してください。'
                                   f'受信後、このDMにお知らせします。{exchange_text}'), view)
            if view and sent:
                update_order(local_id, dm_message_id=sent.id, dm_channel_id=sent.channel.id)
                self.schedule_unlock(local_id)
        except discord.HTTPException:
            log.warning('SMS number issued but DM delivery failed: order %s', local_id)
            update_order(local_id, error='発行完了・DM送信失敗')
            result = 'active_dm_failed'
        # Publishing is not part of the payment / activation success condition.
        try:
            await self.publish_achievement(local_id)
        except Exception:
            log.exception('Achievement logging failure must not change activated order %s', local_id)
        return result

    async def replace_latest(self,interaction, expected_order_id: int, expected_swaps: int):
        if not self.bot.sms.replacement_ready:
            return await interaction.followup.send('番号のキャンセルAPIが未設定のため、自動交換は現在利用できません。管理者へ連絡してください。',ephemeral=True)
        user=interaction.user.id
        lock=self.swap_locks.setdefault(user,asyncio.Lock())
        async with lock:
            local_id = expected_order_id
            order=get_order(local_id)
            if (not order or order['buyer_id'] != user or order['status'] != 'active' or
                    order['swaps'] != expected_swaps):
                return await interaction.followup.send('この番号の交換操作は終了しています。新しいDMをご確認ください。',ephemeral=True)
            if order.get('sms_received_at'):
                await self.update_replacement_button(local_id)
                return await interaction.followup.send('認証コードを受信済みのため、電話番号は変更できません。', ephemeral=True)
            if order['swaps']>=MAX_SWAPS:
                return await interaction.followup.send('交換可能回数の上限です。管理者へ連絡してください。',ephemeral=True)
            if not order.get('issued_at'):
                return await interaction.followup.send('発行時刻を確認できません。管理者へお問い合わせください。',ephemeral=True)
            since_issued=int(time.time())-int(order['issued_at'])
            if since_issued < MIN_CANCEL_AGE_SECONDS:
                wait=MIN_CANCEL_AGE_SECONDS-since_issued
                return await interaction.followup.send(f'番号発行後、交換可能になるまであと約{(wait + 59) // 60}分です。',ephemeral=True)
            try:
                matches=[r for r in await self.bot.sms.active() if str(r.get('id',r.get('activation_id',''))) == order['sms_order_id']]
                if len(matches)!=1:
                    return await interaction.followup.send('現在の番号をAPIで確認できません。管理者へ連絡してください。',ephemeral=True)
                if matches[0].get('received_sms'):
                    mark_sms_received(local_id, order['sms_order_id'])
                    await self.update_replacement_button(local_id)
                    return await interaction.followup.send('認証コードを受信済みのため、電話番号は変更できません。',ephemeral=True)
                with db() as cx:
                    seen=cx.execute('SELECT 1 FROM seen_sms WHERE order_id=? LIMIT 1',(local_id,)).fetchone()
                if seen: return await interaction.followup.send('認証コードを受信済みのため、電話番号は変更できません。',ephemeral=True)
                if get_order(local_id).get('sms_received_at'):
                    await self.update_replacement_button(local_id)
                    return await interaction.followup.send('認証コードを受信済みのため、電話番号は変更できません。', ephemeral=True)
                update_order(local_id,status='replacing')
                await self.update_replacement_button(local_id)
                try:
                    cancelled = await self.bot.sms.cancel(order['sms_order_id'], order['sms_service_key'])
                except APIError as exc:
                    log.warning('Cancellation not confirmed for order %s: %s',local_id,exc)
                    # When the remote order is explicitly still active, the buyer may retry.
                    # Missing, inactive, or inconsistent remote status requires manual review.
                    try:
                        latest_rows=[r for r in await self.bot.sms.active()
                                     if str(r.get('id',''))==order['sms_order_id']]
                        if len(latest_rows)==1 and latest_rows[0].get('is_active') is True:
                            if latest_rows[0].get('received_sms'):
                                mark_sms_received(local_id, order['sms_order_id'])
                                update_order(local_id,status='active',error='SMSが到着しているため交換できません')
                                await self.update_replacement_button(local_id)
                                return await interaction.followup.send('SMSを受信済みのため交換できません。DMをご確認ください。',ephemeral=True)
                            update_order(local_id,status='active',error='番号のキャンセルを確認できませんでした')
                            await self.update_replacement_button(local_id)
                            return await interaction.followup.send('現在この番号はキャンセルできません。時間を空けてもう一度お試しください。',ephemeral=True)
                    except Exception:
                        log.warning('Reconciliation failed for cancellation %s',local_id,exc_info=True)
                    update_order(local_id,status='needs_review',error='キャンセル結果不明')
                    return await interaction.followup.send('キャンセル結果を確認できません。重複発行を避けるため停止しました。',ephemeral=True)
                except Exception:
                    log.exception('Cancellation uncertain for order %s',local_id)
                    update_order(local_id,status='needs_review',error='キャンセル結果不明')
                    return await interaction.followup.send('キャンセル結果を確認できません。重複発行を避けるため停止しました。',ephemeral=True)
                # A message could arrive between the read-before-cancel and the cancel response.
                # If so, deliver it but never issue a replacement number.
                received = sms_items(cancelled)
                if cancelled.get('received_sms'):
                    mark_sms_received(local_id, order['sms_order_id'])
                    update_order(local_id,status='cancelled_sms',error='キャンセル時にSMS受信を確認')
                    await self.update_replacement_button(local_id)
                    for message in received:
                        fp=hashlib.sha256(message.encode('utf-8')).hexdigest()
                        if unseen_sms(local_id,fp):
                            try:
                                await dm(self.bot,user,notify('SMSを受信しました',
                                    f'**受信内容**\n```{discord.utils.escape_markdown(message[:1200]).replace("`", "ʼ")}```\n\n'
                                    '認証コードを受信したため、この電話番号は交換できません。'))
                                mark_sms(local_id,fp)
                                await self.refresh_achievement(local_id)
                            except discord.HTTPException:
                                log.warning('SMS DM failed after cancellation for order %s',local_id)
                    return await interaction.followup.send('キャンセル処理の応答でSMS受信が確認されました。番号の再発行は行っていません。',ephemeral=True)
                if get_order(local_id).get('sms_received_at'):
                    update_order(local_id, status='cancelled_sms', error='SMS受信済みのため再発行停止')
                    await self.update_replacement_button(local_id)
                    return await interaction.followup.send('認証コードが届いているため、新しい番号は発行しません。',ephemeral=True)
                update_order(local_id,status='paid_unfulfilled',sms_order_id=None,phone=None,expires_at=None,
                             issued_at=None, dm_message_id=None,dm_channel_id=None,swaps=order['swaps']+1)
                await self.issue_number(local_id)
                latest=get_order(local_id)
                if latest and latest['status']=='active':
                    await interaction.followup.send('旧番号をキャンセルし、新しい番号を発行しました。DMをご確認ください。',ephemeral=True)
                else:
                    await interaction.followup.send('旧番号はキャンセルしましたが、新しい番号の発行確認が必要です。管理者に連絡してください。',ephemeral=True)
            except APIError:
                log.warning('Number exchange API error', exc_info=True)
                await interaction.followup.send('番号交換を確認できませんでした。管理者にお問い合わせください。',ephemeral=True)
            except Exception:
                log.exception('Replacement failure')
                await interaction.followup.send('交換処理が失敗しました。管理者へ連絡してください。',ephemeral=True)

    @tasks.loop(seconds=20)
    async def poller(self):
        orders=active_orders()
        if not orders:return
        for order in orders:
            if (order['status']=='active' and order['expires_at'] is not None and
                int(order['expires_at']) <= int(time.time())):
                update_order(order['id'],status='expired')
                await self.update_replacement_button(order['id'])
        orders=active_orders()
        sms_orders=[o for o in orders if o['status']=='active' and o['sms_order_id']]
        holds=[o for o in orders if o['status']=='hold']
        for order in holds:
            if order['hold_deadline'] and int(time.time()) > order['hold_deadline']:
                update_order(order['id'], status='needs_review', error='承認期限切れ：入金要確認')
        if sms_orders:
            try: remote=await self.bot.sms.active()
            except Exception:
                log.exception('SMS poll failed')
                remote=[]
            active_map={str(r.get('id',r.get('activation_id',''))):r for r in remote if isinstance(r,dict)}
            for order in sms_orders:
                row=active_map.get(order['sms_order_id'])
                if not row:continue
                if row.get('received_sms'):
                    # Record the SMS arrival BEFORE attempting a DM. A blocked DM
                    # must never allow another phone number to be purchased.
                    first_arrival = mark_sms_received(order['id'], order['sms_order_id'])
                    if first_arrival:
                        unlock = self.unlock_tasks.pop(order['id'], None)
                        if unlock:
                            unlock.cancel()
                    if (first_arrival or (order['id'], order['swaps']) not in self.sms_lock_buttons_synced):
                        await self.update_replacement_button(order['id'])
                for message in sms_items(row):
                    fp=hashlib.sha256(message.encode('utf-8')).hexdigest()
                    if not unseen_sms(order['id'],fp):continue
                    try:
                        await dm(self.bot,order['buyer_id'],notify('SMSを受信しました',
                            f'**受信内容**\n```{discord.utils.escape_markdown(message[:1200]).replace("`", "ʼ")}```\n\n'
                            '認証コードを受信したため、この電話番号は交換できません。'))
                    except discord.HTTPException:
                        log.warning('SMS DM failed for order %s',order['id'])
                        continue
                    mark_sms(order['id'],fp)
                    await self.update_replacement_button(order['id'])
                    await self.refresh_achievement(order['id'])
    @poller.before_loop
    async def before_poll(self): await self.bot.wait_until_ready()

    def restore_sales_panels(self):
        import json
        try:
            items = json.loads(get_setting('panels', '[]'))
        except (ValueError, TypeError):
            return
        for item in items:
            try:
                service = item.get('service_key')
                country = item.get('country_id')
                if service and country:
                    self.bot.add_view(SalesPanel(self.bot, service, int(country)),
                                      message_id=int(item['message_id']))
            except (TypeError, ValueError, KeyError):
                log.warning('Invalid saved sales panel entry')

    async def remember_panel(self, channel_id: int, message_id: int, service_key: str, country_id: int, title: str):
        import json
        try:
            ids = json.loads(get_setting('panels', '[]'))
        except (ValueError, TypeError):
            ids = []
        ids = [x for x in ids if not (x.get('channel_id') == channel_id and x.get('message_id') == message_id)]
        ids.append({'channel_id': channel_id, 'message_id': message_id, 'service_key': service_key, 'country_id': country_id, 'title': title})
        set_setting('panels', json.dumps(ids[-50:]))

    async def refresh_panels(self, price: int) -> int:
        import json
        try:
            ids = json.loads(get_setting('panels', '[]'))
        except (ValueError, TypeError):
            ids = []
        updated = 0
        for item in ids:
            try:
                channel = self.bot.get_channel(item['channel_id']) or await self.bot.fetch_channel(item['channel_id'])
                message = await channel.fetch_message(item['message_id'])
                if not item.get('service_key') or not item.get('country_id'):
                    await message.edit(embed=notify('販売受付停止', '管理者がサービスを指定してパネルを設置し直してください。'), view=None)
                    continue
                await message.edit(embed=panel_embed(price, item.get('title', 'SMS認証番号')),
                                   view=SalesPanel(self.bot, item['service_key'], int(item['country_id'])))
                updated += 1
            except (KeyError, discord.HTTPException):
                log.warning('Could not refresh SMS purchase panel')
        return updated

    @app_commands.command(name='実績チャンネル選択', description='SMS番号の発行実績を投稿するチャンネルを指定します')
    @app_commands.guild_only()
    @app_commands.describe(channel='発行実績を投稿するテキストチャンネル')
    async def set_achievement_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.user.id != self.bot.config.owner_id:
            return await interaction.response.send_message('利用権限がありません。', ephemeral=True)
        if not interaction.guild or channel.guild.id != interaction.guild.id:
            return await interaction.response.send_message('同じサーバーのチャンネルを選んでください。', ephemeral=True)
        member = interaction.guild.me or interaction.guild.get_member(self.bot.user.id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(self.bot.user.id)
            except discord.HTTPException:
                return await interaction.response.send_message('Botの権限を確認できません。', ephemeral=True)
        permissions = channel.permissions_for(member)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            return await interaction.response.send_message(
                'Botに「チャンネルを見る」「メッセージを送信」「埋め込みリンク」の権限が必要です。', ephemeral=True)
        set_setting(f'achievement_channel:{interaction.guild.id}', str(channel.id))
        await interaction.response.send_message(
            f'実績チャンネルを {channel.mention} に設定しました。\n'
            '番号発行成功時に実績を投稿し、SMS受信後は同じ投稿を更新します。', ephemeral=True)

    @app_commands.command(name='注文', description='管理者が番号1件を直接テスト発行します（残高消費）')
    @app_commands.guild_only()
    @app_commands.describe(service='認証サービスのコード', country='国コード（日本：182）')
    @app_commands.autocomplete(service=service_autocomplete)
    async def order(self, interaction: discord.Interaction, service: str, country: int = 182):
        if interaction.user.id != self.bot.config.owner_id:
            return await interaction.response.send_message('利用権限がありません。', ephemeral=True)
        try:
            service, country = validate_service(service, country)
        except APIError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        owner_id = interaction.user.id
        # Use the same lock as regular purchases; no PayPay link or receipt is used.
        lock = self.checkout_locks.setdefault(owner_id, asyncio.Lock())
        async with lock:
            if buyer_has_unfinished(owner_id):
                return await interaction.followup.send(
                    '処理中の注文があります。`/sms注文確認` で状態を確認してください。', ephemeral=True)
            try:
                status = await self.bot.sms.ensure_available(service, country)
            except OutOfStockError:
                return await interaction.followup.send('在庫がありません。', ephemeral=True)
            except APIError as exc:
                return await interaction.followup.send(
                    '残高・料金を確認できませんでした。設定を確認してください。', ephemeral=True)
            except Exception:
                log.exception('Test order balance verification failure')
                return await interaction.followup.send(
                    '仕入れ用残高を確認できませんでした。注文は行っていません。', ephemeral=True)
            try:
                local_id = make_test_order(owner_id, interaction.guild_id, service, country)
            except UnfinishedOrderError:
                return await interaction.followup.send(
                    '処理中の注文があります。`/sms注文確認` で状態を確認してください。', ephemeral=True)
            except Exception:
                log.exception('Test order record creation failure')
                return await interaction.followup.send(
                    '注文を記録できなかったため、発行は実行していません。', ephemeral=True)

            # The non-idempotent POST happens exactly once through issue_number.
            # Never call it again automatically if the response cannot be verified.
            result = await self.issue_number(local_id)
            if result in ('active', 'active_dm_failed'):
                row = get_order(local_id)
                if not row or not row.get('phone'):
                    return await interaction.followup.send(
                        '発行記録を確認できません。管理者が有効注文を確認してください。', ephemeral=True)
                expiry_text = (f'\n有効期限：<t:{row["expires_at"]}:f>' if row.get('expires_at') else '')
                note = ('\nDMを送れなかったため、ここで番号を確認してください。'
                        if result == 'active_dm_failed' else '\nSMS受信内容はDMに通知します。')
                return await interaction.followup.send(
                    embed=notify('テスト注文：発行完了',
                                 f'**電話番号**\n```{public_phone(row["phone"])}```\n'
                                 f'注文番号：`{row["sms_order_id"]}`'
                                 f'{expiry_text}{note}'), ephemeral=True)
            if result == 'no_stock':
                return await interaction.followup.send('在庫がありません。', ephemeral=True)
            return await interaction.followup.send(
                '番号購入の結果を確認できません。自動再発行は行いません。'
                '`/sms管理` と仕入れ用の有効注文を確認してください。', ephemeral=True)

    @app_commands.command(name='smsパネル設置',description='SMS認証番号の購入パネルを設置します')
    @app_commands.describe(service='認証サービスのコード', country='国コード（日本：182）', title='販売パネルの表示名')
    @app_commands.autocomplete(service=service_autocomplete)
    async def setup_panel(self, interaction:discord.Interaction, service: str,
                          country: int = 182, title: str = 'SMS認証番号'):
        if interaction.user.id!=self.bot.config.owner_id:
            return await interaction.response.send_message('権限がありません。',ephemeral=True)
        try:
            service, country = validate_service(service, country)
            if not 1 <= len(title) <= 80:
                return await interaction.response.send_message('表示名は1～80文字にしてください。', ephemeral=True)
        except APIError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.sms.offering(service, country)
        except APIError:
            return await interaction.followup.send('選択したサービスの料金を確認できません。', ephemeral=True)
        price=current_price()
        try:
            message = await interaction.channel.send(embed=panel_embed(price, title),
                                                     view=SalesPanel(self.bot, service, country))
            await self.remember_panel(interaction.channel_id, message.id, service, country, title)
        except discord.HTTPException:
            return await interaction.followup.send('パネルを設置できません。Botの送信権限を確認してください。', ephemeral=True)
        await interaction.followup.send('購入パネルを設置しました。', ephemeral=True)

    @app_commands.command(name='sms価格設定',description='SMS番号の販売金額を変更します')
    async def set_price(self,interaction:discord.Interaction,price:int):
        if interaction.user.id!=self.bot.config.owner_id:
            return await interaction.response.send_message('権限がありません。',ephemeral=True)
        if not (MIN_PRICE<=price<=100000):
            return await interaction.response.send_message(f'{MIN_PRICE}～100,000円で設定してください。',ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        set_setting('price_yen',str(price))
        updated = await self.refresh_panels(price)
        await interaction.followup.send(f'販売価格を `{price}円` に設定しました。公開パネル更新：`{updated}件`',ephemeral=True)

    @app_commands.command(name='sms在庫確認', description='番号の在庫と購入可否を確認します')
    @app_commands.describe(service='認証サービスのコード', country='国コード（日本：182）')
    @app_commands.autocomplete(service=service_autocomplete)
    async def stock_check(self, interaction: discord.Interaction, service: str, country: int = 182):
        if interaction.user.id != self.bot.config.owner_id:
            return await interaction.response.send_message('権限がありません。', ephemeral=True)
        try:
            service, country = validate_service(service, country)
        except APIError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            status = await self.bot.sms.stock_snapshot(service, country)
        except APIError as exc:
            return await interaction.followup.send(embed=notify('在庫確認',
                '購入前の確認に失敗しました。管理者へお問い合わせください。'), ephemeral=True)
        except Exception:
            log.exception('Inventory diagnostic failed')
            return await interaction.followup.send('在庫情報を取得できませんでした。', ephemeral=True)
        if not status['available']:
            return await interaction.followup.send(embed=notify('在庫確認',
                f'在庫がありません。\n\n残高：`{status["balance"]}`\n購入に必要：`{status["cost"]}`'), ephemeral=True)
        return await interaction.followup.send(embed=notify('在庫確認',
            f'購入可能：`はい`\n残高：`{status["balance"]}`\n'
            f'番号購入料金：`{status["cost"]}`\n料金取得元：`{status["cost_source"]}`'), ephemeral=True)

    @app_commands.command(name='sms注文確認',description='現在のSMS注文の状態を確認します')
    async def status(self,interaction:discord.Interaction):
        with db() as cx:
            row=cx.execute('SELECT * FROM orders WHERE buyer_id=? ORDER BY id DESC LIMIT 1',(interaction.user.id,)).fetchone()
        if not row: return await interaction.response.send_message('注文履歴がありません。',ephemeral=True)
        text=f'状態：`{row["status"]}`\n注文番号：`{row["id"]}`'
        if row['source'] == 'test': text += '\n種別：`テスト注文（PayPay決済なし）`'
        if row['phone']:text+=f'\n電話番号：`{public_phone(row["phone"])}`'
        await interaction.response.send_message(embed=notify('注文状態',text),ephemeral=True)


    @app_commands.command(name='sms管理',description='保留や未発行の注文を確認します')
    async def administration(self, interaction: discord.Interaction):
        if interaction.user.id != self.bot.config.owner_id:
            return await interaction.response.send_message('権限がありません。',ephemeral=True)
        with db() as cx:
            rows = cx.execute('SELECT id,amount,status,error,source FROM orders ORDER BY id DESC LIMIT 12').fetchall()
        if not rows:
            return await interaction.response.send_message('注文はありません。',ephemeral=True)
        desc='\n'.join(f'注文 `#{r["id"]}` ／ ' +
                        ('テスト' if r['source']=='test' else f'{r["amount"]}円') +
                        f' ／ `{r["status"]}`' +
                        (f' ／ {str(r["error"])[:45]}' if r['error'] else '') for r in rows)
        await interaction.response.send_message(embed=notify('SMS注文管理',desc),ephemeral=True)

    @app_commands.command(name='sms入金確定', description='管理者が保留注文の入金を確認して発行を進めます')
    @app_commands.describe(order_id='対象の注文番号')
    async def approve_payment(self, interaction: discord.Interaction, order_id: int):
        if interaction.user.id != self.bot.config.owner_id:
            return await interaction.response.send_message('権限がありません。', ephemeral=True)
        order = get_order(order_id)
        if not order or order['status'] != 'needs_review' or not (
            str(order.get('error') or '').startswith(('保留解除', '承認確認', '承認期限'))
        ):
            return await interaction.response.send_message('この注文は入金確定の対象外です。', ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not claim_status(order_id, 'needs_review', 'paid_unfulfilled'):
            return await interaction.followup.send('注文状態が変わりました。', ephemeral=True)
        result = await self.issue_number(order_id)
        if result in ('active', 'active_dm_failed'):
            return await interaction.followup.send('番号を発行しました。', ephemeral=True)
        return await interaction.followup.send('入金は記録しましたが、番号発行の確認が必要です。', ephemeral=True)

    @app_commands.command(name='sms未発行再開',description='未発行の注文だけ再開します')
    async def resume(self,interaction:discord.Interaction,order_id:int):
        if interaction.user.id!=self.bot.config.owner_id:
            return await interaction.response.send_message('権限がありません。',ephemeral=True)
        order=get_order(order_id)
        if not order or order['status']!='paid_unfulfilled' or order['sms_order_id']:
            return await interaction.response.send_message('再開できない注文です。',ephemeral=True)
        await interaction.response.defer(ephemeral=True,thinking=True)
        await self.issue_number(order_id)
        await interaction.followup.send('処理しました。最新の注文状態は /sms管理 でご確認ください。',ephemeral=True)


async def make(bot):return Sales(bot)
