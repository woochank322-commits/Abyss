import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, request, abort
import numpy as np
from scipy.io import wavfile
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

# 유저별 임시 인증 정보 저장: {user_id: {"year2": "09", "gender": "여", "nickname": "마루"}}
user_profiles = {}


def clear_user_profile(user_id: str | None):
    """인증 완료 후 해당 유저의 임시 프로필 정보를 폐기"""
    if user_id and user_id in user_profiles:
        del user_profiles[user_id]


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

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            in_path,
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


def compute_spectral_centroid(frame: np.ndarray, sample_rate: int) -> float:
    """단일 프레임의 스펙트럼 중심(Spectral Centroid, Hz) 계산"""
    windowed = frame * np.hanning(len(frame))
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(len(windowed), d=1.0 / sample_rate)

    band_mask = (freqs >= 80.0) & (freqs <= 6000.0)
    band_mag = spectrum[band_mask]
    band_freqs = freqs[band_mask]

    total_mag = float(np.sum(band_mag))
    if total_mag <= 1e-9:
        return 0.0
    return float(np.sum(band_freqs * band_mag) / total_mag)


def analyze_voice_features(wav_path: str) -> dict:
    """녹음 길이, 유성음 비율, 피치 중앙값(Hz), 스펙트럼 중심 중앙값(Hz) 및 성별 추정"""
    sample_rate, samples = wavfile.read(wav_path)
    if samples.size == 0 or sample_rate <= 0:
        return {"ok": False}

    duration_sec = float(len(samples)) / float(sample_rate)
    if duration_sec < 1.0:
        return {"ok": False, "reason": "short_audio"}

    audio = samples.astype(np.float32)
    max_val = float(np.max(np.abs(audio)))
    if max_val > 0:
        audio = audio / max_val

    frame_len = int(sample_rate * 0.03)
    hop_len = int(sample_rate * 0.015)
    min_lag = max(1, int(sample_rate / 350))
    max_lag = min(frame_len - 1, int(sample_rate / 70))

    total_frames = 0
    voiced_frames = 0
    pitches = []
    centroids = []

    for start in range(0, len(audio) - frame_len, hop_len):
        total_frames += 1
        frame = audio[start : start + frame_len]
        rms = float(np.sqrt(np.mean(frame ** 2)))
        if rms < 0.025:
            continue

        windowed = frame * np.hanning(frame_len)
        corr = np.correlate(windowed, windowed, mode="full")[frame_len - 1 :]
        if corr[0] <= 0:
            continue

        region = corr[min_lag : max_lag + 1]
        if region.size == 0:
            continue

        best_idx = int(np.argmax(region))
        best_lag = min_lag + best_idx
        peak_ratio = float(region[best_idx] / corr[0])

        if peak_ratio > 0.32 and best_lag > 0:
            freq = float(sample_rate / best_lag)
            if 75.0 <= freq <= 350.0:
                voiced_frames += 1
                pitches.append(freq)
                sc = compute_spectral_centroid(frame, sample_rate)
                if sc > 0:
                    centroids.append(sc)

    voiced_ratio = (voiced_frames / total_frames) if total_frames > 0 else 0.0
    if voiced_ratio < 0.15 or not pitches:
        return {"ok": False, "reason": "low_voiced_ratio"}

    median_pitch = float(np.median(pitches))
    median_centroid = float(np.median(centroids)) if centroids else 0.0

    # 여성 기본 주파수(보통 165~260Hz)와 남성 기본 주파수(보통 85~165Hz)를 반영한 판별
    if median_pitch >= 165.0:
        est_gender = "여"
    elif median_pitch <= 145.0:
        est_gender = "남"
    else:
        # 145~165Hz 중간 구간은 스펙트럼 중심(밝기)으로 보완
        est_gender = "여" if median_centroid >= 1900.0 else "남"

    return {
        "ok": True,
        "gender": est_gender,
        "duration_sec": round(duration_sec, 2),
        "voiced_ratio": round(voiced_ratio * 100, 1),
        "pitch_hz": round(median_pitch, 1),
        "centroid_hz": round(median_centroid, 1),
    }


def transcribe_korean_speech(wav_path: str) -> str | None:
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
    try:
        return recognizer.recognize_google(audio_data, language="ko-KR")
    except Exception:
        return None


def korean_number_to_int(word: str) -> int | None:
    """'열한', '십칠', '이십오', '반' 등 한국어 수사를 정수로 변환"""
    word = word.strip()
    if not word:
        return None
    if word.isdigit():
        return int(word)
    if word == "반":
        return 30

    hour_map = {
        "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6,
        "일곱": 7, "여덟": 8, "아홉": 9, "열": 10, "열한": 11, "열두": 12,
        "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6,
        "칠": 7, "팔": 8, "구": 9, "십": 10, "십일": 11, "십이": 12,
    }
    if word in hour_map:
        return hour_map[word]

    tens_map = {"": 0, "십": 10, "이십": 20, "삼십": 30, "사십": 40, "오십": 50}
    ones_map = {
        "": 0, "일": 1, "이": 2, "삼": 3, "사": 4,
        "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9,
    }

    for tens_str, tens_val in tens_map.items():
        if tens_str and word.startswith(tens_str):
            rem = word[len(tens_str) :]
            if rem in ones_map:
                return tens_val + ones_map[rem]

    if word in ones_map and ones_map[word] > 0:
        return ones_map[word]

    return None


def extract_spoken_hour_minute(text: str) -> list[tuple[int, int]]:
    """음성 인식 텍스트에서 발화된 (시_24h_후보들, 분) 목록을 추출"""
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return []

    is_pm = "오후" in compact or "저녁" in compact or "밤" in compact
    is_am = "오전" in compact or "아침" in compact or "새벽" in compact

    hour_matches = re.findall(
        r"(\d{1,2}|열한|열두|다섯|여섯|일곱|여덟|아홉|열|한|두|세|네|십일|십이|십|일|이|삼|사|오|육|칠|팔|구)\s*시",
        compact,
    )
    minute_matches = re.findall(
        r"(\d{1,2}|이십|삼십|사십|오십|십|일|이|삼|사|오|육|칠|팔|구|반)\s*분",
        compact,
    )

    if not hour_matches:
        return []

    hours_24_candidates = set()
    for h_raw in hour_matches:
        h_val = korean_number_to_int(h_raw)
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
        m_val = korean_number_to_int(m_raw)
        if m_val is not None and 0 <= m_val <= 59:
            minutes.append(m_val)

    if not minutes:
        return []

    return [(h, m) for h in hours_24_candidates for m in minutes]


def verify_current_kst_time(transcript: str | None, now_kst: datetime) -> bool:
    """현재 한국 시간(KST)과 말한 시각(시/분)이 일치하는지 검증"""
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


def verify_spoken_script(transcript: str | None, expected_nickname: str | None, now_kst: datetime) -> bool:
    """현재 KST 시간과 닉네임을 함께 말했는지 검증"""
    if not transcript:
        return False

    compact = re.sub(r"\s+", "", transcript)
    if not compact:
        return False

    if not verify_current_kst_time(transcript, now_kst):
        return False

    if expected_nickname:
        return expected_nickname in compact

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
    profile = user_profiles.get(user_id) if user_id else None
    now_kst = datetime.now(KST)

    try:
        audio_bytes = download_line_audio(event.message.id)
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = convert_m4a_to_wav_16k_mono(audio_bytes, tmpdir)
            voice_info = analyze_voice_features(wav_path)
            transcript = transcribe_korean_speech(wav_path)
    except Exception:
        logger.exception("[VOICE] audio processing error user_id=%s", user_id)
        reply_messages(event.reply_token, [MSG_4])
        return

    logger.info(
        "[VOICE] user_id=%s kst=%s voice_info=%s transcript=%r profile=%s",
        user_id,
        now_kst.strftime("%Y-%m-%d %H:%M:%S"),
        voice_info,
        transcript,
        profile,
    )

    if not voice_info.get("ok"):
        reply_messages(event.reply_token, [MSG_4])
        return

    est_gender = voice_info["gender"]
    expected_gender = profile["gender"] if profile else None
    expected_nick = profile["nickname"] if profile else None

    gender_ok = expected_gender is None or est_gender == expected_gender
    script_ok = verify_spoken_script(transcript, expected_nick, now_kst)

    if gender_ok and script_ok:
        outputs = [MSG_3]
        if profile:
            outputs.append(build_nickname(profile["nickname"], profile["gender"], profile["year2"]))
        reply_messages(event.reply_token, outputs)
        clear_user_profile(user_id)
    else:
        reply_messages(event.reply_token, [MSG_4])


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    user_id = getattr(event.source, "user_id", None)

    if user_text in MANUAL_REPLIES:
        outputs = [MANUAL_REPLIES[user_text]]
        if user_text == "/3" and user_id and user_id in user_profiles:
            p = user_profiles[user_id]
            outputs.append(build_nickname(p["nickname"], p["gender"], p["year2"]))
            clear_user_profile(user_id)
        reply_messages(event.reply_token, outputs)
        return

    parsed = parse_step1_form(user_text)
    if parsed:
        if user_id:
            user_profiles[user_id] = parsed
        reply_messages(event.reply_token, [MSG_2])
        return

    parts = user_text.split()
    if len(parts) == 3:
        nick, gender, raw_year = parts
        year2 = normalize_year2(raw_year)
        if is_valid_hangul_two_chars(nick) and gender in GENDER_EMOJI_MAP and year2:
            reply_messages(event.reply_token, [build_nickname(nick, gender, year2)])


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
