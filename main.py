from datetime import datetime, timedelta
from email.mime.text import MIMEText
from io import BytesIO
from typing import List, Optional, Union
import os
import re
import shutil
import smtplib
import uuid

import numpy as np
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from passlib.context import CryptContext
from pymongo import MongoClient, ASCENDING
import jwt

try:
    from web3 import Web3
except ImportError:
    # web3 (and its lru-dict/ckzg dependencies) can be finicky to build on
    # some machines. It's only needed for on-chain minting, which is fully
    # optional - everything else in the app works without it. If it's not
    # installed, on-chain minting is simply skipped and tokens are recorded
    # off-chain instead (see chain_is_configured()).
    Web3 = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MONGO_URI = os.environ.get("SKILLCOM_MONGO_URI", "mongodb://localhost:27017/")
SECRET_KEY = os.environ.get("SKILLCOM_SECRET_KEY", "dev-secret-change-me")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24 * 7
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")

# --- On-chain minting config -----------------------------------------------
# All optional. If any of these are unset, minting silently falls back to an
# off-chain (database-only) reputation token instead of failing the exchange.
# See ../contracts/SkillcomReputationToken.sol and the README for how to
# deploy the contract and fill these in.
CHAIN_RPC_URL = os.environ.get("SKILLCOM_CHAIN_RPC_URL")            # e.g. an Alchemy/Infura Sepolia URL
CONTRACT_ADDRESS = os.environ.get("SKILLCOM_CONTRACT_ADDRESS")      # deployed SkillcomReputationToken address
PLATFORM_PRIVATE_KEY = os.environ.get("SKILLCOM_PLATFORM_PRIVATE_KEY")  # platform wallet that pays gas & owns the contract
CHAIN_ID = int(os.environ.get("SKILLCOM_CHAIN_ID", "11155111"))     # default: Ethereum Sepolia testnet
BLOCK_EXPLORER_BASE = os.environ.get("SKILLCOM_BLOCK_EXPLORER_BASE", "https://sepolia.etherscan.io")

# --- Email (password reset) config ------------------------------------------
# Optional, same graceful-fallback pattern as on-chain minting above: if SMTP
# isn't configured, reset emails are printed to the server console instead of
# failing, so the reset flow is fully testable without real mail credentials.
SMTP_HOST = os.environ.get("SKILLCOM_SMTP_HOST")
SMTP_PORT = int(os.environ.get("SKILLCOM_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SKILLCOM_SMTP_USER")
SMTP_PASSWORD = os.environ.get("SKILLCOM_SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SKILLCOM_SMTP_FROM", SMTP_USER or "noreply@skillcom.local")
FRONTEND_BASE_URL = os.environ.get("SKILLCOM_FRONTEND_BASE_URL", "http://127.0.0.1:5500")

# --- Default admin account ---------------------------------------------------
DEFAULT_ADMIN_USERNAME = os.environ.get("SKILLCOM_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("SKILLCOM_ADMIN_PASSWORD", "root")

CONTRACT_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "string", "name": "skillName", "type": "string"},
            {"internalType": "string", "name": "sessionId", "type": "string"},
        ],
        "name": "mintToken",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "from", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "to", "type": "address"},
            {"indexed": True, "internalType": "uint256", "name": "tokenId", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
]

os.makedirs(UPLOAD_DIR, exist_ok=True)

# Fixed vocabulary of technical skills so peers can't type free-form, ambiguous
# skill names (avoids "js" vs "JavaScript" vs "javascript" style mismatches).
TECHNICAL_SKILLS = [
    "Python", "JavaScript", "TypeScript", "React", "Vue.js", "Node.js",
    "Machine Learning", "Deep Learning", "PyTorch", "TensorFlow",
    "Natural Language Processing", "Computer Vision", "Data Science",
    "Data Structures & Algorithms", "SQL", "MongoDB / NoSQL",
    "Solidity", "Smart Contracts", "Blockchain Development", "Web3.js",
    "Java", "C++", "C#", "Go", "Rust",
    "Docker", "Kubernetes", "AWS", "Azure", "DevOps / CI-CD",
    "Cybersecurity", "Network Engineering", "UI/UX Design",
    "Product Management", "Statistics", "Linear Algebra",
]

# ---------------------------------------------------------------------------
# App / DB / Security setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Skillcom API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

client = MongoClient(MONGO_URI)
db = client["skillcom_db"]
users_db = db["users"]
sessions_db = db["sessions"]
messages_db = db["messages"]
login_events_db = db["login_events"]

users_db.create_index([("username", ASCENDING)], unique=True)
# sparse=True: older/legacy accounts may not have an email yet, and a plain
# unique index would reject more than one such document (nulls collide).
users_db.create_index([("email", ASCENDING)], unique=True, sparse=True)
login_events_db.create_index([("logged_in_at", -1)])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def send_email(to_address: str, subject: str, body: str) -> None:
    if SMTP_HOST and SMTP_USER and SMTP_PASSWORD:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_address
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_address], msg.as_string())
    else:
        print("=" * 70)
        print(f"[DEV EMAIL - SKILLCOM_SMTP_* not configured] To: {to_address}")
        print(f"Subject: {subject}\n")
        print(body)
        print("=" * 70)


@app.on_event("startup")
def seed_admin_account():
    """Creates the default admin account (username 'admin' / password 'root'
    unless overridden via SKILLCOM_ADMIN_USERNAME / SKILLCOM_ADMIN_PASSWORD)
    the first time the app starts against a fresh database. Leaves it alone
    on every later startup, so changing the password later in the admin
    dashboard or database sticks."""
    if users_db.find_one({"role": "admin"}):
        return
    users_db.insert_one({
        "_id": uuid.uuid4().hex,
        "name": "Skillcom Admin",
        "username": DEFAULT_ADMIN_USERNAME.strip().lower(),
        "email": None,
        "password_hash": hash_password(DEFAULT_ADMIN_PASSWORD),
        "role": "admin",
        "is_active": True,
        "skills_offered": [],
        "skills_wanted": [],
        "photo_url": None,
        "wallet_address": None,
        "reputation_tokens": [],
        "created_at": datetime.utcnow(),
    })
    print(f"[Skillcom] Seeded default admin account -> username='{DEFAULT_ADMIN_USERNAME}' password='{DEFAULT_ADMIN_PASSWORD}'. Change this after first login.")

# Sentence embedding model, used for semantic AI matching between "skills
# wanted" and a candidate's "skills offered".
_embedding_model = None


def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embedding_model


# --- On-chain minting -------------------------------------------------------

_web3_client = None


def get_web3():
    """Returns a connected Web3 client, or None if web3 isn't installed or chain config is missing."""
    global _web3_client
    if Web3 is None or not CHAIN_RPC_URL:
        return None
    if _web3_client is None:
        _web3_client = Web3(Web3.HTTPProvider(CHAIN_RPC_URL))
    return _web3_client


def get_contract():
    w3 = get_web3()
    if w3 is None or not CONTRACT_ADDRESS:
        return None
    return w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=CONTRACT_ABI)


def chain_is_configured() -> bool:
    return bool(Web3 is not None and CHAIN_RPC_URL and CONTRACT_ADDRESS and PLATFORM_PRIVATE_KEY)


_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalize_wallet_address(address: str) -> str:
    """
    Validates and normalizes an Ethereum-style address. Uses web3's proper
    EIP-55 checksum encoding when web3 is installed; otherwise falls back to
    a plain regex check (still valid for linking - only the on-chain mint
    itself needs web3 installed).
    """
    if Web3 is not None:
        if not Web3.is_address(address):
            raise ValueError("invalid address")
        return Web3.to_checksum_address(address)

    if not _ETH_ADDRESS_RE.match(address):
        raise ValueError("invalid address")
    return address


def mint_onchain_token(to_wallet: str, skill_name: str, session_id: str) -> dict:
    """
    Mints a real SkillcomReputationToken NFT to `to_wallet` on-chain, paid for
    by the platform wallet (SKILLCOM_PLATFORM_PRIVATE_KEY) so the recipient
    doesn't need any testnet ETH themselves.

    Raises RuntimeError if on-chain minting isn't configured, or a web3/RPC
    exception if the transaction itself fails - callers should catch these
    and fall back to an off-chain-only token rather than blocking the
    Skillcom exchange flow.
    """
    if not chain_is_configured():
        raise RuntimeError(
            "On-chain minting is not configured. Set SKILLCOM_CHAIN_RPC_URL, "
            "SKILLCOM_CONTRACT_ADDRESS, and SKILLCOM_PLATFORM_PRIVATE_KEY to enable it."
        )

    w3 = get_web3()
    contract = get_contract()
    platform_account = w3.eth.account.from_key(PLATFORM_PRIVATE_KEY)
    to_checksum = Web3.to_checksum_address(to_wallet)

    fn = contract.functions.mintToken(to_checksum, skill_name, session_id)
    gas_estimate = fn.estimate_gas({"from": platform_account.address})

    tx = fn.build_transaction({
        "from": platform_account.address,
        "nonce": w3.eth.get_transaction_count(platform_account.address),
        "chainId": CHAIN_ID,
        "gas": int(gas_estimate * 1.2),
        "gasPrice": w3.eth.gas_price,
    })

    signed = w3.eth.account.sign_transaction(tx, private_key=PLATFORM_PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    # Parse the Transfer event to recover the minted tokenId.
    token_id = None
    transfer_topic = w3.keccak(text="Transfer(address,address,uint256)").hex()
    for log in receipt["logs"]:
        if log["topics"] and log["topics"][0].hex() == transfer_topic and len(log["topics"]) >= 4:
            token_id = int(log["topics"][3].hex(), 16)
            break

    tx_hash_hex = tx_hash.hex()
    return {
        "tx_hash": tx_hash_hex,
        "token_id": token_id,
        "explorer_url": f"{BLOCK_EXPLORER_BASE}/tx/{tx_hash_hex}" if BLOCK_EXPLORER_BASE else None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bcrypt_safe(password: str) -> str:
    """bcrypt cannot handle secrets longer than 72 bytes. Truncate on the byte
    boundary (not the character boundary) so multi-byte characters aren't
    split in half, then decode back to a string."""
    return password.encode("utf-8")[:72].decode("utf-8", errors="ignore")


def hash_password(password: str) -> str:
    return pwd_context.hash(_bcrypt_safe(password))


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(_bcrypt_safe(password), password_hash)


def create_access_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> dict:
    token = creds.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired, please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid authentication token.")

    user = users_db.find_one({"_id": payload["sub"]})
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists.")
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Your account has been suspended by an administrator.")
    return user


def get_current_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user


def public_user(user: dict, include_private: bool = False) -> dict:
    """Strip sensitive fields before sending a user document to the client.
    `include_private=True` is only passed for a user looking at their OWN
    account (register/login/me) - it adds email, role, and account status,
    none of which peers should see about each other."""
    data = {
        "id": user["_id"],
        "name": user.get("name"),
        "username": user.get("username"),
        "skills_offered": user.get("skills_offered", []),
        "skills_wanted": user.get("skills_wanted", []),
        "photo_url": user.get("photo_url"),
        "wallet_address": user.get("wallet_address"),
        "reputation_tokens": user.get("reputation_tokens", []),
        "created_at": user.get("created_at"),
    }
    if include_private:
        data["email"] = user.get("email")
        data["role"] = user.get("role", "student")
        data["is_active"] = user.get("is_active", True)
    return data


def semantic_score(wanted: List[str], offered: List[str]) -> int:
    """Cosine-similarity based compatibility between two skill lists (0-100)."""
    if not wanted or not offered:
        return 0
    model = get_embedding_model()
    wanted_vec = model.encode(", ".join(wanted))
    offered_vec = model.encode(", ".join(offered))
    denom = (np.linalg.norm(wanted_vec) * np.linalg.norm(offered_vec)) or 1e-8
    score = float(np.dot(wanted_vec, offered_vec) / denom)
    return max(0, min(100, round(score * 100)))


def compatibility_payload(current_user: dict, candidate: dict) -> dict:
    """
    Creative AI-compatibility payload combining:
      - a semantic match score (skills wanted <-> skills offered)
      - "token verification": how many of the candidate's reputation tokens
        are themselves *verified* by having completed a real confirmed session
        (i.e. not just self-declared skills, but proven-through-exchange skills).
    """
    base_score = semantic_score(
        current_user.get("skills_wanted", []),
        candidate.get("skills_offered", []),
    )
    tokens = candidate.get("reputation_tokens", [])
    verified_tokens = [t for t in tokens if t.get("verified")]
    # Verified tokens nudge the score up, rewarding proven experience.
    boost = min(15, len(verified_tokens) * 3)
    final_score = min(100, base_score + boost)
    return {
        "match_score": final_score,
        "semantic_score": base_score,
        "verified_token_count": len(verified_tokens),
        "total_token_count": len(tokens),
        "trust_boost": boost,
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class MessageRequest(BaseModel):
    to_user: str
    text: str = Field(min_length=1, max_length=2000)


class SessionProposeRequest(BaseModel):
    to_user: str
    my_offer: str
    topic: str
    session_time: str  # ISO datetime string from <input type="datetime-local">


class WalletLinkRequest(BaseModel):
    wallet_address: str


class EmailUpdateRequest(BaseModel):
    email: str


class PasswordResetRequest(BaseModel):
    email: str


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=6)


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@app.get("/api/skills")
def list_skills():
    return {"skills": TECHNICAL_SKILLS}


# ---------------------------------------------------------------------------
# Auth: register / login / me
# ---------------------------------------------------------------------------

@app.post("/api/register")
def register(
    name: str = Form(...),
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(..., min_length=6),
    skills_offered: str = Form(..., description="Comma-separated list of TECHNICAL_SKILLS values"),
    skills_wanted: str = Form(..., description="Comma-separated list of TECHNICAL_SKILLS values"),
    photo: Union[UploadFile, str, None] = File(default=None),
):
    username = username.strip().lower()
    email = email.strip().lower()

    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="That doesn't look like a valid email address.")
    if users_db.find_one({"username": username}):
        raise HTTPException(status_code=409, detail="That username is already taken.")
    if users_db.find_one({"email": email}):
        raise HTTPException(status_code=409, detail="That email is already registered.")

    offered_list = [s.strip() for s in skills_offered.split(",") if s.strip()]
    wanted_list = [s.strip() for s in skills_wanted.split(",") if s.strip()]

    for s in offered_list + wanted_list:
        if s not in TECHNICAL_SKILLS:
            raise HTTPException(status_code=400, detail=f"Unknown skill '{s}'. Choose from the provided skill list.")

    photo_url = None
    # Some clients (Swagger UI's "Try it out", certain browsers) send an empty
    # string instead of omitting the field entirely when no file is chosen -
    # treat that the same as "no photo" rather than erroring.
    if photo is not None and not isinstance(photo, str) and photo.filename:
        ext = os.path.splitext(photo.filename or "")[1] or ".jpg"
        filename = f"{uuid.uuid4().hex}{ext}"
        dest_path = os.path.join(UPLOAD_DIR, filename)
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(photo.file, f)
        photo_url = f"/uploads/{filename}"

    user_doc = {
        "_id": uuid.uuid4().hex,
        "name": name.strip(),
        "username": username,
        "email": email,
        "password_hash": hash_password(password),
        "role": "student",
        "is_active": True,
        "skills_offered": offered_list,
        "skills_wanted": wanted_list,
        "photo_url": photo_url,
        "wallet_address": None,
        "reputation_tokens": [],
        "created_at": datetime.utcnow(),
    }
    users_db.insert_one(user_doc)

    return {"message": "Registered successfully.", "user": public_user(user_doc, include_private=True)}


@app.post("/api/login")
def login(payload: LoginRequest):
    username = payload.username.strip().lower()
    user = users_db.find_one({"username": username})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Your account has been suspended by an administrator.")

    login_events_db.insert_one({
        "_id": uuid.uuid4().hex,
        "user_id": user["_id"],
        "username": user["username"],
        "role": user.get("role", "student"),
        "logged_in_at": datetime.utcnow(),
    })

    token = create_access_token(user["_id"])
    return {"access_token": token, "token_type": "bearer", "user": public_user(user, include_private=True)}


@app.get("/api/me")
def me(current_user: dict = Depends(get_current_user)):
    return public_user(current_user, include_private=True)


@app.post("/api/me/wallet")
def link_wallet(payload: WalletLinkRequest, current_user: dict = Depends(get_current_user)):
    """
    Links a wallet address to the logged-in user so future reputation tokens
    can be minted to it on-chain. This is a read-only address lookup on the
    frontend (MetaMask's eth_requestAccounts) - no signing or gas needed here,
    since the platform wallet pays for minting later.
    """
    try:
        checksum = normalize_wallet_address(payload.wallet_address)
    except ValueError:
        raise HTTPException(status_code=400, detail="That doesn't look like a valid wallet address.")

    users_db.update_one({"_id": current_user["_id"]}, {"$set": {"wallet_address": checksum}})
    return {"message": "Wallet linked.", "wallet_address": checksum}


@app.post("/api/me/email")
def update_email(payload: EmailUpdateRequest, current_user: dict = Depends(get_current_user)):
    email = payload.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="That doesn't look like a valid email address.")
    existing = users_db.find_one({"email": email})
    if existing and existing["_id"] != current_user["_id"]:
        raise HTTPException(status_code=409, detail="That email is already linked to another account.")
    users_db.update_one({"_id": current_user["_id"]}, {"$set": {"email": email}})
    return {"message": "Email updated.", "email": email}


# ---------------------------------------------------------------------------
# Password reset (email-verified)
# ---------------------------------------------------------------------------

RESET_TOKEN_LIFETIME_MINUTES = 30


def _issue_reset_link(user: dict) -> None:
    token = uuid.uuid4().hex
    users_db.update_one({"_id": user["_id"]}, {"$set": {
        "reset_token": token,
        "reset_token_expires": datetime.utcnow() + timedelta(minutes=RESET_TOKEN_LIFETIME_MINUTES),
    }})
    reset_link = f"{FRONTEND_BASE_URL}/reset-password.html?token={token}"
    send_email(
        user["email"],
        "Reset your Skillcom password",
        f"Hi {user.get('name', 'there')},\n\n"
        f"Click the link below to reset your Skillcom password. This link "
        f"expires in {RESET_TOKEN_LIFETIME_MINUTES} minutes:\n\n{reset_link}\n\n"
        f"If you didn't request this, you can safely ignore this email.",
    )


@app.post("/api/password-reset/request")
def request_password_reset(payload: PasswordResetRequest):
    """Used from the LOGGED-OUT login page ('Forgot password?'). Always
    returns the same generic message whether or not the email is registered,
    so this endpoint can't be used to discover which emails have accounts."""
    email = payload.email.strip().lower()
    generic_response = {"message": "If that email is registered, a password reset link has been sent."}
    user = users_db.find_one({"email": email})
    if not user:
        return generic_response
    _issue_reset_link(user)
    return generic_response


@app.post("/api/me/password-reset/request")
def request_own_password_reset(current_user: dict = Depends(get_current_user)):
    """Used from the profile page's 'Change Password' button - the user is
    already authenticated, so this sends the reset link straight to the
    email already on file rather than asking them to type it again."""
    if not current_user.get("email"):
        raise HTTPException(status_code=400, detail="No email is linked to your account yet. Add one above first.")
    _issue_reset_link(current_user)
    return {"message": f"A password reset link has been sent to {current_user['email']}."}


@app.post("/api/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirm):
    user = users_db.find_one({"reset_token": payload.token})
    if not user:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has already been used.")
    expires = user.get("reset_token_expires")
    if not expires or expires < datetime.utcnow():
        raise HTTPException(status_code=400, detail="This reset link has expired. Request a new one.")

    users_db.update_one(
        {"_id": user["_id"]},
        {
            "$set": {"password_hash": hash_password(payload.new_password)},
            "$unset": {"reset_token": "", "reset_token_expires": ""},
        },
    )
    return {"message": "Password updated. You can now log in with your new password."}


# ---------------------------------------------------------------------------
# Search / matching (the Instagram-like home feed)
# ---------------------------------------------------------------------------

@app.get("/api/users/search")
def search_users(q: str = Query("", description="Username or skill keyword"), current_user: dict = Depends(get_current_user)):
    query = q.strip()
    mongo_filter = {"_id": {"$ne": current_user["_id"]}}

    if query:
        mongo_filter["$or"] = [
            {"username": {"$regex": query, "$options": "i"}},
            {"name": {"$regex": query, "$options": "i"}},
            {"skills_offered": {"$regex": query, "$options": "i"}},
        ]

    results = []
    for candidate in users_db.find(mongo_filter).limit(50):
        entry = public_user(candidate)
        entry["compatibility"] = compatibility_payload(current_user, candidate)
        results.append(entry)

    results.sort(key=lambda u: u["compatibility"]["match_score"], reverse=True)
    return {"results": results}


@app.get("/api/users/{user_id}")
def get_user(user_id: str, current_user: dict = Depends(get_current_user)):
    candidate = users_db.find_one({"_id": user_id})
    if not candidate:
        raise HTTPException(status_code=404, detail="User not found.")
    entry = public_user(candidate)
    entry["compatibility"] = compatibility_payload(current_user, candidate)
    return entry


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@app.post("/api/messages")
def send_message(payload: MessageRequest, current_user: dict = Depends(get_current_user)):
    if not users_db.find_one({"_id": payload.to_user}):
        raise HTTPException(status_code=404, detail="Recipient not found.")

    msg = {
        "_id": uuid.uuid4().hex,
        "from_user": current_user["_id"],
        "to_user": payload.to_user,
        "text": payload.text,
        "created_at": datetime.utcnow(),
    }
    messages_db.insert_one(msg)
    return {"message": "sent"}


@app.get("/api/messages/{peer_id}")
def get_conversation(peer_id: str, current_user: dict = Depends(get_current_user)):
    convo = messages_db.find({
        "$or": [
            {"from_user": current_user["_id"], "to_user": peer_id},
            {"from_user": peer_id, "to_user": current_user["_id"]},
        ]
    }).sort("created_at", ASCENDING)

    return {
        "messages": [
            {
                "from_user": m["from_user"],
                "text": m["text"],
                "created_at": m["created_at"].isoformat(),
                "is_mine": m["from_user"] == current_user["_id"],
            }
            for m in convo
        ]
    }


# ---------------------------------------------------------------------------
# Exchange / booking flow
# ---------------------------------------------------------------------------

@app.post("/api/sessions/propose")
def propose_session(payload: SessionProposeRequest, current_user: dict = Depends(get_current_user)):
    peer = users_db.find_one({"_id": payload.to_user})
    if not peer:
        raise HTTPException(status_code=404, detail="Peer not found.")

    if payload.my_offer not in current_user.get("skills_offered", []):
        raise HTTPException(
            status_code=400,
            detail="You can only offer to teach a skill you've selected on your profile as one you can teach.",
        )
    if payload.topic not in peer.get("skills_offered", []):
        raise HTTPException(
            status_code=400,
            detail="That peer hasn't listed this skill as one they teach.",
        )

    session_doc = {
        "_id": "SK-" + uuid.uuid4().hex[:8].upper(),
        "participants": [current_user["_id"], peer["_id"]],
        "proposed_by": current_user["_id"],
        "my_offer": payload.my_offer,
        "topic": payload.topic,
        "session_time": payload.session_time,
        "agreed_by": [current_user["_id"]],  # proposer auto-agrees
        "status": "PENDING",
        "created_at": datetime.utcnow(),
        "confirmed_at": None,
    }
    sessions_db.insert_one(session_doc)
    return {"session_id": session_doc["_id"], "status": session_doc["status"]}


@app.post("/api/sessions/{session_id}/agree")
def agree_session(session_id: str, current_user: dict = Depends(get_current_user)):
    session_doc = sessions_db.find_one({"_id": session_id})
    if not session_doc:
        raise HTTPException(status_code=404, detail="Session not found.")
    if current_user["_id"] not in session_doc["participants"]:
        raise HTTPException(status_code=403, detail="You are not part of this session.")

    agreed = set(session_doc["agreed_by"])
    agreed.add(current_user["_id"])
    update = {"agreed_by": list(agreed)}

    minted_summary = []

    if agreed == set(session_doc["participants"]) and session_doc["status"] != "CONFIRMED":
        update["status"] = "CONFIRMED"
        update["confirmed_at"] = datetime.utcnow()

        # Mint a reputation token for each participant, tied to the topic taught/learned.
        # If the participant has linked a wallet and the platform has chain config
        # set, this is a REAL on-chain mint (platform wallet pays gas). Otherwise it
        # quietly falls back to an off-chain (database-only) token so the exchange
        # flow never breaks just because a wallet isn't linked or the chain is down.
        for uid in session_doc["participants"]:
            participant = users_db.find_one({"_id": uid})
            token = {
                "id": "TOK-" + uuid.uuid4().hex[:6].upper(),
                "skill": session_doc["topic"],
                "session_id": session_id,
                "verified": True,
                "issued_at": datetime.utcnow(),
                "on_chain": False,
            }

            wallet = participant.get("wallet_address") if participant else None
            if wallet and chain_is_configured():
                try:
                    chain_result = mint_onchain_token(wallet, session_doc["topic"], session_id)
                    token["on_chain"] = True
                    token["tx_hash"] = chain_result["tx_hash"]
                    token["chain_token_id"] = chain_result["token_id"]
                    token["explorer_url"] = chain_result["explorer_url"]
                except Exception as chain_error:
                    # Don't fail the whole exchange over a chain hiccup - keep the
                    # off-chain token and record why on-chain minting didn't happen.
                    token["mint_error"] = str(chain_error)

            users_db.update_one({"_id": uid}, {"$push": {"reputation_tokens": token}})
            minted_summary.append({"user_id": uid, **token, "issued_at": token["issued_at"].isoformat()})

        update["minted_tokens"] = minted_summary

    sessions_db.update_one({"_id": session_id}, {"$set": update})
    updated = sessions_db.find_one({"_id": session_id})
    return {
        "session_id": session_id,
        "status": updated["status"],
        "agreed_by": updated["agreed_by"],
    }


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, current_user: dict = Depends(get_current_user)):
    session_doc = sessions_db.find_one({"_id": session_id})
    if not session_doc:
        raise HTTPException(status_code=404, detail="Session not found.")
    if current_user["_id"] not in session_doc["participants"]:
        raise HTTPException(status_code=403, detail="You are not part of this session.")

    peer_id = [p for p in session_doc["participants"] if p != current_user["_id"]][0]
    peer = users_db.find_one({"_id": peer_id})

    return {
        "session_id": session_doc["_id"],
        "status": session_doc["status"],
        "topic": session_doc["topic"],
        "my_offer": session_doc["my_offer"],
        "session_time": session_doc["session_time"],
        "peer": public_user(peer) if peer else None,
        "agreed_by": session_doc["agreed_by"],
        "i_have_agreed": current_user["_id"] in session_doc["agreed_by"],
        "minted_tokens": session_doc.get("minted_tokens", []),
    }


@app.get("/api/sessions")
def my_sessions(current_user: dict = Depends(get_current_user)):
    docs = sessions_db.find({"participants": current_user["_id"]}).sort("created_at", -1)
    out = []
    for s in docs:
        peer_id = [p for p in s["participants"] if p != current_user["_id"]][0]
        peer = users_db.find_one({"_id": peer_id})
        out.append({
            "session_id": s["_id"],
            "status": s["status"],
            "topic": s["topic"],
            "session_time": s["session_time"],
            "peer_name": peer["name"] if peer else "Unknown",
        })
    return {"sessions": out}


@app.get("/api/meetings")
def my_meetings(current_user: dict = Depends(get_current_user)):
    """
    Classifies every session the current user is part of into three buckets
    for the Meetings page:
      - "pending":   proposed but not yet agreed to by both peers.
      - "arranged":  both peers agreed (CONFIRMED) and the scheduled time is
                     still in the future.
      - "occurred":  both peers agreed (CONFIRMED) and the scheduled time has
                     already passed.
    Classification is done fresh on every request against the live session
    time, not stored on the document, so a session automatically moves from
    "arranged" to "occurred" the moment its scheduled time passes.
    """
    now = datetime.utcnow()
    docs = sessions_db.find({"participants": current_user["_id"]}).sort("session_time", ASCENDING)

    pending, arranged, occurred = [], [], []
    for s in docs:
        other = [p for p in s["participants"] if p != current_user["_id"]]
        peer = users_db.find_one({"_id": other[0]}) if other else None

        entry = {
            "session_id": s["_id"],
            "status": s["status"],
            "topic": s["topic"],
            "my_offer": s["my_offer"],
            "session_time": s["session_time"],
            "peer": public_user(peer) if peer else None,
            "i_have_agreed": current_user["_id"] in s.get("agreed_by", []),
        }

        # <input type="datetime-local"> sends a naive local-time ISO string
        # (e.g. "2026-09-14T15:00"); treated as naive throughout so the
        # comparison against `now` stays consistent for a single-timezone
        # deployment like this one.
        try:
            session_dt = datetime.fromisoformat(s["session_time"])
        except (ValueError, TypeError):
            session_dt = None

        if s["status"] != "CONFIRMED":
            pending.append(entry)
        elif session_dt is not None and session_dt <= now:
            occurred.append(entry)
        else:
            arranged.append(entry)

    return {"pending": pending, "arranged": arranged, "occurred": occurred}


@app.get("/api/sessions/{session_id}/receipt")
def download_receipt(session_id: str, current_user: dict = Depends(get_current_user)):
    session_doc = sessions_db.find_one({"_id": session_id})
    if not session_doc:
        raise HTTPException(status_code=404, detail="Session not found.")
    if current_user["_id"] not in session_doc["participants"]:
        raise HTTPException(status_code=403, detail="You are not part of this session.")
    if session_doc["status"] != "CONFIRMED":
        raise HTTPException(status_code=400, detail="Receipt is only available once both peers have agreed.")

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import cm

    participants = [users_db.find_one({"_id": p}) for p in session_doc["participants"]]
    names = " & ".join(p["name"] for p in participants if p)

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 20)
    c.drawString(2 * cm, height - 3 * cm, "SKILLCOM")
    c.setFont("Helvetica", 12)
    c.drawString(2 * cm, height - 3.8 * cm, "Skill Barter Session Receipt")

    c.line(2 * cm, height - 4.2 * cm, width - 2 * cm, height - 4.2 * cm)

    lines = [
        f"Session ID: {session_doc['_id']}",
        f"Participants: {names}",
        f"Topic: {session_doc['topic']}",
        f"Skill offered by proposer: {session_doc['my_offer']}",
        f"Scheduled time: {session_doc['session_time']}",
        f"Status: {session_doc['status']}",
        f"Confirmed at: {session_doc['confirmed_at']}",
    ]
    y = height - 5.2 * cm
    c.setFont("Helvetica", 11)
    for line in lines:
        c.drawString(2 * cm, y, line)
        y -= 0.8 * cm

    minted = session_doc.get("minted_tokens", [])
    if minted:
        y -= 0.4 * cm
        c.setFont("Helvetica-Bold", 11)
        c.drawString(2 * cm, y, "Reputation Tokens Issued")
        y -= 0.7 * cm
        c.setFont("Helvetica", 9)
        for t in minted:
            holder = next((p["name"] for p in participants if p and p["_id"] == t["user_id"]), t["user_id"])
            if t.get("on_chain"):
                line = f"{holder}: {t['id']}  (on-chain, tx {t['tx_hash'][:10]}...{t['tx_hash'][-6:]})"
            else:
                line = f"{holder}: {t['id']}  (recorded off-chain)"
            c.drawString(2.3 * cm, y, line)
            y -= 0.6 * cm

    c.setFont("Helvetica-Oblique", 9)
    c.drawString(2 * cm, 2 * cm, "This receipt confirms a mutually-agreed peer skill exchange on Skillcom.")

    c.showPage()
    c.save()
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=skillcom-receipt-{session_id}.pdf"},
    )


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def admin_member_view(user: dict) -> dict:
    return {
        "id": user["_id"],
        "name": user.get("name"),
        "username": user.get("username"),
        "email": user.get("email"),
        "role": user.get("role", "student"),
        "is_active": user.get("is_active", True),
        "skills_offered": user.get("skills_offered", []),
        "skills_wanted": user.get("skills_wanted", []),
        "reputation_token_count": len(user.get("reputation_tokens", [])),
        "created_at": user.get("created_at"),
    }


@app.get("/api/admin/members")
def admin_list_members(current_admin: dict = Depends(get_current_admin)):
    """All registered members, newest first, for the admin dashboard's
    Members tab."""
    members = [admin_member_view(u) for u in users_db.find().sort("created_at", -1)]
    return {"members": members}


@app.post("/api/admin/members/{user_id}/reject")
def admin_reject_member(user_id: str, current_admin: dict = Depends(get_current_admin)):
    """Suspends a member's account: is_active=False blocks both future
    logins (checked in login()) and use of any existing token (checked in
    get_current_user()), so a rejected member is cut off immediately even
    mid-session."""
    target = users_db.find_one({"_id": user_id})
    if not target:
        raise HTTPException(status_code=404, detail="Member not found.")
    if target.get("role") == "admin":
        raise HTTPException(status_code=400, detail="Admin accounts can't be rejected.")

    users_db.update_one({"_id": user_id}, {"$set": {"is_active": False}})
    return {"message": f"{target['username']} has been suspended.", "is_active": False}


@app.post("/api/admin/members/{user_id}/reinstate")
def admin_reinstate_member(user_id: str, current_admin: dict = Depends(get_current_admin)):
    target = users_db.find_one({"_id": user_id})
    if not target:
        raise HTTPException(status_code=404, detail="Member not found.")

    users_db.update_one({"_id": user_id}, {"$set": {"is_active": True}})
    return {"message": f"{target['username']} has been reinstated.", "is_active": True}


@app.get("/api/admin/logins")
def admin_list_logins(
    scope: str = Query("recent", description="'recent' (last 24h) or 'all'"),
    current_admin: dict = Depends(get_current_admin),
):
    """Backs both the 'New Logins' tab (scope=recent -> last 24h, capped at
    100) and the 'All Logins' tab (scope=all -> full history, capped at 500
    for safety) on the admin dashboard."""
    mongo_filter = {}
    limit = 500
    if scope == "recent":
        mongo_filter["logged_in_at"] = {"$gte": datetime.utcnow() - timedelta(hours=24)}
        limit = 100

    events = login_events_db.find(mongo_filter).sort("logged_in_at", -1).limit(limit)
    logins = [
        {
            "id": e["_id"],
            "user_id": e["user_id"],
            "username": e["username"],
            "role": e.get("role", "student"),
            "logged_in_at": e["logged_in_at"].isoformat(),
        }
        for e in events
    ]
    return {"logins": logins, "scope": scope}
