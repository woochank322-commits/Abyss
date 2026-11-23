import os
import re
import subprocess
import tempfile
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

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN과 LINE_CHANNEL_SECRET 환경 변수를 설정해주세요.")

app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

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

# 유저별 /1 입력 정보 저장: {user_id: {"year2": "09", "gender": "여", "nickname": "마루"}}
user_profiles = {}


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
    """/1 양식이 작성되었는지 검증하고 년생/성별/닉네임을 추출"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    info_line = next((l for l in lines if "년생" in l and ":" in l), None)
    nick_line = next((l for l in lines if "닉네임" in l and ":" in l), None)
    inviter_line = next((l for l in lines if "초대자" in l and ":" in l), None)
    tendency_line = next((l for l in lines if "성향" in l and ":" in l), None)

    if not all([info_line, nick_line, inviter_line, tendency_line]):
        return None

    info_val = info_line.split(":", 1)[1].strip()
    nick_val = nick_line.split(":", 1)[1].strip()
    inviter_val = inviter_line.split(":", 1)[1].strip()
    tendency_val = tendency_line.split(":", 1)[1].strip()

    if not info_val or not nick_val or not inviter_val or not tendency_val:
        return None

    # '09 / 서울 / 여' 또는 '2009년생, 잠실, 남' 등 구분자 유연 처리
    tokens = [t.strip() for t in re.split(r"[/,|]+", info_val) if t.strip()]
    if len(tokens) < 3:
        return None

    year2 = normalize_year2(tokens[0])
    gender_token = tokens[2]

    gender = None
    if "여" in gender_token and "남" not in gender_token:
        gender = "여"
    elif "남" in gender_token and "여" not in gender_token:
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


def estimate_gender_hz(wav_path: str) -> str | None:
    sample_rate, samples = wavfile.read(wav_path)
    if samples.size == 0:
        return None

    audio = samples.astype(np.float32)
    max_val = np.max(np.abs(audio))
    if max_val > 0:
        audio = audio / max_val

    frame_len = int(sample_rate * 0.03)
    hop_len = int(sample_rate * 0.015)
    min_lag = max(1, int(sample_rate / 350))
    max_lag = min(frame_len - 1, int(sample_rate / 70))

    pitches = []
    for start in range(0, len(audio) - frame_len, hop_len):
        frame = audio[start : start + frame_len]
        if np.sqrt(np.mean(frame ** 2)) < 0.03:
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
        if (region[best_idx] / corr[0]) > 0.35 and best_lag > 0:
            freq = sample_rate / best_lag
            if 70.0 <= freq <= 350.0:
                pitches.append(freq)

    if not pitches:
        return None

    median_hz = float(np.median(pitches))
    return "여" if median_hz >= 165.0 else "남"


def transcribe_korean_speech(wav_path: str) -> str | None:
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
    try:
        return recognizer.recognize_google(audio_data, language="ko-KR")
    except Exception:
        return None


def verify_spoken_script(transcript: str | None, expected_nickname: str | None) -> bool:
    """현재 시간 표현(시/분)과 하실 닉네임 포함 여부 검증"""
    if not transcript:
        return False

    compact = re.sub(r"\s+", "", transcript)
    has_time = bool(re.search(r"(\d{1,2}시|분|오전|오후)", compact))
    if not has_time:
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

    try:
        audio_bytes = download_line_audio(event.message.id)
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = convert_m4a_to_wav_16k_mono(audio_bytes, tmpdir)
            est_gender = estimate_gender_hz(wav_path)
            transcript = transcribe_korean_speech(wav_path)
    except Exception:
        reply_messages(event.reply_token, [MSG_4])
        return

    expected_gender = profile["gender"] if profile else None
    expected_nick = profile["nickname"] if profile else None

    gender_ok = est_gender is not None and (expected_gender is None or est_gender == expected_gender)
    script_ok = verify_spoken_script(transcript, expected_nick)

    if gender_ok and script_ok:
        outputs = [MSG_3]
        if profile:
            outputs.append(build_nickname(profile["nickname"], profile["gender"], profile["year2"]))
        reply_messages(event.reply_token, outputs)
    else:
        reply_messages(event.reply_token, [MSG_4])


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    user_id = getattr(event.source, "user_id", None)

    # 수동 명령어 /1, /2, /3, /4
    if user_text in MANUAL_REPLIES:
        outputs = [MANUAL_REPLIES[user_text]]
        if user_text == "/3" and user_id and user_id in user_profiles:
            p = user_profiles[user_id]
            outputs.append(build_nickname(p["nickname"], p["gender"], p["year2"]))
        reply_messages(event.reply_token, outputs)
        return

    # /1 양식 작성 후 전송 시 자동 /2 발송 및 프로필 저장
    parsed = parse_step1_form(user_text)
    if parsed:
        if user_id:
            user_profiles[user_id] = parsed
        reply_messages(event.reply_token, [MSG_2])
        return

    # 수동 닉네임 생성 보조 입력: "닉네임 성별 년생" (예: 마루 여 09 -> 마루🦭⁰⁹)
    parts = user_text.split()
    if len(parts) == 3:
        nick, gender, raw_year = parts
        year2 = normalize_year2(raw_year)
        if is_valid_hangul_two_chars(nick) and gender in GENDER_EMOJI_MAP and year2:
            reply_messages(event.reply_token, [build_nickname(nick, gender, year2)])


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
