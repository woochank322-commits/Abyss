import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, request, abort
import speech_recognition as sr

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    AudioMessageContent,
    MemberJoinedEvent,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("velvet_auth_bot")

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
UPSTASH_REDIS_REST_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or "").strip()
UPSTASH_REDIS_REST_TOKEN = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip()

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN과 LINE_CHANNEL_SECRET 환경 변수를 설정해주세요.")

app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

KST = ZoneInfo("Asia/Seoul")
# 현재 시각 검증 시 분(minute) 허용 오차 (0이면 정확히 일치, 1이면 ±1분 허용)
ALLOWED_MINUTE_DIFF = 1

PROFILE_STORE_PATH = "/tmp/velvet_user_profiles.json"
REDIS_PROFILE_KEY = "velvet:auth:profiles"
FALLBACK_MATCH_WINDOW_SEC = 1800  # 같은 방 내 30분 이내 단독 대기 프로필 폴백 허용
profile_lock = Lock()

redis_client = None
if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN:
    try:
        from upstash_redis import Redis
        redis_client = Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
        logger.info("[STORAGE] Upstash Redis connected for auth profiles")
    except Exception as e:
        logger.warning("[STORAGE] Redis init failed, falling back to /tmp JSON: %s", e)

MSG_1 = (
    "📋─ 인증전 간단소개 ─ 📋\n"
    "• 년생 / 지역 / 성별  : \n"
    "• 사용할 닉네임(한글 두글자) : \n"
    "• 야단라 경험이 있으면 사용했던 임티(방장) : \n"
    "• 초대자 닉네임 : \n"
    "• bdsm 성향 :"
)

MSG_2 = (
    "🔎 ─ 인증방법 ─ 🔍\n\n"
    "두 가지 모두 해주셔야 합니다!\n"
    "번거로우시겠지만 부탁드려요 🙏🏻\n\n"
    "   —— 토스 인증 ——\n\n"
    "• 현재 시간이 보이게 생년월일 캡처 부탁드립니다.\n"
    "(토스 → 전체 → 설정 → 내 정보)\n\n"
    "📸 화면 캡처 후 보내주시면 됩니다.\n"
    "※ 생년월일을 제외한 나머지 개인정보는 꼭!!! 가려주세요.\n\n"
    "   —— 음성 녹음 인증 ——\n\n"
    "🎙️ 음성 녹음으로\n"
    "**「현재 시간 + 하실 닉네임」**을 말해서 보내주세요!\n\n"
    "예시)\n"
    "“오후 7시 52분, 닉네임 ○○입니다.”\n\n"
    "토스 먼저 인증 후 음성녹음 부탁드립니다."
)

MSG_3 = (
    "🌊─ 인증완료 ─🌊\n"
    "두가지! 설정 부탁드려요.\n\n"
    "1.    운영진이 보내준 닉 복사 해서 닉변해주세요.\n"
    "2.    프로필 본인사진으로 꼭 변경 해주세요 얼굴 아니어도 됩니다. (본인 신체부위도 가능)\n"
    "3.    설정 다 하셨다면 다 했다고 말씀해 주세요."
)

MSG_4 = (
    "지금 현재 인증이 어려우신 것 같습니다.!\n"
    "관리자 (하윙, 마루, 해달, 하연, 토리) 호출 후 문의 부탁드립니다.🫶"
)

MANUAL_REPLIES = {
    "/1": MSG_1,
    "/2": MSG_2,
    "/3": MSG_3,
    "/4": MSG_4,
}

GENDER_EMOJI_MAP = {
    "남": "🐧",
    "여": "🦭",
}

SUPERSCRIPT_MAP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def load_profiles() -> dict:
    if redis_client is not None:
        try:
            raw = redis_client.get(REDIS_PROFILE_KEY)
            if raw:
                data = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning("[PROFILE] redis load failed: %s", e)

    if not os.path.exists(PROFILE_STORE_PATH):
        return {}
    try:
        with open(PROFILE_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_profiles(profiles: dict):
    if redis_client is not None:
        try:
            redis_client.set(REDIS_PROFILE_KEY, json.dumps(profiles, ensure_ascii=False))
        except Exception as e:
            logger.warning("[PROFILE] redis save failed: %s", e)

    try:
        with open(PROFILE_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("[PROFILE] failed to persist profiles: %s", e)


user_profiles = load_profiles()


def set_user_profile(user_id: str | None, chat_id: str | None, profile: dict):
    now_ts = time.time()
    enriched = {
        **profile,
        "user_id": user_id or "",
        "chat_id": chat_id or "",
        "saved_at": now_ts,
    }
    key = user_id if user_id else f"anon_{chat_id or 'default'}_{int(now_ts * 1000)}"
    with profile_lock:
        latest = load_profiles()
        user_profiles.clear()
        user_profiles.update(latest)
        user_profiles[key] = enriched
        save_profiles(user_profiles)


def get_user_profile(user_id: str | None, chat_id: str | None = None) -> tuple[str | None, dict | None]:
    """
    1순위: user_id로 정확히 매칭된 프로필 반환
    2순위: user_id 매칭 실패 시 동일 채팅방(chat_id)에서 최근 30분 내 대기 중인 단일 프로필 폴백
    """
    now_ts = time.time()
    with profile_lock:
        latest = load_profiles()
        user_profiles.clear()
        user_profiles.update(latest)

        if user_id and user_id in user_profiles and isinstance(user_profiles[user_id], dict):
            return user_id, user_profiles[user_id]

        if chat_id:
            recent_room_profiles = [
                (k, v)
                for k, v in user_profiles.items()
                if isinstance(v, dict)
                and v.get("chat_id") == chat_id
                and (now_ts - float(v.get("saved_at", 0))) <= FALLBACK_MATCH_WINDOW_SEC
            ]
            if len(recent_room_profiles) == 1:
                return recent_room_profiles[0]

    return None, None


def clear_user_profile(storage_key: str | None):
    """인증 완료 후 해당 유저의 임시 프로필 정보를 폐기"""
    if not storage_key:
        return
    with profile_lock:
        latest = load_profiles()
        user_profiles.clear()
        user_profiles.update(latest)
        if storage_key in user_profiles:
            del user_profiles[storage_key]
            save_profiles(user_profiles)


def to_superscript(text: str) -> str:
    return text.translate(SUPERSCRIPT_MAP)


def is_valid_hangul_two_chars(nickname: str) -> bool:
    return bool(re.match(r"^[가-힣]{2}$", nickname))


def normalize_year2(raw_year: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw_year)
    if len(digits) == 4 and (digits.startswith("19") or digits.startswith("20")):
        return digits[-2:]
    if len(digits) == 2:
        return digits
    return None


def build_nickname(nickname: str, gender: str, year2: str) -> str:
    emoji = GENDER_EMOJI_MAP[gender]
    return f"{nickname}{emoji}{to_superscript(year2)}"


def _extract_field_value(line: str) -> str:
    cleaned = re.sub(r"^[•·\-*\s]+", "", line).strip()
    if ":" in cleaned:
        return cleaned.split(":", 1)[1].strip()
    if "：" in cleaned:
        return cleaned.split("：", 1)[1].strip()
    if "-" in cleaned:
        return cleaned.split("-", 1)[1].strip()
    return ""


def _extract_two_char_hangul_nickname(raw_nick: str) -> str | None:
    cleaned = raw_nick.strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"\([^)]*\)", " ", cleaned).strip()
    if is_valid_hangul_two_chars(cleaned):
        return cleaned
    for tok in re.split(r"[\s/,|]+", cleaned):
        tok_clean = re.sub(r"[^가-힣]", "", tok)
        if is_valid_hangul_two_chars(tok_clean):
            return tok_clean
    return None


def _parse_year_and_gender(info_val: str) -> tuple[str | None, str | None]:
    """'08 인천 남', '08/인천/남', '2007 / 서울 / 여' 등에서 2자리 년생과 성별 추출"""
    tokens = [t.strip() for t in re.split(r"[/,|\s]+", info_val) if t.strip()]
    year2 = None
    gender = None

    for tok in tokens:
        if not year2:
            candidate_year = normalize_year2(tok)
            if candidate_year:
                year2 = candidate_year
                continue
        if not gender:
            tok_hangul = re.sub(r"[^가-힣]", "", tok)
            if tok_hangul in ("여", "여자", "여성"):
                gender = "여"
            elif tok_hangul in ("남", "남자", "남성"):
                gender = "남"

    if not year2:
        m_y = re.search(r"(?:19|20)?(\d{2})\s*년생?", info_val)
        if m_y:
            year2 = m_y.group(1)
    if not gender:
        if re.search(r"(?:^|[/\s,])여(?:자|성)?(?:$|[/\s,])", info_val):
            gender = "여"
        elif re.search(r"(?:^|[/\s,])남(?:자|성)?(?:$|[/\s,])", info_val):
            gender = "남"

    return year2, gender


def parse_step1_form(text: str) -> dict | None:
    """
    1단계 간단소개 양식 파싱:
    1) 라벨 포함 형식 ('• 년생 / 지역 / 성별 : ...', '• 사용할 닉네임 : ...')
    2) 라벨을 모두 지우고 값만 보낸 형식 (예: 08 인천 남 / 인천 / 있음 / 하연 / 디그레이더)
    """
    lines = [
        re.sub(r"^[•·\-*\s]+", "", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]
    if len(lines) < 2:
        return None

    content_lines = [
        l for l in lines
        if "인증전 간단소개" not in l and not re.fullmatch(r"[📋─\-\s]+", l)
    ]
    if len(content_lines) < 2:
        return None

    # 케이스 1: 라벨이 포함된 양식
    info_line = next((l for l in content_lines if ("년생" in l or "성별" in l or "지역" in l)), None)
    nick_line = next((l for l in content_lines if "닉네임" in l and "초대자" not in l), None)

    if info_line and nick_line:
        info_val = _extract_field_value(info_line)
        nick_val_raw = _extract_field_value(nick_line)
        if info_val and nick_val_raw:
            nickname = _extract_two_char_hangul_nickname(nick_val_raw)
            year2, gender = _parse_year_and_gender(info_val)
            if year2 and gender and nickname:
                return {
                    "year2": year2,
                    "gender": gender,
                    "nickname": nickname,
                }

    # 케이스 2: 라벨 없이 답변 값만 4~6줄로 보낸 양식
    if 4 <= len(content_lines) <= 6:
        year2, gender = _parse_year_and_gender(content_lines[0])
        nickname = _extract_two_char_hangul_nickname(content_lines[1])
        if year2 and gender and nickname:
            return {
                "year2": year2,
                "gender": gender,
                "nickname": nickname,
            }

    return None


def convert_m4a_to_wav_16k_mono(audio_bytes: bytes, tmpdir: str) -> str:
    in_path = os.path.join(tmpdir, "voice.m4a")
    wav_path = os.path.join(tmpdir, "voice.wav")

    with open(in_path, "wb") as f:
        f.write(audio_bytes)

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            in_path,
            "-af",
            "volume=2.5",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            wav_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return wav_path


def transcribe_korean_speech(wav_path: str) -> str | None:
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
    try:
        return recognizer.recognize_google(audio_data, language="ko-KR")
    except sr.UnknownValueError:
        logger.warning("[STT] Google STT could not understand audio")
        return None
    except sr.RequestError as e:
        logger.warning("[STT] Google STT request failed: %s", e)
        return None
    except Exception as e:
        logger.warning("[STT] unexpected error: %s", e)
        return None


def korean_number_to_int(word: str, is_hour: bool = False) -> int | None:
    word = word.strip()
    if not word:
        return None
    if word.isdigit():
        return int(word)
    if word == "반":
        return 30

    if is_hour:
        hour_map = {
            "열두": 12, "열한": 11, "열": 10,
            "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5,
            "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9,
            "십이": 12, "십일": 11, "십": 10,
            "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5,
            "육": 6, "칠": 7, "팔": 8, "구": 9,
        }
        if word in hour_map:
            return hour_map[word]

    tens_map = {
        "십": 10,
        "이십": 20, "스물": 20,
        "삼십": 30, "서른": 30,
        "사십": 40, "마흔": 40,
        "오십": 50, "쉰": 50,
    }
    ones_map = {
        "": 0,
        "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5,
        "육": 6, "칠": 7, "팔": 8, "구": 9,
        "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5,
        "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9,
    }

    for tens_str, tens_val in tens_map.items():
        if word.startswith(tens_str):
            rem = word[len(tens_str) :]
            if rem in ones_map:
                return tens_val + ones_map[rem]

    if word in ones_map and ones_map[word] > 0:
        return ones_map[word]

    return None


def extract_spoken_hour_minute(text: str) -> list[tuple[int, int]]:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return []

    is_pm = "오후" in compact or "저녁" in compact or "밤" in compact
    is_am = "오전" in compact or "아침" in compact or "새벽" in compact

    hour_pattern = (
        r"(?:\d{1,2}|열두|열한|열|십이|십일|십|한|두|세|네|다섯|여섯|일곱|여덟|아홉|일|이|삼|사|오|육|칠|팔|구)"
    )
    ones_pattern = r"(?:일|이|삼|사|오|육|칠|팔|구|한|두|세|네|다섯|여섯|일곱|여덟|아홉)"
    minute_pattern = (
        rf"(?:\d{{1,2}}|(?:이십|스물|삼십|서른|사십|마흔|오십|쉰|십){ones_pattern}?|{ones_pattern}|반)"
    )

    hour_matches = re.findall(rf"({hour_pattern})시", compact)
    minute_matches = re.findall(rf"({minute_pattern})분", compact)

    if not minute_matches and re.search(rf"{hour_pattern}시반", compact):
        minute_matches = ["반"]

    if not hour_matches or not minute_matches:
        return []

    hours_24_candidates = set()
    for h_raw in hour_matches:
        h_val = korean_number_to_int(h_raw, is_hour=True)
        if h_val is None:
            continue
        if is_pm and 1 <= h_val <= 11:
            hours_24_candidates.add(h_val + 12)
        elif is_am and h_val == 12:
            hours_24_candidates.add(0)
        else:
            hours_24_candidates.add(h_val % 24)
            if not is_am and not is_pm and 1 <= h_val <= 11:
                hours_24_candidates.add(h_val + 12)

    minutes = []
    for m_raw in minute_matches:
        m_val = korean_number_to_int(m_raw, is_hour=False)
        if m_val is not None and 0 <= m_val <= 59:
            minutes.append(m_val)

    if not minutes:
        return []

    return [(h, m) for h in hours_24_candidates for m in minutes]


def verify_current_kst_time(transcript: str | None, now_kst: datetime) -> bool:
    if not transcript:
        return False

    spoken_pairs = extract_spoken_hour_minute(transcript)
    if not spoken_pairs:
        return False

    cur_hour = now_kst.hour
    cur_min = now_kst.minute

    for h, m in spoken_pairs:
        if h != cur_hour:
            continue
        diff = abs(m - cur_min)
        wrapped_diff = min(diff, 60 - diff)
        if wrapped_diff <= ALLOWED_MINUTE_DIFF:
            return True

    return False


def verify_spoken_script(transcript: str | None, now_kst: datetime) -> bool:
    """닉네임 확인 없이 현재 시각 일치 여부만 검증"""
    if not transcript:
        return False
    if not re.sub(r"\s+", "", transcript):
        return False
    return verify_current_kst_time(transcript, now_kst)


def download_line_audio(message_id: str) -> bytes:
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        return blob_api.get_message_content(message_id)


def reply_messages(reply_token: str, texts: list[str]):
    messages = [TextMessage(text=t) for t in texts[:5]]
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=messages,
            )
        )


def get_chat_id(event) -> str:
    source = getattr(event, "source", None)
    return (
        getattr(source, "group_id", None)
        or getattr(source, "room_id", None)
        or getattr(source, "user_id", None)
        or ""
    )


@app.route("/", methods=["GET"])
def health_check():
    return "VELVET HOUR Auth Bot is running.", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MemberJoinedEvent)
def handle_member_joined(event):
    reply_messages(event.reply_token, [MSG_1])


@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio_message(event):
    user_id = getattr(event.source, "user_id", None)
    chat_id = get_chat_id(event)
    profile_key, profile = get_user_profile(user_id, chat_id=chat_id)
    now_kst = datetime.now(KST)

    logger.info(
        "[VOICE] received message_id=%s user_id=%s chat_id=%s has_profile=%s",
        event.message.id,
        user_id,
        chat_id,
        bool(profile),
    )

    try:
        audio_bytes = download_line_audio(event.message.id)
        logger.info("[VOICE] downloaded bytes=%d user_id=%s", len(audio_bytes), user_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = convert_m4a_to_wav_16k_mono(audio_bytes, tmpdir)
            transcript = transcribe_korean_speech(wav_path)
    except Exception:
        logger.exception("[VOICE] audio processing error user_id=%s", user_id)
        reply_messages(event.reply_token, [MSG_4])
        return

    spoken_pairs = extract_spoken_hour_minute(transcript or "")
    script_ok = verify_spoken_script(transcript, now_kst)

    logger.info(
        "[VOICE] user_id=%s kst=%s parsed_pairs=%s script_ok=%s transcript=%r profile=%s",
        user_id,
        now_kst.strftime("%Y-%m-%d %H:%M:%S"),
        spoken_pairs,
        script_ok,
        transcript,
        profile,
    )

    if script_ok:
        outputs = [MSG_3]
        if profile:
            outputs.append(build_nickname(profile["nickname"], profile["gender"], profile["year2"]))
            clear_user_profile(profile_key)
        else:
            logger.warning("[VOICE] verified but no stored profile for user_id=%s chat_id=%s", user_id, chat_id)
        reply_messages(event.reply_token, outputs)
    else:
        reply_messages(event.reply_token, [MSG_4])


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    user_id = getattr(event.source, "user_id", None)
    chat_id = get_chat_id(event)

    if user_text in MANUAL_REPLIES:
        outputs = [MANUAL_REPLIES[user_text]]
        if user_text == "/3":
            p_key, p = get_user_profile(user_id, chat_id=chat_id)
            if p:
                outputs.append(build_nickname(p["nickname"], p["gender"], p["year2"]))
                clear_user_profile(p_key)
        reply_messages(event.reply_token, outputs)
        return

    parsed = parse_step1_form(user_text)
    if parsed:
        set_user_profile(user_id, chat_id, parsed)
        preview_nick = build_nickname(parsed["nickname"], parsed["gender"], parsed["year2"])
        logger.info("[PROFILE] saved user_id=%s chat_id=%s profile=%s", user_id, chat_id, parsed)
        reply_messages(
            event.reply_token,
            [
                MSG_2,
                f"✅ 간단소개 확인 완료 (예정 닉네임: {preview_nick})",
            ],
        )
        return

    parts = [p for p in re.split(r"[\s/]+", user_text) if p]
    if len(parts) == 3:
        nick, gender, raw_year = parts
        year2 = normalize_year2(raw_year)
        if is_valid_hangul_two_chars(nick) and gender in GENDER_EMOJI_MAP and year2:
            reply_messages(event.reply_token, [build_nickname(nick, gender, year2)])


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
