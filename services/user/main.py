"""User Service: đăng nhập (JWT) + hồ sơ người dùng. DB riêng: userdb."""
import os, time, hashlib, secrets, jwt, psycopg2
from contextlib import contextmanager
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

SECRET, KEY = os.environ["JWT_SECRET"], os.environ["INTERNAL_KEY"]
app = FastAPI(title="User Service")

def err(s, code, msg): return HTTPException(s, {"code": code, "message": msg})

@contextmanager
def tx():
    c = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=RealDictCursor)
    try:
        with c, c.cursor() as cur:
            yield cur
    finally:
        c.close()

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()

def check_pw(pw, stored):
    return secrets.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)

SEED = [("sv_an", "Nguyễn Văn An", "0901000001", "an@example.com"),
        ("sv_binh", "Trần Thị Bình", "0901000002", "binh@example.com"),
        ("sv_chi", "Lê Minh Chi", "0901000003", "chi@example.com"),
        ("sv_dung", "Phạm Quốc Dũng", "0901000004", "dung@example.com")]

@app.on_event("startup")
def init():
    for _ in range(30):
        try:
            with tx() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS users(
                  id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
                  full_name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT NOT NULL)""")
                cur.execute("SELECT count(*) n FROM users")
                if cur.fetchone()["n"] == 0: # type: ignore
                    for u in SEED:  # mật khẩu mẫu: 123456
                        cur.execute("INSERT INTO users(username,password_hash,full_name,phone,email) VALUES(%s,%s,%s,%s,%s)",
                                    (u[0], hash_pw("123456"), u[1], u[2], u[3]))
            return
        except psycopg2.OperationalError:
            time.sleep(2)

def profile(r): return {"id": r["id"], "full_name": r["full_name"], "phone": r["phone"], "email": r["email"]}

class Login(BaseModel):
    username: str
    password: str

@app.post("/auth/login")
def login(b: Login):
    with tx() as cur:
        cur.execute("SELECT * FROM users WHERE username=%s", (b.username,))
        r = cur.fetchone()
    if not r or not check_pw(b.password, r["password_hash"]): # type: ignore
        raise err(401, "INVALID_CREDENTIALS", "Sai username hoặc password")
    token = jwt.encode({"sub": str(r["id"]), "exp": int(time.time()) + 3600}, SECRET, "HS256") # type: ignore
    return {"access_token": token, "token_type": "bearer", "user": profile(r)}

@app.get("/users/me")
def me(authorization: str = Header(None)):
    try:
        uid = int(jwt.decode(authorization.split()[1], SECRET, algorithms=["HS256"])["sub"])
    except Exception:
        raise err(401, "UNAUTHORIZED", "Chưa đăng nhập hoặc token hết hạn")
    with tx() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
        r = cur.fetchone()
    if not r: raise err(404, "USER_NOT_FOUND", "Không tìm thấy người dùng")
    return profile(r)

@app.get("/internal/users/{uid}")
def internal_user(uid: int, x_internal_key: str = Header(None)):
    if x_internal_key != KEY: raise err(403, "FORBIDDEN", "Internal only")
    with tx() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
        r = cur.fetchone()
    if not r: raise err(404, "USER_NOT_FOUND", "Không tìm thấy người dùng")
    return profile(r)