import os
import requests

# ── Mailjet transactional email API ────────────────────────────────
# HTTPS API вместо SMTP — Railway блокирует исходящий SMTP (порты
# 25/465/587) на бесплатных тарифах, а HTTPS (443) не трогает.
# Ключи брать тут: Mailjet → Account Settings → REST API → API Key Management.
MAILJET_API_KEY = os.environ.get("MAILJET_API_KEY", "")
MAILJET_API_SECRET = os.environ.get("MAILJET_API_SECRET", "")
MAILJET_API_URL = "https://api.mailjet.com/v3.1/send"

SENDER_EMAIL = os.environ.get("GMAIL", "") or os.environ.get("MAILJET_SENDER_EMAIL", "")
FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Business Law Consulting")
LAWYER_EMAIL = os.environ.get("LAWYER_EMAIL", "")


def _send(to_email: str, subject: str, html_body: str = "", text_body: str = "") -> bool:
    """Общая отправка через Mailjet API. Возвращает True/False, без исключения наружу."""
    if not (MAILJET_API_KEY and MAILJET_API_SECRET and SENDER_EMAIL):
        print("[MAIL] Пропущена отправка — не заданы MAILJET_API_KEY/MAILJET_API_SECRET/GMAIL(sender)")
        return False

    message = {
        "From": {"Email": SENDER_EMAIL, "Name": FROM_NAME},
        "To": [{"Email": to_email}],
        "Subject": subject,
    }
    if html_body:
        message["HTMLPart"] = html_body
    if text_body:
        message["TextPart"] = text_body

    payload = {"Messages": [message]}

    try:
        resp = requests.post(
            MAILJET_API_URL,
            json=payload,
            auth=(MAILJET_API_KEY, MAILJET_API_SECRET),
            timeout=20,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            status = data.get("Messages", [{}])[0].get("Status", "")
            if status == "success":
                return True
            print(f"[MAIL ERROR] {to_email}: unexpected status — {data}")
            return False
        print(f"[MAIL ERROR] {to_email}: HTTP {resp.status_code} — {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"[MAIL ERROR] {to_email}: {e}")
        return False


def send_html_email(to_email: str, subject: str, html_body: str, text_body: str = "") -> bool:
    """Отправка одного HTML-письма (для массовой рассылки)."""
    return _send(to_email, subject, html_body=html_body, text_body=text_body)


def send_lead_email(client_name: str, phone: str, message_text: str, username: str) -> bool:
    if not LAWYER_EMAIL:
        print("[MAIL] Пропущена отправка — не задан LAWYER_EMAIL")
        return False
    body = (
        f"Новая заявка от клиента с анкетой\n\n"
        f"Имя: {client_name or '—'}\n"
        f"Телефон: {phone or '—'}\n"
        f"Telegram: @{username or '—'}\n\n"
        f"Сообщение:\n{message_text}"
    )
    return _send(
        LAWYER_EMAIL,
        subject=f"Новая заявка — {client_name or phone or username}",
        text_body=body,
    )
