import asyncio
import json
import os
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger

@register("group_link_monitor", "花忤", "进群关联监控插件，支持规则别名与群主/群管管理，未加关联群禁言/踢出，进群自动@提醒", "v1.4.0")
class GroupLinkMonitor(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.rules = self.config.get("rules", [])
        # 兼容处理历史没有 name 属性的规则
        changed = False
        for r in self.rules:
            if not r.get("name"):
                r["name"] = str(r.get("group1", "监控规则"))
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
            "📖 【群互联/群监控指令帮助 v1.4.0】\n"
            "----------------------------\n"
            "仅【群主 / 群管理员 / Bot管理员】可用：\n\n"
            "1. 添加群监控 <监控群> <目标群> [禁言/踢出] [缓冲秒] [规则名称]\n"
            "   - 例: 添加群监控 111 222 (默认群号为名，禁言+60秒)\n"
            "   - 例: 添加群监控 111 222 踢出 (默认踢出+10分钟)\n"
            "   - 例: 添加群监控 111 222 踢出 300 粉丝二群联动\n"
            "2. 群监控列表 : 查看所有规则名称及配置\n"
            "3. 删除群监控 <序号> : 删除对应规则\n"
            "4. 设置惩罚 <序号> <禁言/踢出> [缓冲秒] : 调整规则\n"
            "----------------------------\n"
            "提示：WebUI 中也已支持单独设置每条规则的备注名称！"
        )

    @filter.command("添加群监控")
    async def add_rule(self, event: AstrMessageEvent, group1: str, group2: str, punish: str = "禁言", delay: int = 0, name: str = ""):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        g1 = str(group1).strip()
        g2 = str(group2).strip()
        p = punish.strip()
        if p in ["踢出", "kick"]:
            action = "kick"
            wait_time = int(delay) if delay > 0 else 600
        else:
            action = "mute"
            wait_time = int(delay) if delay > 0 else 60

        rule_name = str(name).strip() if name else f"群{g1}"

        rule = {
            "__template_key": "group_rule",
            "name": rule_name,
            "group1": g1,
            "group2": g2,
            "punish": action,
            "delay": wait_time
        }
        self.rules.append(rule)
        self.save_rules()
        p_desc = "禁言" if action == "mute" else "踢出"
        yield event.plain_result(
            f"✅ 添加成功！序号: {len(self.rules)}\n"
            f"规则名称: {rule_name}\n"
            f"监控群: {rule['group1']}\n"
            f"目标群: {rule['group2']}\n"
            f"惩罚方式: {p_desc}\n"
            f"缓冲时限: {wait_time}秒"
        )

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
            p_text = "禁言" if r.get("punish") == "mute" else "踢出"
            d = int(r.get("delay", 60))
            d_str = f"{d//60}分钟" if d >= 60 and d % 60 == 0 else f"{d}秒"
            msg += f"\n[{idx}] 📌 {r_name} (监控群: {r.get('group1')} -> 目标群: {r.get('group2')}) | 惩罚: {p_text} | 缓冲: {d_str}"
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
            r_name = removed.get("name") or f"群{removed.get('group1')}"
            yield event.plain_result(f"🗑️ 已删除规则 [{index}]【{r_name}】: 监控群 {removed.get('group1')} -> 目标群 {removed.get('group2')}")
        else:
            yield event.plain_result(f"❌ 序号 [{index}] 不存在。")

    @filter.command("设置惩罚")
    async def set_punish(self, event: AstrMessageEvent, index: int, punish: str, delay: int = 0):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        idx = int(index) - 1
        if not (0 <= idx < len(self.rules)):
            yield event.plain_result(f"❌ 序号 [{index}] 不存在。")
            return
        p = punish.strip()
        if p in ["踢出", "kick"]:
            self.rules[idx]["punish"] = "kick"
            if delay > 0:
                self.rules[idx]["delay"] = int(delay)
            elif self.rules[idx].get("delay", 60) == 60:
                self.rules[idx]["delay"] = 600
        elif p in ["禁言", "mute"]:
            self.rules[idx]["punish"] = "mute"
            if delay > 0:
                self.rules[idx]["delay"] = int(delay)
        else:
            yield event.plain_result("❌ 惩罚只支持：禁言 或 踢出")
            return
        self.save_rules()
        p_text = "踢出" if self.rules[idx]["punish"] == "kick" else "禁言"
        yield event.plain_result(f"✅ 已将规则 [{index}]【{self.rules[idx].get('name')}】修改为: 惩罚={p_text}, 缓冲={self.rules[idx]['delay']}秒")

    @filter.on_decorating_result()
    async def monitor_join(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", {})
        if isinstance(raw, dict) and raw.get("notice_type") == "group_increase":
            g_id = str(raw.get("group_id"))
            u_id = str(raw.get("user_id"))
            bot = event.bot
            for r in self.rules:
                if str(r.get("group1")) == g_id:
                    target_g = str(r.get("group2"))
                    punish_type = r.get("punish", "mute")
                    delay_sec = int(r.get("delay", 60 if punish_type == "mute" else 600))
                    
                    asyncio.create_task(self._send_join_notice(bot, g_id, target_g, u_id, punish_type, delay_sec))
                    asyncio.create_task(self._check_and_punish(bot, g_id, target_g, u_id, punish_type, delay_sec))

    async def _send_join_notice(self, bot, g_id: str, target_g: str, u_id: str, punish: str, delay: int):
        try:
            d_str = f"{delay//60}分钟" if delay >= 60 and delay % 60 == 0 else f"{delay}秒"
            punish_text = "禁言" if punish == "mute" else "踢出群聊"
            msg = f" 欢迎入群！\n⚠️ 本群已开启主从群关联验证，请在【{d_str}】内加入目标群【{target_g}】，逾期未加将自动执行【{punish_text}】处罚！"
            
            try:
                await bot.api.call_action("send_group_msg", group_id=int(g_id), message=[
                    {"type": "at", "data": {"qq": str(u_id)}},
                    {"type": "text", "data": {"text": msg}}
                ])
            except Exception:
                await bot.api.call_action("send_group_msg", group_id=int(g_id), message=f"[CQ:at,qq={u_id}]{msg}")
        except Exception as e:
            logger.error(f"发送入群提示失败: {e}")

    async def _check_and_punish(self, bot, g1: str, g2: str, uid: str, punish: str, delay: int):
        await asyncio.sleep(delay)
        try:
            res = await bot.api.call_action("get_group_member_info", group_id=int(g2), user_id=int(uid), no_cache=True)
            if res and res.get("user_id"):
                return
        except Exception:
            pass

        try:
            if punish == "mute":
                await bot.api.call_action("set_group_ban", group_id=int(g1), user_id=int(uid), duration=86400)
                logger.info(f"用户 {uid} 未加入目标群 {g2}，已在群 {g1} 禁言。")
            else:
                await bot.api.call_action("set_group_kick", group_id=int(g1), user_id=int(uid), reject_add_request=False)
                logger.info(f"用户 {uid} 未加入目标群 {g2}，已在群 {g1} 踢出。")
        except Exception as e:
            logger.error(f"执行惩罚失败: {e}")
