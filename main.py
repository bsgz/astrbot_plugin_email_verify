import asyncio
import random
import smtplib
import string
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star


class QQEmailVerifyPlugin(Star):
    """QQ群邮箱验证码入群验证插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # --- SMTP 邮件配置 ---
        self.smtp_host: str = config.get("smtp_host", "smtp.qq.com")
        self.smtp_port: int = config.get("smtp_port", 465)
        self.smtp_user: str = config.get("smtp_user", "")
        self.smtp_password: str = config.get("smtp_password", "")
        self.sender_name: str = config.get("sender_name", "QQ群验证系统")

        # --- 群白名单 ---
        raw_groups = config.get("enabled_groups", [])
        self.enabled_groups: List[str] = [str(g) for g in raw_groups] if raw_groups else []

        # --- 时间参数 ---
        self.verification_timeout: int = config.get("verification_timeout", 300)
        self.kick_countdown_warning_time: int = config.get("kick_countdown_warning_time", 60)
        self.kick_delay: int = config.get("kick_delay", 5)

        # --- 重试与验证码 ---
        self.max_retries: int = config.get("max_retries", 3)
        self.code_length: int = config.get("code_length", 6)

        # --- 消息模板 ---
        self.new_member_prompt: str = config.get(
            "new_member_prompt",
            "{at_user} 欢迎加入本群！\n📬 验证码已发送到你的QQ邮箱（{email}），"
            "请查收邮件并在 {timeout} 分钟内直接发送验证码完成验证。",
        )
        self.wrong_answer_prompt: str = config.get(
            "wrong_answer_prompt",
            "{at_user} ❌ 验证码错误！请检查邮箱后重新输入。你还剩 {remaining} 次机会。",
        )
        self.countdown_warning_prompt: str = config.get(
            "countdown_warning_prompt",
            "{at_user} ⏰ 验证即将超时！请尽快查看QQ邮箱中的验证码并发送，否则将被移出群聊。",
        )
        self.failure_message: str = config.get(
            "failure_message",
            "{at_user} ⏱️ 验证超时，你将在 {countdown} 秒后被移出群聊。",
        )
        self.verify_success_message: str = config.get(
            "verify_success_message",
            "{at_user} ✅ 验证通过，欢迎加入！",
        )

        # --- 待验证状态 ---
        self.pending: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def terminate(self):
        """插件卸载/停用时清理所有任务"""
        async with self._lock:
            for info in self.pending.values():
                task = info.get("task")
                if task and not task.done():
                    task.cancel()
            self.pending.clear()
        logger.info("[邮箱验证] 插件已卸载，所有验证任务已清理。")

    # ==================================================================
    # 工具方法
    # ==================================================================

    @staticmethod
    def _make_key(uid: str, gid: int) -> str:
        return f"{uid}_{gid}"

    def _is_group_enabled(self, gid: int) -> bool:
        if not self.enabled_groups:
            return True
        return str(gid) in self.enabled_groups

    @staticmethod
    def _is_valid_qq_number(qq: str) -> bool:
        return qq.isdigit() and 5 <= len(qq) <= 12

    def _generate_code(self) -> str:
        return "".join(random.choices(string.digits, k=self.code_length))

    def _build_email(self, recipient_email: str, code: str) -> MIMEMultipart:
        """构建验证邮件"""
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{Header(self.sender_name, 'utf-8').encode()} <{self.smtp_user}>"
        msg["To"] = recipient_email
        msg["Subject"] = Header("【群验证】你的入群验证码", "utf-8").encode()

        text_content = f"你的入群验证码是：{code}，请在群内发送此验证码完成验证。"
        html_content = f"""
        <div style="font-family: Arial, sans-serif; max-width: 480px; margin: auto;
                     padding: 24px; border: 1px solid #e0e0e0; border-radius: 8px;">
            <h2 style="color: #4CAF50; text-align: center;">📬 入群验证码</h2>
            <p>你好！你正在加入一个 QQ 群，请使用以下验证码完成验证：</p>
            <div style="text-align: center; margin: 24px 0;">
                <span style="font-size: 32px; font-weight: bold; color: #333;
                              background: #f5f5f5; padding: 12px 32px;
                              border-radius: 6px; letter-spacing: 6px;">{code}</span>
            </div>
            <p style="color: #666; font-size: 14px;">
                请在群内直接发送此验证码即可。<br>
                验证码有效期为 {self.verification_timeout // 60} 分钟。
            </p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 16px 0;">
            <p style="color: #999; font-size: 12px; text-align: center;">
                此邮件由验证系统自动发送，请勿回复。
            </p>
        </div>
        """
        msg.attach(MIMEText(text_content, "plain", "utf-8"))
        msg.attach(MIMEText(html_content, "html", "utf-8"))
        return msg

    async def _send_email(self, recipient_email: str, code: str) -> bool:
        """异步发送验证邮件"""
        msg = self._build_email(recipient_email, code)

        def _do_send():
            try:
                if self.smtp_port == 465:
                    server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15)
                else:
                    server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15)
                    server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_user, recipient_email, msg.as_string())
                server.quit()
                return True
            except Exception as e:
                logger.error(f"[邮箱验证] SMTP 发送失败: {e}")
                return False

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _do_send)

    # ==================================================================
    # 跳过验证命令（群主/群管理员/机器人管理员可用）
    # ==================================================================

    @filter.command("skip", alias={"跳过验证"})
    async def skip_verification(self, event: AstrMessageEvent):
        """跳过指定用户的验证。用法: /skip @某人 或 /跳过验证 @某人"""
        sender_id = str(event.get_sender_id())
        gid = event.message_obj.raw_message.get("group_id") if event.message_obj and event.message_obj.raw_message else None

        if not gid:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        # --- 权限检查 ---
        is_bot_admin = event.is_admin()
        if not is_bot_admin:
            try:
                bot = event.bot
                sender_info = await bot.api.call_action(
                    "get_group_member_info", group_id=gid, user_id=int(sender_id)
                )
                role = sender_info.get("role", "member")
                if role not in ("owner", "admin"):
                    yield event.plain_result("❌ 仅群主、群管理员或机器人管理员可使用此命令。")
                    return
            except Exception as e:
                logger.error(f"[邮箱验证] 获取发送者 {sender_id} 群角色失败: {e}")
                yield event.plain_result("❌ 权限校验失败，请稍后重试。")
                return

        # --- 从消息中提取 @目标用户 ---
        target_uid = None
        raw = event.message_obj.raw_message
        msg_segments = raw.get("message", [])
        if isinstance(msg_segments, list):
            for seg in msg_segments:
                if seg.get("type") == "at":
                    at_qq = str(seg.get("data", {}).get("qq", ""))
                    if at_qq and at_qq != str(event.get_self_id()):
                        target_uid = at_qq
                        break

        if not target_uid:
            yield event.plain_result("❌ 请 @需要跳过验证的用户。\n用法: /skip @某人")
            return

        # --- 清理验证状态 ---
        key = self._make_key(target_uid, gid)
        async with self._lock:
            info = self.pending.pop(key, None)

        if not info:
            yield event.plain_result(f"ℹ️ 用户 {target_uid} 当前没有待验证任务。")
            return

        task = info.get("task")
        if task and not task.done():
            task.cancel()

        logger.info(f"[邮箱验证] 用户 {target_uid} 在群 {gid} 的验证已被管理员跳过。")

        success_msg = f"[CQ:at,qq={target_uid}] ✅ 验证已被管理员跳过，欢迎加入！"
        bot = info.get("bot", event.bot)
        await bot.api.call_action("send_group_msg", group_id=gid, message=success_msg)
        event.stop_event()

    # ==================================================================
    # 事件入口
    # ==================================================================

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_event(self, event: AstrMessageEvent):
        """监听所有事件，分发到入群/退群/验证消息处理"""
        if not event.message_obj or not event.message_obj.raw_message:
            return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            return

        post_type = raw.get("post_type")
        gid = raw.get("group_id")

        if post_type == "notice":
            notice_type = raw.get("notice_type")
            if notice_type == "group_increase":
                if gid and not self._is_group_enabled(gid):
                    return
                if str(raw.get("user_id")) == str(event.get_self_id()):
                    return
                await self._on_member_join(event)
                event.stop_event()
            elif notice_type == "group_decrease":
                await self._on_member_leave(event)

        elif post_type == "message" and raw.get("message_type") == "group":
            if gid and not self._is_group_enabled(gid):
                return
            await self._on_group_message(event)

    # ==================================================================
    # 入群处理
    # ==================================================================

    async def _on_member_join(self, event: AstrMessageEvent):
        raw = event.message_obj.raw_message
        uid = str(raw.get("user_id"))
        gid = raw.get("group_id")
        if not self._is_valid_qq_number(uid):
            logger.warning(f"[邮箱验证] 用户 {uid} 的 QQ 号不合法，跳过验证。")
            return
        await self._start_verification(event, uid, gid)

    # ==================================================================
    # 退群处理
    # ==================================================================

    async def _on_member_leave(self, event: AstrMessageEvent):
        raw = event.message_obj.raw_message
        uid = str(raw.get("user_id"))
        gid = raw.get("group_id")
        key = self._make_key(uid, gid)
        async with self._lock:
            info = self.pending.pop(key, None)
        if info:
            task = info.get("task")
            if task and not task.done():
                task.cancel()
            logger.info(f"[邮箱验证] 用户 {uid} 退出群 {gid}，已清理验证状态。")

    # ==================================================================
    # 群消息处理（直接发验证码，无需@机器人）
    # ==================================================================

    async def _on_group_message(self, event: AstrMessageEvent):
        """处理群消息，判断是否为验证码回复（无需@机器人）"""
        uid = str(event.get_sender_id())
        raw = event.message_obj.raw_message
        gid = raw.get("group_id")
        key = self._make_key(uid, gid)

        async with self._lock:
            info = self.pending.get(key)
        if not info:
            return

        user_input = event.message_str.strip()
        if not user_input:
            return

        correct_code = info.get("code")

        if user_input == correct_code:
            # ✅ 验证成功
            async with self._lock:
                removed = self.pending.pop(key, None)
            if removed:
                task = removed.get("task")
                if task and not task.done():
                    task.cancel()

            logger.info(f"[邮箱验证] 用户 {uid} 在群 {gid} 验证成功。")
            bot = info.get("bot", event.bot)
            nickname = info.get("nickname", uid)
            at_user = f"[CQ:at,qq={uid}]"
            success_msg = self.verify_success_message.format(
                at_user=at_user, member_name=nickname
            )
            await bot.api.call_action("send_group_msg", group_id=gid, message=success_msg)
            event.stop_event()

        else:
            # ❌ 验证失败
            async with self._lock:
                info["retries"] = info.get("retries", 0) + 1
                retries = info["retries"]

            bot = info.get("bot", event.bot)
            nickname = info.get("nickname", uid)
            at_user = f"[CQ:at,qq={uid}]"

            if self.max_retries > 0 and retries >= self.max_retries:
                async with self._lock:
                    removed = self.pending.pop(key, None)
                if removed:
                    task = removed.get("task")
                    if task and not task.done():
                        task.cancel()

                logger.info(f"[邮箱验证] 用户 {uid} 在群 {gid} 答错 {retries} 次，执行踢出。")
                try:
                    await bot.api.call_action(
                        "send_group_msg", group_id=gid,
                        message=f"{at_user} ❌ 验证码输入错误次数过多，你将被移出群聊。",
                    )
                    await asyncio.sleep(self.kick_delay)
                    await bot.api.call_action(
                        "set_group_kick", group_id=gid,
                        user_id=int(uid), reject_add_request=False,
                    )
                except Exception as e:
                    logger.error(f"[邮箱验证] 重试上限踢人失败: {e}")
            else:
                logger.info(f"[邮箱验证] 用户 {uid} 在群 {gid} 验证码错误 (第 {retries} 次)。")
                new_code = self._generate_code()
                async with self._lock:
                    if key in self.pending:
                        self.pending[key]["code"] = new_code

                email_addr = f"{uid}@qq.com"
                email_ok = await self._send_email(email_addr, new_code)

                if email_ok:
                    remaining = (self.max_retries - retries) if self.max_retries > 0 else "无限"
                    wrong_msg = self.wrong_answer_prompt.format(
                        at_user=at_user, member_name=nickname, remaining=remaining
                    )
                    await bot.api.call_action("send_group_msg", group_id=gid, message=wrong_msg)
                else:
                    await bot.api.call_action(
                        "send_group_msg", group_id=gid,
                        message=f"{at_user} ❌ 新验证码邮件发送失败，请联系管理员。",
                    )

            event.stop_event()

    # ==================================================================
    # 验证流程核心
    # ==================================================================

    async def _start_verification(self, event: AstrMessageEvent, uid: str, gid: int):
        """启动验证流程"""
        key = self._make_key(uid, gid)

        async with self._lock:
            old = self.pending.pop(key, None)
        if old:
            task = old.get("task")
            if task and not task.done():
                task.cancel()

        code = self._generate_code()
        email_addr = f"{uid}@qq.com"
        logger.info(f"[邮箱验证] 为用户 {uid} 在群 {gid} 生成验证码: {code}，目标邮箱: {email_addr}")

        bot = event.bot
        nickname = uid
        try:
            user_info = await bot.api.call_action(
                "get_group_member_info", group_id=gid, user_id=int(uid)
            )
            nickname = user_info.get("card", "") or user_info.get("nickname", uid)
        except Exception as e:
            logger.warning(f"[邮箱验证] 获取用户 {uid} 昵称失败: {e}")

        email_ok = await self._send_email(email_addr, code)

        if not email_ok:
            logger.error(f"[邮箱验证] 向 {email_addr} 发送邮件失败，跳过验证。")
            try:
                at_user = f"[CQ:at,qq={uid}]"
                await bot.api.call_action(
                    "send_group_msg", group_id=gid,
                    message=f"{at_user} 验证邮件发送失败，请联系群管理员。",
                )
            except Exception:
                pass
            return

        task = asyncio.create_task(self._timeout_kick(key, uid, gid, nickname, bot))

        async with self._lock:
            self.pending[key] = {
                "gid": gid,
                "uid": uid,
                "code": code,
                "retries": 0,
                "task": task,
                "bot": bot,
                "nickname": nickname,
            }

        at_user = f"[CQ:at,qq={uid}]"
        timeout_min = self.verification_timeout // 60
        prompt = self.new_member_prompt.format(
            at_user=at_user, member_name=nickname,
            email=email_addr, timeout=timeout_min,
        )
        await bot.api.call_action("send_group_msg", group_id=gid, message=prompt)

    # ==================================================================
    # 超时踢出协程
    # ==================================================================

    async def _timeout_kick(self, key: str, uid: str, gid: int, nickname: str, bot):
        """超时后警告并踢出"""
        try:
            at_user = f"[CQ:at,qq={uid}]"

            wait_before_warn = self.verification_timeout - self.kick_countdown_warning_time
            if wait_before_warn > 0:
                await asyncio.sleep(wait_before_warn)

            async with self._lock:
                if key not in self.pending:
                    return

            if self.kick_countdown_warning_time > 0:
                warn_msg = self.countdown_warning_prompt.format(
                    at_user=at_user, member_name=nickname
                )
                try:
                    await bot.api.call_action("send_group_msg", group_id=gid, message=warn_msg)
                except Exception as e:
                    logger.warning(f"[邮箱验证] 发送超时警告失败: {e}")

                await asyncio.sleep(self.kick_countdown_warning_time)

            async with self._lock:
                if key not in self.pending:
                    return

            failure_msg = self.failure_message.format(
                at_user=at_user, member_name=nickname, countdown=self.kick_delay
            )
            try:
                await bot.api.call_action("send_group_msg", group_id=gid, message=failure_msg)
            except Exception:
                pass

            await asyncio.sleep(self.kick_delay)

            async with self._lock:
                if key not in self.pending:
                    return

            try:
                await bot.api.call_action(
                    "set_group_kick", group_id=gid,
                    user_id=int(uid), reject_add_request=False,
                )
                logger.info(f"[邮箱验证] 用户 {uid} 验证超时，已从群 {gid} 踢出。")
            except Exception as e:
                logger.error(f"[邮箱验证] 踢人失败 (权限不足?): {e}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[邮箱验证] 超时踢出流程异常 (用户 {uid}, 群 {gid}): {e}")
        finally:
            async with self._lock:
                self.pending.pop(key, None)