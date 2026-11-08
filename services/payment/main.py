"""Payment Service: tài khoản, giao dịch, OTP, transaction + concurrency. DB riêng: paymentdb.

Luồng thanh toán (Saga có bù trừ):
  1. POST /payments            -> kiểm tra, tạo giao dịch PENDING_OTP, sinh OTP, gửi email
  2. POST /payments/{id}/verify-otp:
       a. khóa dòng giao dịch (FOR UPDATE), kiểm tra OTP (1 lần, chưa hết hạn, tối đa MAX_OTP_ATTEMPTS lần sai)
       b. Tuition.reserve  (UPDATE có điều kiện -> chỉ 1 người giành được học phí)
       c. DB transaction: SELECT balance FOR UPDATE -> trừ tiền -> giao dịch SUCCESS (atomic)
       d. Tuition.confirm (PAID) ; nếu b/c lỗi -> Tuition.release (bù trừ)
       e. gửi email xác nhận
Job nền (maintenance_loop):
  - expire_stale_otps : giao dịch PENDING_OTP mà OTP mới nhất đã hết hạn -> FAILED
  - reconcile_payments: giao dịch SUCCESS mà học phí chưa được confirm -> gọi lại confirm (đối soát)
"""
import os, time, uuid, secrets, logging, threading, jwt, requests, psycopg2
from contextlib import contextmanager
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

SECRET, KEY = os.environ["JWT_SECRET"], os.environ["INTERNAL_KEY"]
USER, TUITION, NOTIFY = os.environ["USER_URL"], os.environ["TUITION_URL"], os.environ["NOTIFY_URL"]
IH = {"X-Internal-Key": KEY}
MAX_OTP_ATTEMPTS = 5                                    # sai đủ số lần này -> giao dịch FAILED
SWEEP_INTERVAL = int(os.environ.get("SWEEP_INTERVAL", "30"))  # giây giữa 2 lần chạy job nền
log = logging.getLogger("uvicorn.error")
app = FastAPI(title="Payment Service")

def err(s, code, msg): return HTTPException(s, {"code": code, "message": msg})

@contextmanager
def tx():
    c = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=RealDictCursor)
    try:
        with c, c.cursor() as cur:
            yield cur
    finally:
        c.close()

DDL = """
CREATE TABLE IF NOT EXISTS accounts(user_id INT PRIMARY KEY, balance BIGINT NOT NULL CHECK(balance>=0));
CREATE TABLE IF NOT EXISTS transactions(
  id UUID PRIMARY KEY, user_id INT NOT NULL REFERENCES accounts(user_id), mssv TEXT NOT NULL, student_name TEXT,
  amount BIGINT NOT NULL CHECK(amount>0),
  status TEXT NOT NULL CHECK(status IN('PENDING_OTP','SUCCESS','FAILED')),
  reason TEXT, payer_name TEXT, payer_email TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS otps(
  id SERIAL PRIMARY KEY, txn_id UUID NOT NULL REFERENCES transactions(id),
  code TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, used BOOLEAN NOT NULL DEFAULT false);
-- migration (an toàn với DB cũ): đếm số lần nhập sai OTP theo GIAO DỊCH (gửi lại OTP không reset được),
-- và cờ đánh dấu học phí đã được gạch nợ (PAID) ở Tuition Service chưa
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS otp_attempts INT NOT NULL DEFAULT 0;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tuition_confirmed BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS idx_otps_txn ON otps(txn_id);
CREATE INDEX IF NOT EXISTS idx_txn_user ON transactions(user_id, created_at DESC);
"""
SEED_BALANCES = {1: 20_000_000, 2: 20_000_000, 3: 20_000_000, 4: 1_000_000}

@app.on_event("startup")
def init():
    for _ in range(30):
        try:
            with tx() as cur:
                cur.execute(DDL)
                cur.execute("SELECT count(*) n FROM accounts")
                if cur.fetchone()["n"] == 0: # type: ignore
                    for u, b in SEED_BALANCES.items():
                        cur.execute("INSERT INTO accounts VALUES(%s,%s)", (u, b))
            break
        except psycopg2.OperationalError:
            time.sleep(2)
    threading.Thread(target=maintenance_loop, daemon=True, name="maintenance").start()

# ----------------------------------------------------------------------------------------------
# Job nền: hết hạn OTP + đối soát Saga
# ----------------------------------------------------------------------------------------------
def expire_stale_otps():
    """PENDING_OTP mà OTP mới nhất chưa dùng & đã hết hạn -> FAILED.
    - Chỉ xét OTP mới nhất: giao dịch đang chạy Saga (OTP đã used=true) không bao giờ bị quét nhầm.
    - FOR UPDATE SKIP LOCKED: bỏ qua dòng đang bị verify/resend giữ khóa."""
    with tx() as cur:
        cur.execute("""
            WITH stale AS (
              SELECT t.id FROM transactions t
              WHERE t.status='PENDING_OTP' AND EXISTS (
                SELECT 1 FROM otps o
                WHERE o.txn_id=t.id AND o.used=false AND o.expires_at<now()
                  AND o.id=(SELECT max(id) FROM otps WHERE txn_id=t.id))
              FOR UPDATE OF t SKIP LOCKED)
            UPDATE transactions SET status='FAILED', reason='OTP hết hạn', completed_at=now()
            WHERE id IN (SELECT id FROM stale)""")
        if cur.rowcount:
            log.info("EXPIRE: đã hủy %s giao dịch hết hạn OTP", cur.rowcount)

def reconcile_payments():
    """Đối soát: giao dịch SUCCESS (tiền đã trừ) nhưng học phí chưa confirm -> gọi lại confirm.
    Giả định Tuition `confirm` idempotent theo txn_id (gọi lại với cùng txn_id đã PAID vẫn trả 200).
    Chỉ xét giao dịch SUCCESS đã > 60s để không tranh với vòng thử lại trong verify()."""
    with tx() as cur:
        cur.execute("""SELECT id, mssv FROM transactions
                       WHERE status='SUCCESS' AND NOT tuition_confirmed
                         AND completed_at < now() - interval '60 seconds'
                       ORDER BY completed_at LIMIT 20 FOR UPDATE SKIP LOCKED""")
        for t in cur.fetchall():
            try:
                r = tuition_call("confirm", t["mssv"], t["id"]) # type: ignore
            except requests.RequestException:
                log.warning("RECONCILE: Tuition không khả dụng, thử lại sau (txn %s)", t["id"]) # type: ignore
                continue
            if r.ok:
                cur.execute("UPDATE transactions SET tuition_confirmed=true WHERE id=%s", (t["id"],)) # type: ignore
                log.warning("RECONCILE: đã gạch nợ bù cho txn %s (mssv %s)", t["id"], t["mssv"]) # type: ignore
            else:
                log.error("RECONCILE: confirm trả %s cho txn %s (mssv %s) - cần xử lý thủ công",
                          r.status_code, t["id"], t["mssv"]) # type: ignore

def maintenance_loop():
    while True:
        for job in (expire_stale_otps, reconcile_payments):
            try:
                job()
            except Exception:
                log.exception("maintenance job %s lỗi", job.__name__)
        time.sleep(SWEEP_INTERVAL)

# ----------------------------------------------------------------------------------------------
def uid_of(a):
    try:
        return int(jwt.decode(a.split()[1], SECRET, algorithms=["HS256"])["sub"])
    except Exception:
        raise err(401, "UNAUTHORIZED", "Chưa đăng nhập hoặc token hết hạn")

def new_otp(cur, txn):
    """OTP 6 số, gắn với giao dịch, hiệu lực 5 phút, không trùng OTP còn hiệu lực của giao dịch khác."""
    cur.execute("SELECT pg_advisory_xact_lock(42)")  # tuần tự hóa việc sinh OTP để kiểm tra trùng chính xác
    cur.execute("UPDATE otps SET used=true WHERE txn_id=%s AND used=false", (txn,))  # vô hiệu OTP cũ
    while True:
        code = f"{secrets.randbelow(10**6):06d}"
        cur.execute("SELECT 1 FROM otps WHERE code=%s AND used=false AND expires_at>now()", (code,))
        if not cur.fetchone(): break
    cur.execute("INSERT INTO otps(txn_id,code,expires_at) VALUES(%s,%s,now()+interval '5 minutes') RETURNING expires_at",
                (txn, code))
    return code, cur.fetchone()["expires_at"]

def fail_txn(cur, txn, reason):
    """Đánh dấu FAILED trong CÙNG transaction đang mở (gọi trước khi thoát `with tx()`)."""
    cur.execute("UPDATE transactions SET status='FAILED', reason=%s, completed_at=now() WHERE id=%s", (reason, txn))

def notify(path, payload):
    try:
        return requests.post(NOTIFY + path, json=payload, headers=IH, timeout=15).ok
    except requests.RequestException:
        return False

def tuition_call(action, mssv, txn):
    return requests.post(f"{TUITION}/internal/tuitions/{mssv}/{action}", json={"txn_id": str(txn)}, headers=IH, timeout=10)

def safe_release(mssv, txn):
    """Bù trừ: trả học phí về UNPAID. Không bao giờ ném lỗi để các bước dọn dẹp phía sau (set_status FAILED)
    luôn được chạy; nếu release thất bại, học phí giữ LOCKED và chỉ được log lại để xử lý/đối soát."""
    try:
        r = tuition_call("release", mssv, txn)
        if not r.ok:
            log.error("RELEASE: Tuition trả %s cho txn %s (mssv %s)", r.status_code, txn, mssv)
    except requests.RequestException:
        log.error("RELEASE: không gọi được Tuition cho txn %s (mssv %s) - học phí có thể kẹt LOCKED", txn, mssv)

def set_status(txn, status, reason=None):
    with tx() as cur:
        cur.execute("UPDATE transactions SET status=%s, reason=%s, completed_at=now() WHERE id=%s", (status, reason, txn))

class PayReq(BaseModel):
    mssv: str = Field(min_length=1, max_length=20, pattern=r"^\w+$")

class OtpReq(BaseModel):
    otp: str = Field(pattern=r"^\d{6}$")

@app.get("/payments/account")
def account(authorization: str = Header(None)):
    uid = uid_of(authorization)
    with tx() as cur:
        cur.execute("SELECT balance FROM accounts WHERE user_id=%s", (uid,))
        r = cur.fetchone()
    if not r: raise err(404, "ACCOUNT_NOT_FOUND", "Không tìm thấy tài khoản")
    return {"balance": r["balance"]} # type: ignore

@app.get("/payments/history")
def history(authorization: str = Header(None)):
    uid = uid_of(authorization)
    with tx() as cur:
        cur.execute("""SELECT id,mssv,student_name,amount,status,reason,created_at,completed_at
                       FROM transactions WHERE user_id=%s ORDER BY created_at DESC""", (uid,))
        return cur.fetchall()

@app.post("/payments", status_code=201)
def create_payment(b: PayReq, authorization: str = Header(None)):
    uid = uid_of(authorization)
    r = requests.get(f"{TUITION}/tuitions/{b.mssv}", headers={"Authorization": authorization}, timeout=10)
    if r.status_code == 404: raise err(404, "TUITION_NOT_FOUND", "Không tìm thấy MSSV hoặc khoản học phí")
    if not r.ok: raise err(502, "TUITION_UNAVAILABLE", "Dịch vụ học phí tạm thời không khả dụng")
    t = r.json()
    if t["status"] != "UNPAID":
        raise err(409, "TUITION_NOT_PAYABLE", "Học phí đã được thanh toán hoặc đang được xử lý")
    u = requests.get(f"{USER}/internal/users/{uid}", headers=IH, timeout=10)
    if not u.ok: raise err(502, "USER_UNAVAILABLE", "Dịch vụ người dùng tạm thời không khả dụng")
    u, txn = u.json(), str(uuid.uuid4())
    with tx() as cur:
        cur.execute("SELECT balance FROM accounts WHERE user_id=%s", (uid,))
        a = cur.fetchone()
        if not a or a["balance"] < t["amount"]: # type: ignore
            raise err(422, "INSUFFICIENT_BALANCE", "Số dư khả dụng không đủ để thanh toán")
        cur.execute("""INSERT INTO transactions(id,user_id,mssv,student_name,amount,status,payer_name,payer_email)
                       VALUES(%s,%s,%s,%s,%s,'PENDING_OTP',%s,%s)""",
                    (txn, uid, b.mssv, t["student_name"], t["amount"], u["full_name"], u["email"]))
        code, exp = new_otp(cur, txn)
    if not notify("/internal/notify/otp", {"email": u["email"], "name": u["full_name"], "otp": code,
                                           "txn_id": txn, "mssv": b.mssv, "amount": t["amount"]}):
        set_status(txn, "FAILED", "Không gửi được email OTP")
        raise err(502, "OTP_EMAIL_FAILED", "Không gửi được email OTP, vui lòng thử lại")
    return {"transaction_id": txn, "expires_at": exp, "message": "OTP đã được gửi tới email của bạn"}

@app.post("/payments/{txn_id}/resend-otp")
def resend(txn_id: uuid.UUID, authorization: str = Header(None)):
    uid, txn = uid_of(authorization), str(txn_id)
    bad = None
    with tx() as cur:
        cur.execute("SELECT * FROM transactions WHERE id=%s AND user_id=%s FOR UPDATE", (txn, uid))
        t = cur.fetchone()
        if not t: raise err(404, "TXN_NOT_FOUND", "Không tìm thấy giao dịch")
        if t["status"] != "PENDING_OTP": raise err(409, "TXN_NOT_PENDING", "Giao dịch không còn chờ OTP") # type: ignore
        cur.execute("SELECT expires_at<=now() AS expired FROM otps WHERE txn_id=%s ORDER BY id DESC LIMIT 1", (txn,))
        o = cur.fetchone()
        if o and o["expired"]:  # type: ignore # OTP đã hết hạn -> giao dịch hết hạn, không cho gửi lại
            fail_txn(cur, txn, "OTP hết hạn")
            bad = err(400, "OTP_EXPIRED", "OTP đã hết hạn, giao dịch đã bị hủy. Vui lòng thực hiện lại")
        else:
            code, exp = new_otp(cur, txn)
    if bad: raise bad  # raise SAU khi transaction đã commit để việc đánh dấu FAILED không bị rollback
    if not notify("/internal/notify/otp", {"email": t["payer_email"], "name": t["payer_name"], "otp": code, # type: ignore
                                           "txn_id": txn, "mssv": t["mssv"], "amount": t["amount"]}): # type: ignore
        raise err(502, "OTP_EMAIL_FAILED", "Không gửi được email OTP")
    return {"transaction_id": txn, "expires_at": exp}

class Insufficient(Exception): pass

@app.post("/payments/{txn_id}/verify-otp")
def verify(txn_id: uuid.UUID, b: OtpReq, authorization: str = Header(None)):
    uid, txn = uid_of(authorization), str(txn_id)
    # --- (a) kiểm tra & tiêu thụ OTP trong 1 transaction, khóa dòng giao dịch ---
    # Lưu ý: HTTPException ném BÊN TRONG `with tx()` sẽ rollback mọi UPDATE (kể cả đếm lần sai),
    # nên các lỗi cần ghi DB được gán vào `bad` rồi raise sau khi đã commit.
    bad = None
    with tx() as cur:
        cur.execute("SELECT * FROM transactions WHERE id=%s AND user_id=%s FOR UPDATE", (txn, uid))
        t = cur.fetchone()
        if not t: raise err(404, "TXN_NOT_FOUND", "Không tìm thấy giao dịch")
        if t["status"] != "PENDING_OTP": raise err(409, "TXN_NOT_PENDING", "Giao dịch đã được xử lý") # type: ignore
        cur.execute("SELECT id,code,used,expires_at>now() AS valid FROM otps WHERE txn_id=%s ORDER BY id DESC LIMIT 1", (txn,))
        o = cur.fetchone()
        if not o or o["used"]: raise err(400, "OTP_USED", "OTP đã được sử dụng hoặc không còn hiệu lực") # type: ignore
        if not o["valid"]: # type: ignore
            fail_txn(cur, txn, "OTP hết hạn")
            bad = err(400, "OTP_EXPIRED", "OTP đã hết hạn, giao dịch đã bị hủy. Vui lòng thực hiện lại")
        elif not secrets.compare_digest(o["code"], b.otp): # type: ignore
            n = t["otp_attempts"] + 1  # type: ignore
            if n >= MAX_OTP_ATTEMPTS:
                fail_txn(cur, txn, f"Nhập sai OTP quá {MAX_OTP_ATTEMPTS} lần")
                cur.execute("UPDATE transactions SET otp_attempts=%s WHERE id=%s", (n, txn))
                bad = err(400, "OTP_LOCKED", f"Nhập sai OTP quá {MAX_OTP_ATTEMPTS} lần, giao dịch đã bị hủy")
            else:
                cur.execute("UPDATE transactions SET otp_attempts=%s WHERE id=%s", (n, txn))
                bad = err(400, "OTP_INVALID", f"OTP không đúng. Bạn còn {MAX_OTP_ATTEMPTS - n} lần thử")
        else:
            cur.execute("UPDATE otps SET used=true WHERE id=%s", (o["id"],)) # type: ignore
    if bad: raise bad
    # --- (b) giành khoản học phí (atomic ở Tuition Service) ---
    try:
        r = tuition_call("reserve", t["mssv"], txn) # type: ignore
    except requests.RequestException:
        set_status(txn, "FAILED", "Dịch vụ học phí không khả dụng")
        raise err(502, "TUITION_UNAVAILABLE", "Dịch vụ học phí tạm thời không khả dụng")
    if r.status_code == 409:
        set_status(txn, "FAILED", "Học phí đã được thanh toán bởi giao dịch khác")
        raise err(409, "TUITION_NOT_PAYABLE", "Học phí đã được thanh toán hoặc đang được xử lý")
    if not r.ok or r.json()["amount"] != t["amount"]: # type: ignore
        safe_release(t["mssv"], txn) # type: ignore
        set_status(txn, "FAILED", "Số tiền học phí đã thay đổi")
        raise err(409, "AMOUNT_CHANGED", "Số tiền học phí đã thay đổi, vui lòng thực hiện lại")
    # --- (c) trừ tiền + ghi nhận giao dịch trong 1 DB transaction, khóa dòng tài khoản ---
    try:
        with tx() as cur:
            cur.execute("SELECT balance FROM accounts WHERE user_id=%s FOR UPDATE", (uid,))
            bal = cur.fetchone()["balance"] # type: ignore
            if bal < t["amount"]: raise Insufficient() # type: ignore
            cur.execute("UPDATE accounts SET balance=balance-%s WHERE user_id=%s", (t["amount"], uid)) # type: ignore
            cur.execute("UPDATE transactions SET status='SUCCESS', completed_at=now() WHERE id=%s", (txn,))
            new_bal = bal - t["amount"] # type: ignore
    except Insufficient:
        safe_release(t["mssv"], txn) # type: ignore
        set_status(txn, "FAILED", "Số dư không đủ")
        raise err(422, "INSUFFICIENT_BALANCE", "Số dư khả dụng không đủ để thanh toán")
    except Exception:
        log.exception("Lỗi khi trừ tiền txn %s", txn)
        safe_release(t["mssv"], txn) # type: ignore
        set_status(txn, "FAILED", "Lỗi hệ thống khi trừ tiền")
        raise err(500, "PAYMENT_ERROR", "Lỗi hệ thống, giao dịch không được thực hiện")
    # --- (d) gạch nợ học phí (thử lại nếu lỗi). Tiền đã trừ nên giao dịch vẫn là SUCCESS với người dùng;
    #     nếu 3 lần đều lỗi, cờ tuition_confirmed giữ false và job reconcile_payments sẽ gạch nợ bù. ---
    confirmed = False
    for _ in range(3):
        try:
            if tuition_call("confirm", t["mssv"], txn).ok: # type: ignore
                confirmed = True
                break
        except requests.RequestException:
            pass
        time.sleep(1)
    if confirmed:
        try:
            with tx() as cur:
                cur.execute("UPDATE transactions SET tuition_confirmed=true WHERE id=%s", (txn,))
        except Exception:
            log.exception("Không ghi được tuition_confirmed cho txn %s (job đối soát sẽ xử lý)", txn)
    else:
        log.error("Confirm học phí thất bại sau 3 lần, chờ job đối soát: txn %s mssv %s", txn, t["mssv"]) # type: ignore
    # --- (e) email xác nhận ---
    if not notify("/internal/notify/payment-success", {"email": t["payer_email"], "name": t["payer_name"], "txn_id": txn, # type: ignore
                                                       "mssv": t["mssv"], "amount": t["amount"], "balance": new_bal}): # type: ignore
        # giao dịch đã thành công, không rollback; chỉ ghi log để vận hành xử lý/gửi lại
        log.warning("EMAIL: không gửi được email xác nhận cho txn %s (%s)", txn, t["payer_email"]) # type: ignore
    return {"transaction_id": txn, "status": "SUCCESS", "mssv": t["mssv"], "amount": t["amount"], "balance": new_bal} # type: ignore