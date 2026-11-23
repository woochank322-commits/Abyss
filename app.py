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
        if rms < 0.03:
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

        if peak_ratio > 0.35 and best_lag > 0:
            freq = float(sample_rate / best_lag)
            if 70.0 <= freq <= 350.0:
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

    if median_pitch >= 175.0:
        est_gender = "여"
    elif median_pitch < 150.0:
        est_gender = "남"
    else:
        if median_centroid >= 2150.0:
            est_gender = "여"
        elif median_centroid < 1650.0:
            est_gender = "남"
        else:
            est_gender = "여" if median_pitch >= 165.0 else "남"

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


def verify_spoken_script(transcript: str | None, expected_nickname: str | None) -> bool:
    """실제 현재 시각과 일치할 필요는 없으나, 임의의 시간 표현(시/분)과 닉네임을 말했는지 검증"""
    if not transcript:
        return False

    compact = re.sub(r"\s+", "", transcript)
    if not compact:
        return False

    time_pattern = re.compile(
        r"(\d{1,2}시|\d{1,2}분|(한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|열한|열두)시|"
        r"(일|이|삼|사|오|십|이십|삼십|사십|오십|반)분|오전|오후)"
    )
    if not time_pattern.search(compact):
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
