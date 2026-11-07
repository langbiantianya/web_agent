"""超级管理员随机密码的邮件与短信通知。"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    """两个通知渠道的独立结果。"""

    email_status: str
    sms_status: str

    @property
    def delivered(self) -> bool:
        """至少一个渠道成功。"""
        return "sent" in (self.email_status, self.sms_status)


def _password_message(username: str, password: str) -> str:
    """构造不包含点击链接的密码通知正文。"""
    return (
        f"您的 web_agent 超级管理员账号 {username} 已生成新的登录密码：\n\n"
        f"{password}\n\n"
        "该密码不能自定义修改，请妥善保存。登录后可按策略重新生成。"
    )


def _send_smtp(to_email: str, username: str, password: str) -> None:
    """通过 SMTP 同步发送密码邮件。"""
    message = EmailMessage()
    message["Subject"] = "web_agent 超级管理员密码已更新"
    message["From"] = settings.smtp_from
    message["To"] = to_email
    message.set_content(_password_message(username, password))
    context = ssl.create_default_context()
    with smtplib.SMTP(
        settings.smtp_host,
        settings.smtp_port,
        timeout=settings.super_admin_password_notify_timeout_seconds,
    ) as client:
        client.ehlo()
        if settings.smtp_use_tls:
            client.starttls(context=context)
            client.ehlo()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


async def send_email(
    email: str | None, *, username: str, password: str
) -> str:
    """发送密码邮件，返回 sent/skipped/failed。"""
    if not settings.mail_enabled or not email:
        return "skipped"
    if settings.mail_provider != "smtp":
        logger.error("MAIL_PROVIDER 当前只支持 smtp")
        return "failed"
    if not settings.smtp_host or not settings.smtp_from:
        logger.error("邮件通知已启用但 SMTP_HOST/SMTP_FROM 未配置")
        return "failed"
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_send_smtp, email, username, password),
            timeout=settings.super_admin_password_notify_timeout_seconds + 2,
        )
        return "sent"
    except Exception as exc:  # noqa: BLE001 - provider errors must not leak password
        logger.error("发送超级管理员密码邮件失败：%s", type(exc).__name__)
        return "failed"


def _send_aliyun_sms(phone: str, password: str) -> None:
    """使用阿里云官方 SDK 发送短信。"""
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest

    client = AcsClient(
        settings.aliyun_sms_access_key_id,
        settings.aliyun_sms_access_key_secret,
        settings.aliyun_sms_region,
    )
    request = CommonRequest()
    request.set_accept_format("json")
    request.set_domain("dysmsapi.aliyuncs.com")
    request.set_method("POST")
    request.set_protocol_type("https")
    request.set_version("2017-05-25")
    request.set_action_name("SendSms")
    request.add_query_param("PhoneNumbers", phone)
    request.add_query_param("SignName", settings.aliyun_sms_sign_name)
    request.add_query_param("TemplateCode", settings.aliyun_sms_template_code)
    request.add_query_param(
        "TemplateParam",
        json.dumps({"password": password}, ensure_ascii=False),
    )
    client.do_action_with_exception(request)


def _send_tencent_sms(phone: str, password: str) -> None:
    """使用腾讯云官方 SDK 发送短信。"""
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.sms.v20210111 import models, sms_client

    cred = credential.Credential(
        settings.tencent_sms_secret_id,
        settings.tencent_sms_secret_key,
    )
    http_profile = HttpProfile(endpoint="sms.tencentcloudapi.com")
    client_profile = ClientProfile(httpProfile=http_profile)
    client = sms_client.SmsClient(cred, settings.tencent_sms_region, client_profile)
    request = models.SendSmsRequest()
    request.SmsSdkAppId = settings.tencent_sms_sdk_app_id
    request.SignName = settings.tencent_sms_sign_name
    request.TemplateId = settings.tencent_sms_template_id
    request.PhoneNumberSet = [phone]
    request.TemplateParamSet = [password]
    response = client.SendSms(request)
    payload = json.loads(response.to_json_string())
    statuses = payload.get("SendStatusSet") or []
    if not statuses or statuses[0].get("Code") != "Ok":
        raise RuntimeError("tencent_sms_delivery_failed")


async def send_sms(phone: str | None, *, password: str) -> str:
    """调用唯一允许的两家 SMS SDK，返回 sent/skipped/failed。"""
    if not settings.sms_enabled or not phone:
        return "skipped"
    if settings.sms_provider not in {"aliyun", "tencent"}:
        logger.error("SMS_PROVIDER 只允许 aliyun 或 tencent")
        return "failed"
    try:
        if settings.sms_provider == "aliyun":
            await asyncio.wait_for(
                asyncio.to_thread(_send_aliyun_sms, phone, password),
                timeout=settings.super_admin_password_notify_timeout_seconds + 2,
            )
        else:
            await asyncio.wait_for(
                asyncio.to_thread(_send_tencent_sms, phone, password),
                timeout=settings.super_admin_password_notify_timeout_seconds + 2,
            )
        return "sent"
    except Exception as exc:  # noqa: BLE001
        logger.error("发送超级管理员密码短信失败：%s", type(exc).__name__)
        return "failed"


async def notify_password(
    *,
    username: str,
    email: str | None,
    phone: str | None,
    password: str,
) -> DeliveryResult:
    """并行发送邮件和短信。"""
    email_status, sms_status = await asyncio.gather(
        send_email(email, username=username, password=password),
        send_sms(phone, password=password),
    )
    return DeliveryResult(email_status=email_status, sms_status=sms_status)
