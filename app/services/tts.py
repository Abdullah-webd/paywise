"""Gemini 3.1 Flash TTS — generates a voice note from a transcript + persona.

Mirrors the user's provided sample: we feed Gemini a transcript plus an
"audio profile" describing HOW it should speak (the persona). The persona is
what lets us sound native in Pidgin / Yoruba / Hausa / Igbo rather than
English-accented.

Returns raw audio bytes (WAV) ready to ship to Twilio as a media attachment.
"""
from __future__ import annotations

import logging
import mimetypes
import struct

from google import genai
from google.genai import types

from app.config import settings

log = logging.getLogger("paywise.tts")


# Personas per language — VERY detailed so Gemini TTS sounds like a native speaker.
# Each persona is a SPECIFIC person with a name, background, and speaking style.
_PERSONAS = {
    "pidgin": (
        "Your name is Aunty Bose. You are a 45-year-old Nigerian woman from Warri, Delta State. "
        "You have worked in customer service at a busy Lagos bank for 15 years. "
        "You speak NATURAL Nigerian Pidgin English — not British English trying to sound Pidgin. "
        "Your voice is warm, slightly deep, relaxed, and reassuring. You sound like someone's favourite aunty. "
        "You pronounce words the Nigerian way: 'thirty' sounds like 'toti', 'three' sounds like 'tree', "
        "'there' sounds like 'deh'. You drop your 'h' sounds naturally — 'him' becomes 'im', 'her' becomes 'am'. "
        "You stretch vowels expressively: 'Eeeeh, I don hear you.' 'Ah-ahn, no wahala.' "
        "Your rhythm is musical — Pidgin has a sing-song quality. Go up and down in pitch naturally. "
        "You sound like you are smiling while talking. Warm. Never robotic. Never monotone. "
        "Pause briefly between sentences like a real person thinking. "
        "For numbers and bank details, you switch to clear Nigerian-accented English pronunciation. "
        "SPEAK AT A NATURAL CONVERSATIONAL PACE — not too fast, not too slow. Like you are talking to a friend."
    ),
    "yoruba": (
        "Your name is Iya Gbemi. You are a 50-year-old Yoruba woman from Ibadan, Oyo State. "
        "You have run a successful textile business in Dugbe Market for 25 years. "
        "You speak AUTHENTIC Yoruba — not English with Yoruba words. Not Yoruba spoken with an American accent. "
        "Yoruba is a TONAL language. You MUST use the correct tones: do (low), re (mid), mi (high). "
        "The meaning changes with tone — 'owo' with mid-high means 'money', with high-high means 'hand'. Be precise. "
        "Your voice is maternal, warm, slightly high-pitched in the traditional Yoruba market woman style. "
        "You draw out vowels for emphasis: 'E kaaale oooo.' 'Owo re ti de oooo.' "
        "You naturally use filler sounds: 'Ehn...', 'Ooo...', 'Haa...' "
        "You code-switch to Yoruba-accented English ONLY for numbers: 'twenty thousand naira' becomes 'twen-ti tau-sand naira' with a Yoruba rhythm. "
        "Your pace is measured and dignified. You speak like someone who commands respect in the market. "
        "You sound like a Yoruba mother giving important financial advice to someone she cares about."
    ),
    "igbo": (
        "Your name is Mama Ngozi. You are a 48-year-old Igbo woman from Onitsha, Anambra State. "
        "You have owned a pharmaceutical wholesale business at Onitsha Main Market for 20 years. "
        "You speak AUTHENTIC Igbo — not English with Igbo vocabulary. Not Igbo spoken with a foreign accent. "
        "Igbo is tonal. High tone is marked with acute (o), low tone with grave (o). Get the tones right. "
        "Your voice is clear, confident, and slightly sharp — the classic Igbo trader woman's voice. "
        "You speak with conviction and energy. Igbo traders speak with purpose — you sound like someone who means business. "
        "You naturally use Igbo expressions: 'O di mma', 'Ndewo o', 'I na-anu?' "
        "You draw out words for emphasis: 'Ego gi aburula ooooo.' 'Daalu rinne.' "
        "You code-switch to Igbo-accented English for numbers only — crisp and precise. "
        "Your rhythm is faster than Yoruba but slower than Pidgin. You sound like a serious businesswoman who also cares deeply about her customers. "
        "You pause slightly between sentences. You sound like you are looking the person in the eye while speaking."
    ),
    "hausa": (
        "Your name is Hajia Aisha. You are a 46-year-old Hausa woman from Kano. "
        "You have run a grains wholesale business at Dawanau Market for 18 years. "
        "You speak AUTHENTIC Hausa — not English forced into Hausa words. Real Hausa as spoken in Kano. "
        "Hausa has two tones: high and low. Words change meaning with tone. Use proper Kano dialect pronunciation. "
        "Your voice is calm, dignified, and slightly low-pitched. You speak with the measured grace of a Northern Nigerian businesswoman. "
        "Your pace is SLOW and deliberate. Hausa speakers speak with patience and dignity — never rushed. "
        "You naturally use Hausa expressions: 'Sannu', 'Lafiya lau', 'Madalla', 'To', 'Gaskiya ne.' "
        "You draw out vowels gently: 'Kudin ka ya zooooo.' 'Nagode.' "
        "You code-switch to Hausa-accented English for numbers — careful and clear, with Hausa consonant patterns. "
        "You sound like a respected elder who is helping someone younger manage their money. Warm but dignified. Never casual. "
        "A slight smile in your voice. The tone is 'I am here to help you, and you can trust me completely.'"
    ),
    "english": (
        "Your name is Funke. You are a 35-year-old Nigerian woman from Lagos. "
        "You work as a professional customer service agent for a major Nigerian fintech company. "
        "You speak clear, professional Nigerian English — the kind you hear on Nigerian radio stations like Cool FM or Classic FM. "
        "Your accent is distinctly Nigerian but polished and easy to understand. Not British. Not American. Nigerian. "
        "You pronounce words the Nigerian way: 'thirty' is 'toti', 'better' is 'betta', 'water' is 'wota'. "
        "Your voice is warm, friendly, and professional. You sound like someone who solves problems efficiently. "
        "You naturally use Nigerian English expressions: 'No wahala', 'I go sort it out', 'Ehen, I understand.' "
        "You sound confident and reassuring. Like the helpful bank staff everyone wants to deal with. "
        "Your pace is moderate — not too fast, not too slow. Clear and articulate."
    ),
}


def _persona_for(lang: str) -> str:
    return _PERSONAS.get((lang or "").lower(), _PERSONAS["pidgin"])


# ---- WAV header helper (ported from the official sample) ---------------

def _parse_audio_mime_type(mime_type: str) -> dict:
    bits_per_sample = 16
    rate = 24000
    for param in mime_type.split(";"):
        param = param.strip()
        if param.lower().startswith("rate="):
            try:
                rate = int(param.split("=", 1)[1])
            except (ValueError, IndexError):
                pass
        elif param.startswith("audio/L"):
            try:
                bits_per_sample = int(param.split("L", 1)[1])
            except (ValueError, IndexError):
                pass
    return {"bits_per_sample": bits_per_sample, "rate": rate}


def _to_wav(audio_data: bytes, mime_type: str) -> bytes:
    p = _parse_audio_mime_type(mime_type)
    bits_per_sample, sample_rate = p["bits_per_sample"], p["rate"]
    num_channels = 1
    data_size = len(audio_data)
    bytes_per_sample = bits_per_sample // 8
    block_align = num_channels * bytes_per_sample
    byte_rate = sample_rate * block_align
    chunk_size = 36 + data_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", chunk_size, b"WAVE", b"fmt ", 16, 1, num_channels,
        sample_rate, byte_rate, block_align, bits_per_sample, b"data", data_size,
    )
    return header + audio_data


# ---- main entry --------------------------------------------------------

_client = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def synthesise(transcript: str, lang: str) -> bytes:
    """Synthesize a voice note. Returns WAV bytes.

    Args:
        transcript: the EXACT text to be spoken (already in the target language).
        lang:       one of pidgin/yoruba/igbo/hausa/english.

    Retries up to 3 times with exponential backoff on 429 quota errors.
    """
    import time

    persona = _persona_for(lang)
    system_instruction = (
        "You are a voice actor. You must BECOME the character described below. "
        "Speak EXACTLY as that person would speak — their accent, their rhythm, their emotion, their cultural mannerisms. "
        "Do NOT sound like a generic TTS voice reading text. Sound like a REAL Nigerian person having a REAL conversation. "
        "Match the tonal patterns of the language precisely."
    )
    prompt = (
        f"{system_instruction}\n\n"
        f"# CHARACTER PROFILE\n{persona}\n\n"
        f"# SCRIPT TO READ\n{transcript}\n\n"
        f"Read the script above AS the character. Become them completely."
    )

    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
    config = types.GenerateContentConfig(
        temperature=1,
        response_modalities=["audio"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=settings.gemini_tts_voice
                )
            )
        ),
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            data_buffer = b""
            response = _get_client().models.generate_content(
                model=settings.gemini_tts_model, contents=contents, config=config,
            )
            candidates = getattr(response, "candidates", None) or []
            for cand in candidates:
                cand_content = getattr(cand, "content", None)
                parts = getattr(cand_content, "parts", None) if cand_content else None
                for part in (parts or []):
                    inline = getattr(part, "inline_data", None)
                    raw = getattr(inline, "data", None) if inline else None
                    if not raw:
                        continue
                    mime = getattr(inline, "mime_type", "") or ""
                    ext = mimetypes.guess_extension(mime)
                    if ext is None:
                        raw = _to_wav(raw, mime)
                        ext = ".wav"
                    data_buffer += raw
            if not data_buffer:
                raise RuntimeError("Gemini TTS returned no audio")
            log.info("TTS synth ok (%d bytes, lang=%s)", len(data_buffer), lang)
            return data_buffer

        except Exception as exc:
            from google.genai import errors as _genai_errors
            if isinstance(exc, _genai_errors.ClientError) and "429" in str(exc):
                if attempt < max_retries - 1:
                    wait = min(8 * (2 ** attempt), 60)  # 8s, 16s, 32s
                    log.warning("TTS 429 quota hit, retry %d/%d in %ds", attempt + 1, max_retries, wait)
                    time.sleep(wait)
                    continue
            raise

    raise RuntimeError(f"Gemini TTS failed after {max_retries} retries")
