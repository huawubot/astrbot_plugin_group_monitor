import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star, register

VERSION = "v1.4.1"


class GroupIncreaseFilter(CustomFilter):
    """匹配 OneBot V11 的 group_increase（新人进群）通知事件。

    背景（issue #1）：v1.4.0 把入群 handler 注册在 `on_decorating_result`
    上，但那是「发送消息前」钩子——只在事件有非空结果待发送时才会派发。
    入群通知是空消息链事件，不产生任何发送结果，ResultDecorateStage 会
    提前 return，钩子永远不会被调用，核心功能因此失效。

    正确接法：注册一个能匹配「收到的事件」的 filter。filter 通过本身即
    满足 AstrBot 管道的唤醒条件（WakingCheckStage 条件 4），事件会进入
    ProcessStage 并调用本 handler。
    """

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        raw = getattr(event.message_obj, "raw_message", None)
        return (
            isinstance(raw, dict)
            and raw.get("notice_type") == "group_increase"
            and bool(raw.get("group_id"))
        )


@register(
    "group_link_monitor",
    "花忤",
    "进群关联监控插件，支持规则别名与群主/群管管理，未加关联群禁言/踢出，进群自动@提醒",
    VERSION,
)
class GroupLinkMonitor(Star):
    MUTE_DEFAULT = 60            # 禁言惩罚的默认缓冲（秒）
    KICK_DEFAULT = 600           # 踢出惩罚的默认缓冲（秒）
    MUTE_DURATION_DEFAULT = 86400  # 默认禁言时长（秒）= 24 小时

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.rules = self.config.get("rules", [])
        self._tasks: set = set()
        # 兼容处理历史规则：缺 name 补群号名；旧规则补 mute_duration
        changed = False
        for r in self.rules:
            if not r.get("name"):
                r["name"] = str(r.get("group1", "监控规则"))
                changed = True
            if "mute_duration" not in r:
                r["mute_duration"] = self.MUTE_DURATION_DEFAULT
                changed = True
        if changed:
            self.save_rules()

    def save_rules(self):
        try:
            # 补齐空名字
            for r in self.rules:
                if not r.get("name"):
                    r["name"] = str(r.get("group1", "监控群"))
            self.config["rules"] = self.rules
            if hasattr(self.context, "save_config"):
                self.context.save_config()
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    # ---------------- 平台 API 访问 ----------------

    def _get_cqbot(self, event: AstrMessageEvent):
        """获取 aiocqhttp 的 CQHttp 实例（提供 call_action）。

        OneBot V11（aiocqhttp）平台的事件自带 .bot；其余平台返回 None，
        由调用方决定是否降级提示。
        """
        bot = getattr(event, "bot", None)
        if bot is not None and hasattr(bot, "call_action"):
            return bot
        try:
            platform = self.context.get_platform_inst(event.get_platform_id())
        except Exception:
            platform = None
        if platform is not None:
            bot = getattr(platform, "bot", None)
            if bot is not None and hasattr(bot, "call_action"):
                return bot
        return None

    def _spawn(self, coro):
        """create_task 并持有引用，防止任务被 GC 回收。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------- 权限 ----------------

    def _is_authorized(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否具有权限：Bot 管理员、群主 或 群管理员"""
        admin_list = getattr(self.context, "admins", []) or []
        user_id = str(event.get_sender_id())
        if user_id in [str(a) for a in admin_list]:
            return True

        raw = getattr(event.message_obj, "raw_message", {})
        if isinstance(raw, dict):
            sender = raw.get("sender", {})
            role = sender.get("role", "")
            if role in ["owner", "admin"]:
                return True
        return False

    # ---------------- 指令 ----------------

    @filter.command("群监控帮助")
    async def help_cmd1(self, event: AstrMessageEvent):
        """获取群监控帮助文档"""
        yield event.plain_result(self._get_help_text())

    @filter.command("群互联帮助")
    async def help_cmd2(self, event: AstrMessageEvent):
        """获取群互联帮助文档"""
        yield event.plain_result(self._get_help_text())

    def _get_help_text(self) -> str:
        return (
            f"📖 【群互联/群监控指令帮助 {VERSION}】\n"
            "----------------------------\n"
            "仅【群主 / 群管理员 / Bot管理员】可用：\n\n"
            "1. 添加群监控 <监控群> <目标群> [禁言/踢出] [缓冲秒] [规则名称] [禁言时长秒]\n"
            "   - 例: 添加群监控 111 222 (默认群号为名，禁言+60秒缓冲)\n"
            "   - 例: 添加群监控 111 222 踢出 (默认踢出+600秒缓冲)\n"
            "   - 例: 添加群监控 111 222 踢出 300 粉丝二群联动\n"
            "   - 规则名称不要带空格；禁言时长仅在惩罚=禁言时生效，默认 86400 秒\n"
            "2. 群监控列表 : 查看所有规则名称及配置\n"
            "3. 删除群监控 <序号> : 删除对应规则\n"
            "4. 设置惩罚 <序号> <禁言/踢出> [缓冲秒] [禁言时长秒] : 调整规则\n"
            "   - 不带缓冲秒时按惩罚类型取默认：禁言 60 秒 / 踢出 600 秒\n"
            "----------------------------\n"
            "部署前提（不满足则功能不生效）：\n"
            "- 机器人必须同时在监控群和目标群中\n"
            "- 机器人必须在监控群具有管理员权限\n"
            "- 仅支持 OneBot V11 平台（NapCat / Lagrange 等）\n"
            "提示：WebUI 中也已支持单独设置每条规则的备注名称！"
        )

    @filter.command("添加群监控")
    async def add_rule(
        self,
        event: AstrMessageEvent,
        group1: str,
        group2: str,
        punish: str = "禁言",
        delay: int = 0,
        name: str = "",
        mute_duration: int = 0,
    ):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        g1 = str(group1).strip()
        g2 = str(group2).strip()
        if not g1.isdigit() or not g2.isdigit():
            yield event.plain_result("❌ 群号必须是纯数字。")
            return
        if g1 == g2:
            yield event.plain_result("❌ 监控群与目标群不能是同一个群。")
            return

        # 去重：同一监控群只允许一条规则（issue #3）
        for idx, r in enumerate(self.rules, 1):
            if str(r.get("group1")) == g1:
                yield event.plain_result(
                    f"❌ 监控群 {g1} 已存在规则 [{idx}]【{r.get('name') or g1}】。\n"
                    f"请用【设置惩罚 {idx} …】调整，或先【删除群监控 {idx}】再添加。"
                )
                return

        p = punish.strip()
        is_kick = p in ["踢出", "kick"]
        action = "kick" if is_kick else "mute"
        wait_time = int(delay) if delay > 0 else (self.KICK_DEFAULT if is_kick else self.MUTE_DEFAULT)
        mute_duration_val = int(mute_duration) if mute_duration > 0 else self.MUTE_DURATION_DEFAULT
        rule_name = str(name).strip() if name else f"群{g1}"

        bot = self._get_cqbot(event)
        warnings = await self._precheck(bot, event.get_self_id(), g1, g2)

        rule = {
            "__template_key": "group_rule",
            "name": rule_name,
            "group1": g1,
            "group2": g2,
            "punish": action,
            "delay": wait_time,
            "mute_duration": mute_duration_val,
        }
        self.rules.append(rule)
        self.save_rules()
        p_desc = "踢出" if is_kick else f"禁言（时长 {mute_duration_val} 秒）"
        lines = [
            f"✅ 添加成功！序号: {len(self.rules)}",
            f"规则名称: {rule_name}",
            f"监控群: {g1} → 目标群: {g2}",
            f"惩罚方式: {p_desc}",
            f"缓冲时限: {wait_time}秒",
        ]
        if is_kick and wait_time < 300:
            lines.append(f"⚠️ 踢出缓冲仅 {wait_time} 秒偏短，新人可能来不及加群，建议 600 秒。")
        lines.extend(warnings)
        yield event.plain_result("\n".join(lines))

    async def _precheck(self, bot, self_id: str, g1: str, g2: str) -> list:
        """添加规则前的部署自检（issue #2 / #5），返回警告列表。"""
        if bot is None:
            return ["⚠️ 当前平台未提供 OneBot V11 API，无法自检，也无法发送提醒/执行惩罚。"]
        warnings = []
        try:
            info1 = await bot.call_action(
                "get_group_member_info", group_id=int(g1), user_id=int(self_id), no_cache=True
            )
            role1 = (info1 or {}).get("role", "")
            if role1 not in ("owner", "admin"):
                warnings.append(f"⚠️ 机器人在监控群 {g1} 的角色是 {role1 or '未知'}（非管理员），禁言/踢出会失败。")
        except Exception:
            warnings.append(f"⚠️ 机器人不在监控群 {g1}，惩罚无法生效。")
        try:
            await bot.call_action(
                "get_group_member_info", group_id=int(g2), user_id=int(self_id), no_cache=True
            )
        except Exception:
            warnings.append(f"⚠️ 机器人不在目标群 {g2}，无法校验新人是否已加群（校验失败时不会误罚，但规则形同虚设）。")
        return warnings

    @filter.command("群监控列表")
    async def list_rules(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        if not self.rules:
            yield event.plain_result("暂无任何群监控规则，可发送【群监控帮助】查看配置方法。")
            return
        msg = "📋 【群监控规则列表】\n"
        for idx, r in enumerate(self.rules, 1):
            r_name = r.get("name") or f"群{r.get('group1')}"
            is_kick = r.get("punish") == "kick"
            p_text = "踢出" if is_kick else f"禁言({int(r.get('mute_duration', self.MUTE_DURATION_DEFAULT)) // 3600}h)"
            d = int(r.get("delay", self.MUTE_DEFAULT))
            d_str = f"{d//60}分钟" if d >= 60 and d % 60 == 0 else f"{d}秒"
            msg += (
                f"\n[{idx}] 📌 {r_name} (监控群: {r.get('group1')} -> 目标群: {r.get('group2')})"
                f" | 惩罚: {p_text} | 缓冲: {d_str}"
            )
        yield event.plain_result(msg)

    @filter.command("删除群监控")
    async def del_rule(self, event: AstrMessageEvent, index: int):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        idx = int(index) - 1
        if 0 <= idx < len(self.rules):
            removed = self.rules.pop(idx)
            self.save_rules()
            r_name = removed.get("name") or str(removed.get("group1"))
            yield event.plain_result(
                f"🗑️ 已删除规则 [{index}]【{r_name}】: 监控群 {removed.get('group1')} -> 目标群 {removed.get('group2')}"
            )
        else:
            yield event.plain_result(f"❌ 序号 [{index}] 不存在。")

    @filter.command("设置惩罚")
    async def set_punish(self, event: AstrMessageEvent, index: int, punish: str, delay: int = 0, mute_duration: int = 0):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        idx = int(index) - 1
        if not (0 <= idx < len(self.rules)):
            yield event.plain_result(f"❌ 序号 [{index}] 不存在。")
            return
        p = punish.strip()
        rule = self.rules[idx]
        if p in ["踢出", "kick"]:
            rule["punish"] = "kick"
            rule["delay"] = int(delay) if delay > 0 else self.KICK_DEFAULT
        elif p in ["禁言", "mute"]:
            rule["punish"] = "mute"
            rule["delay"] = int(delay) if delay > 0 else self.MUTE_DEFAULT
        else:
            yield event.plain_result("❌ 惩罚只支持：禁言 或 踢出")
            return
        if mute_duration > 0:
            rule["mute_duration"] = int(mute_duration)
        self.save_rules()
        p_text = "踢出" if rule["punish"] == "kick" else f"禁言(时长 {int(rule.get('mute_duration', self.MUTE_DURATION_DEFAULT))} 秒)"
        yield event.plain_result(
            f"✅ 已将规则 [{index}]【{rule.get('name')}】修改为: 惩罚={p_text}, 缓冲={rule['delay']}秒"
        )

    # ---------------- 入群监控 ----------------

    @filter.custom_filter(GroupIncreaseFilter)
    async def monitor_join(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", {})
        # filter 已保证，这里双保险
        if not isinstance(raw, dict) or raw.get("notice_type") != "group_increase":
            return

        g_id = str(raw.get("group_id"))
        u_id = str(raw.get("user_id"))
        bot = self._get_cqbot(event)
        if bot is None:
            yield event.plain_result(
                "⚠️ 群关联监控：当前平台未提供 OneBot V11 API（call_action），"
                "无法发送提醒/执行惩罚。请使用 NapCat / Lagrange 等 OneBot V11 平台。"
            )
            return

        # 同一监控群多条重复规则（历史遗留）只按首条处理（issue #3）
        rule = None
        dup = 0
        for r in self.rules:
            if str(r.get("group1")) == g_id:
                if rule is None:
                    rule = r
                else:
                    dup += 1
        if dup:
            logger.warning(f"监控群 {g_id} 存在 {dup + 1} 条重复规则，仅按首条处理，建议用【删除群监控】清理。")
        if rule is None:
            return

        target_g = str(rule.get("group2"))
        punish_type = rule.get("punish", "mute")
        delay_sec = int(rule.get("delay") or (self.KICK_DEFAULT if punish_type == "kick" else self.MUTE_DEFAULT))
        mute_duration = int(rule.get("mute_duration") or self.MUTE_DURATION_DEFAULT)

        self._spawn(self._send_join_notice(bot, g_id, target_g, u_id, punish_type, delay_sec))
        self._spawn(self._check_and_punish(bot, g_id, target_g, u_id, punish_type, delay_sec, mute_duration))

    async def _send_join_notice(self, bot, g_id: str, target_g: str, u_id: str, punish: str, delay: int):
        try:
            d_str = f"{delay//60}分钟" if delay >= 60 and delay % 60 == 0 else f"{delay}秒"
            punish_text = "禁言" if punish == "mute" else "踢出群聊"
            text = (
                f"欢迎入群！\n⚠️ 本群已开启主从群关联验证，请在【{d_str}】内加入目标群【{target_g}】，"
                f"逾期未加将自动执行【{punish_text}】处罚！"
            )
            await bot.call_action("send_group_msg", group_id=int(g_id), message=[
                {"type": "at", "data": {"qq": str(u_id)}},
                {"type": "text", "data": {"text": text}},
            ])
        except Exception as e:
            logger.error(f"发送入群提示失败: {e}")

    async def _check_in_target(self, bot, g2: str, uid: str, retries: int = 3):
        """带重试地查询 uid 是否已在目标群 g2。

        返回 True=在群；False=确认不在；None=多次查询失败（无法确认）。
        （issue #2：查询失败不能当作"不在群"处理）
        """
        for attempt in range(retries):
            try:
                res = await bot.call_action(
                    "get_group_member_info", group_id=int(g2), user_id=int(uid), no_cache=True
                )
                if res and res.get("user_id"):
                    return True
                return False
            except Exception as e:
                if attempt < retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
                else:
                    logger.error(f"查询目标群成员失败（已重试 {retries} 次）: {e}")
        return None

    async def _check_and_punish(self, bot, g1: str, g2: str, uid: str, punish: str, delay: int, mute_duration: int):
        await asyncio.sleep(delay)
        present = await self._check_in_target(bot, g2, uid)
        if present is True:
            return
        if present is None:
            # 查询失败 ≠ 不在群：不执行惩罚，提示管理员人工处理（issue #2）
            try:
                await bot.call_action("send_group_msg", group_id=int(g1), message=[
                    {"type": "text", "data": {"text": (
                        f"⚠️ 成员 {uid} 的目标群【{g2}】加入校验失败（查询接口多次异常），"
                        f"未自动执行处罚，请管理员人工确认。"
                    )}},
                ])
            except Exception as e:
                logger.error(f"发送校验失败提示失败: {e}")
            return
        try:
            if punish == "mute":
                await bot.call_action(
                    "set_group_ban", group_id=int(g1), user_id=int(uid), duration=mute_duration
                )
                logger.info(f"用户 {uid} 未加入目标群 {g2}，已在群 {g1} 禁言 {mute_duration} 秒。")
            else:
                await bot.call_action("set_group_kick", group_id=int(g1), user_id=int(uid), reject_add_request=False)
                logger.info(f"用户 {uid} 未加入目标群 {g2}，已在群 {g1} 踢出。")
        except Exception as e:
            logger.error(f"执行惩罚失败: {e}")
