from difflib import SequenceMatcher
import json
import logging
import os
import re
import subprocess
import tempfile
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

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN과 LINE_CHANNEL_SECRET 환경 변수를 설정해주세요.")

app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

KST = ZoneInfo("Asia/Seoul")
# 현재 시각 검증 시 분(minute) 허용 오차 (0이면 정확히 일치, 1이면 ±1분 허용)
ALLOWED_MINUTE_DIFF = 1
# STT 닉네임 유사도 허용 기준 (0.0~1.0, 부분 문자열 포함 시에는 무조건 통과)
NICKNAME_SIMILARITY_THRESHOLD = 0.65

PROFILE_STORE_PATH = "/tmp/velvet_user_profiles.json"
profile_lock = Lock()

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
    "인증 후 입장 가능하실때 운영진분께 다시 문의 부탁드립니다.🫶"
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
    if not os.path.exists(PROFILE_STORE_PATH):
        return {}
    try:
        with open(PROFILE_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_profiles(profiles: dict):
    try:
        with open(PROFILE_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("[PROFILE] failed to persist profiles: %s", e)


user_profiles = load_profiles()


def set_user_profile(user_id: str, profile: dict):
    with profile_lock:
        user_profiles[user_id] = profile
        save_profiles(user_profiles)


def get_user_profile(user_id: str | None) -> dict | None:
    if not user_id:
        return None
    with profile_lock:
        if user_id in user_profiles:
            return user_profiles[user_id]
        latest = load_profiles()
        user_profiles.update(latest)
        return user_profiles.get(user_id)


def clear_user_profile(user_id: str | None):
    """인증 완료 후 해당 유저의 임시 프로필 정보를 폐기"""
    if not user_id:
        return
    with profile_lock:
        if user_id in user_profiles:
            del user_profiles[user_id]
            save_profiles(user_profiles)


def to_superscript(text: str) -> str:
    return text.translate(SUPERSCRIPT_MAP)


def is_valid_hangul_two_chars(nickname: str) -> bool:
    return bool(re.match(r"^[가-힣]{2}$", nickname))


def normalize_year2(raw_year: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw_year)
    if len(digits) == 4:
        return digits[-2:]
    if len(digits) == 2:
        return digits
    return None


def build_nickname(nickname: str, gender: str, year2: str) -> str:
    emoji = GENDER_EMOJI_MAP[gender]
    return f"{nickname}{emoji}{to_superscript(year2)}"


def parse_step1_form(text: str) -> dict | None:
    """1단계 양식이 작성되었는지 검증하고 년생/성별/닉네임을 추출"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    info_line = next((l for l in lines if ("년생" in l or "성별" in l) and (":" in l or "-" in l)), None)
    nick_line = next((l for l in lines if "닉네임" in l and (":" in l or "-" in l)), None)
    inviter_line = next((l for l in lines if "초대자" in l and (":" in l or "-" in l)), None)
    tendency_line = next((l for l in lines if "성향" in l and (":" in l or "-" in l)), None)

    if not all([info_line, nick_line, inviter_line, tendency_line]):
        return None

    sep = ":" if ":" in info_line else "-"
    info_val = info_line.split(sep, 1)[1].strip()

    nick_sep = ":" if ":" in nick_line else "-"
    nick_val = nick_line.split(nick_sep, 1)[1].strip()

    inv_sep = ":" if ":" in inviter_line else "-"
    inviter_val = inviter_line.split(inv_sep, 1)[1].strip()

    ten_sep = ":" if ":" in tendency_line else "-"
    tendency_val = tendency_line.split(ten_sep, 1)[1].strip()

    if not info_val or not nick_val or not inviter_val or not tendency_val:
        return None

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
            if tok == "여" or ("여" in tok and "남" not in tok and "년생" not in tok):
                gender = "여"
            elif tok == "남" or ("남" in tok and "여" not in tok and "년생" not in tok):
                gender = "남"

    if not year2 or not gender or not is_valid_hangul_two_chars(nick_val):
        return None

    return {
        "year2": year2,
        "gender": gender,
        "nickname": nick_val,
    }


def convert_m4a_to_wav_16k_mono(audio_bytes: bytes, tmpdir: str) -> str:
    in_path = os.path.join(tmpdir, "voice.m4a")
    wav_path = os.path.join(tmpdir, "voice.wav")

    with open(in_path, "wb") as f:
        f.write(audio_bytes)

    # 조용한 발화 인식을 위해 볼륨을 소폭 부스팅(2.5x)하여 16kHz 모노 WAV로 변환
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
    minute_pattern = (
        r"(?:\d{1,2}|이십[일-구한-아홉]?|스물[일-구한-아홉]?|"
        r"삼십[일-구한-아홉]?|서른[일-구한-아홉]?|"
        r"사십[일-구한-아홉]?|마흔[일-구한-아홉]?|"
        r"오십[일-구한-아홉]?|쉰[일-구한-아홉]?|"
        r"십[일-구한-아홉]?|일|이|삼|사|오|육|칠|팔|구|반)"
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


def matches_nickname_flexible(compact_transcript: str, expected_nickname: str) -> bool:
    if expected_nickname in compact_transcript:
        return True

    nick_len = len(expected_nickname)
    if nick_len == 0 or len(compact_transcript) < nick_len:
        return False

    best_ratio = 0.0
    for idx in range(len(compact_transcript) - nick_len + 1):
        chunk = compact_transcript[idx : idx + nick_len]
        ratio = SequenceMatcher(None, expected_nickname, chunk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio

    return best_ratio >= NICKNAME_SIMILARITY_THRESHOLD


def verify_spoken_script(transcript: str | None, expected_nickname: str | None, now_kst: datetime) -> bool:
    if not transcript:
        return False

    compact = re.sub(r"\s+", "", transcript)
    if not compact:
        return False

    if not verify_current_kst_time(transcript, now_kst):
        return False

    if expected_nickname:
        return matches_nickname_flexible(compact, expected_nickname)

    return True


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
    profile = get_user_profile(user_id)
    now_kst = datetime.now(KST)

    logger.info(
        "[VOICE] received message_id=%s user_id=%s has_profile=%s",
        event.message.id,
        user_id,
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

    if not profile:
        logger.info("[VOICE] missing profile user_id=%s transcript=%r", user_id, transcript)
        reply_messages(event.reply_token, [MSG_1])
        return

    expected_nick = profile["nickname"]
    spoken_pairs = extract_spoken_hour_minute(transcript or "")
    script_ok = verify_spoken_script(transcript, expected_nick, now_kst)

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
        nickname_str = build_nickname(profile["nickname"], profile["gender"], profile["year2"])
        reply_messages(event.reply_token, [MSG_3, nickname_str])
        clear_user_profile(user_id)
    else:
        reply_messages(event.reply_token, [MSG_4])


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    user_id = getattr(event.source, "user_id", None)

    if user_text in MANUAL_REPLIES:
        outputs = [MANUAL_REPLIES[user_text]]
        if user_text == "/3":
            p = get_user_profile(user_id)
            if p:
                outputs.append(build_nickname(p["nickname"], p["gender"], p["year2"]))
                clear_user_profile(user_id)
        reply_messages(event.reply_token, outputs)
        return

    parsed = parse_step1_form(user_text)
    if parsed:
        if user_id:
            set_user_profile(user_id, parsed)
            logger.info("[PROFILE] saved user_id=%s profile=%s", user_id, parsed)
        else:
            logger.warning("[PROFILE] parsed form without user_id: %s", parsed)
        reply_messages(event.reply_token, [MSG_2])
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
