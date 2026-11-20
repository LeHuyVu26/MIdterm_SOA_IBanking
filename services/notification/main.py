import os, smtplib
from email.message import EmailMessage
from email.utils import formataddr
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

app = FastAPI(title="Notification Service")
KEY = os.environ["INTERNAL_KEY"]

def send(to, subject, body):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ.get("SMTP_USER", "")
    pw = os.environ.get("SMTP_PASS", "").replace(" ", "")
    if not user or not pw:
        raise RuntimeError("Chưa cấu hình SMTP_USER / SMTP_PASS (xem file .env)")
    m = EmailMessage()
    m["From"] = formataddr((os.environ.get("MAIL_FROM_NAME", "iBanking TDTU"), os.environ.get("MAIL_FROM") or user))
    m["To"], m["Subject"] = to, subject
    m.set_content(body)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as s:
            s.login(user, pw); s.send_message(m)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(); s.login(user, pw); s.send_message(m)

class OtpMail(BaseModel):
    email: str; name: str; otp: str; txn_id: str; mssv: str; amount: int; minutes: int = 5

class OkMail(BaseModel):
    email: str; name: str; txn_id: str; mssv: str; amount: int; balance: int

def guard(k):
    if k != KEY: raise HTTPException(403, {"code": "FORBIDDEN", "message": "Internal only"})

@app.post("/internal/notify/otp")
def otp(b: OtpMail, x_internal_key: str = Header(None)):
    guard(x_internal_key)
    try:
        send(b.email, "OTP xác nhận thanh toán học phí iBanking",
             f"Mã OTP: {b.otp}\nMã giao dịch: {b.txn_id}\n\nXin chào {b.name},\n"
             f"Mã có hiệu lực {b.minutes} phút, dùng một lần, cho khoản học phí MSSV {b.mssv} "
             f"({b.amount:,} VND). Không chia sẻ mã này cho bất kỳ ai.")
    except Exception as e:
        raise HTTPException(502, {"code": "MAIL_FAILED", "message": str(e)})
    return {"sent": True}

@app.post("/internal/notify/payment-success")
def ok(b: OkMail, x_internal_key: str = Header(None)):
    guard(x_internal_key)
    try:
        send(b.email, "Thanh toán học phí thành công",
             f"Xin chào {b.name},\n\nGiao dịch {b.txn_id} đã thành công.\nMSSV: {b.mssv}\n"
             f"Số tiền: {b.amount:,} VND\nSố dư còn lại: {b.balance:,} VND\n\nCảm ơn bạn đã sử dụng iBanking.")
    except Exception as e:
        raise HTTPException(502, {"code": "MAIL_FAILED", "message": str(e)})
    return {"sent": True}