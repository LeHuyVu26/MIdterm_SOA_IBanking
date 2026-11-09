import os, time, jwt, psycopg2, logging, threading
from contextlib import contextmanager
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

SECRET, KEY = os.environ["JWT_SECRET"], os.environ["INTERNAL_KEY"]
app = FastAPI(title="Tuition Service")
LOCK_TTL = int(os.environ.get("LOCK_TTL_SECONDS", "600"))

def err(s, code, msg): return HTTPException(s, {"code": code, "message": msg})

@contextmanager
def tx():
    c = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=RealDictCursor)
    try:
        with c, c.cursor() as cur:
            yield cur
    finally:
        c.close()

SEED = [("52100001", "Nguyễn Văn An", 15_000_000), 
        ("52100002", "Trần Thị Bình", 15_000_000),
        ("52100003", "Lê Minh Chi", 12_000_000), 
        ("52100004", "Phạm Quốc Dũng", 18_500_000),
        ("52100005", "Phan Đức Tài", 9_000_000)]

def release_stale_locks():
    with tx() as cur:
        cur.execute("""UPDATE tuitions SET status='UNPAID', locked_by=NULL, locked_at=NULL
                       WHERE status='LOCKED' AND locked_at < now() - make_interval(secs => %s)""", (LOCK_TTL,))
        if cur.rowcount:
            logging.warning("RELEASE-STALE: giải phóng %s lock quá hạn", cur.rowcount)

def maintenance_loop():
    while True:
        try:
            release_stale_locks()
        except Exception:
            logging.exception("release_stale_locks lỗi")
        time.sleep(60)

@app.on_event("startup")
def init():
    for _ in range(30):
        try:
            with tx() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS tuitions(
                  mssv TEXT PRIMARY KEY, student_name TEXT NOT NULL,
                  amount BIGINT NOT NULL CHECK(amount>0),
                  status TEXT NOT NULL DEFAULT 'UNPAID' CHECK(status IN('UNPAID','LOCKED','PAID')),
                  locked_by TEXT, paid_by_txn TEXT,
                  locked_at TIMESTAMPTZ)""")
                cur.execute("ALTER TABLE tuitions ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ")
                cur.execute("SELECT count(*) n FROM tuitions")
                if cur.fetchone()["n"] == 0: # type: ignore
                    for s in SEED:
                        cur.execute("INSERT INTO tuitions(mssv,student_name,amount) VALUES(%s,%s,%s)", s)
            break
        except psycopg2.OperationalError:
            time.sleep(2)
    threading.Thread(target=maintenance_loop, daemon=True).start()

@app.get("/tuitions/{mssv}")
def get_tuition(mssv: str, authorization: str = Header(None)):
    try:
        jwt.decode(authorization.split()[-1], SECRET, algorithms=["HS256"])
    except Exception:
        raise err(401, "UNAUTHORIZED", "Chưa đăng nhập hoặc token hết hạn")
    with tx() as cur:
        cur.execute("SELECT mssv,student_name,amount,status FROM tuitions WHERE mssv=%s", (mssv,))
        r = cur.fetchone()
    if not r: raise err(404, "TUITION_NOT_FOUND", "Không tìm thấy MSSV hoặc khoản học phí")
    return r

class Txn(BaseModel):
    txn_id: str

def internal(k):
    if k != KEY: raise err(403, "FORBIDDEN", "Internal only")

@app.post("/internal/tuitions/{mssv}/reserve")
def reserve(mssv: str, b: Txn, x_internal_key: str = Header(None)):
    internal(x_internal_key)
    with tx() as cur:
        cur.execute("""UPDATE tuitions SET status='LOCKED', locked_by=%s, locked_at=now()
                       WHERE mssv=%s AND (status='UNPAID' OR (status='LOCKED' AND locked_by=%s))
                       RETURNING amount""", (b.txn_id, mssv, b.txn_id))
        r = cur.fetchone()
    if not r: raise err(409, "TUITION_NOT_PAYABLE", "Học phí đã được thanh toán hoặc đang được xử lý")
    return {"amount": r["amount"]} # type: ignore

@app.post("/internal/tuitions/{mssv}/confirm")
def confirm(mssv: str, b: Txn, x_internal_key: str = Header(None)):
    internal(x_internal_key)
    with tx() as cur:
        cur.execute("""UPDATE tuitions SET status='PAID', paid_by_txn=locked_by, locked_at=NULL
                       WHERE mssv=%s AND (
                           (status='LOCKED' AND locked_by=%s) OR 
                           (status='PAID'   AND paid_by_txn=%s)
                       )""", (mssv, b.txn_id, b.txn_id))
        if cur.rowcount == 0:
            raise err(409, "NOT_LOCKED_BY_TXN", "Khoản học phí không bị khóa bởi giao dịch này")
    return {"status": "PAID"}

@app.post("/internal/tuitions/{mssv}/release")
def release(mssv: str, b: Txn, x_internal_key: str = Header(None)):
    internal(x_internal_key)
    with tx() as cur:
        cur.execute("""UPDATE tuitions SET status='UNPAID', locked_by=NULL, locked_at=NULL
                       WHERE mssv=%s AND status='LOCKED' AND locked_by=%s""", (mssv, b.txn_id))
    return {"status": "UNPAID"}