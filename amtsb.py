# -*- coding: utf-8 -*-
"""
AMTSB – Automatic Movie Translator SuBtitles

Tłumaczy napisy na żywo, nad dowolnym odtwarzaczem (Netflix, YouTube, VLC…):
  * Z ekranu – czyta napisy filmu (OCR), zakrywa je czarnym paskiem i pokazuje tłumaczenie,
    ok. 0,3–0,6 s po pojawieniu się napisu.
  * Ze słuchu – gdy film nie ma żadnych napisów: słucha dźwięku z komputera (Whisper)
    i pokazuje przetłumaczone napisy na dole ekranu (opóźnienie 2–3 s).

Działa w Windows, macOS i Linuksie (szczegóły różnic: README.md). Wszystko lokalnie.
Uruchamianie: AMTSB.bat (Windows), AMTSB.command (macOS), amtsb.sh (Linux)  albo: python amtsb.py
Skróty (działają też w pełnym ekranie): Ctrl+Alt+S – start/stop, Ctrl+Alt+P – pauza.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from amt_core import (  # noqa: E402
    ACCENTS, COLORS, IS_LINUX, IS_MAC, IS_WIN, LANGS, NLLB_CODE, NLLB_MODELS, SR, UI_FONT, UI_SCALES,
    WHISPER_MODELS, AutoWrap, clean_text, fit_geometry, get_translator, guess_lang, hwnd_of, init_ui,
    lang_label, load_settings, load_whisper, make_fonts, make_root, save_settings, set_app_id, set_window_icon,
    setup_logging, ui_scaling)

LOG_FILE = setup_logging("amtsb")

import asyncio  # noqa: E402
import collections  # noqa: E402
import ctypes  # noqa: E402
import difflib  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402

import numpy as np  # noqa: E402

APP = "amtsb"
ASSETS = os.path.join(HERE, "assets")
DEFAULTS = {
    "mode": "screen", "src": "auto", "tgt": "pl", "nllb": "600M", "whisper": "small",
    "region": None, "screen_scale": 100, "font": 30, "show_orig": False, "device": "", "ui_scale": "100%",
}
KEYS_LABEL = "Ctrl+Option+S – start/stop   ·   Ctrl+Option+P – pauza" if IS_MAC else \
    "Ctrl+Alt+S – start/stop   ·   Ctrl+Alt+P – pauza"


# ================================================================= dźwięk (tryb „ze słuchu”)

def to_mono16k(x, channels, rate):
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate == SR:
        return x.astype(np.float32)
    if rate % SR == 0:
        f = rate // SR
        n = len(x) // f * f
        return x[:n].reshape(-1, f).mean(axis=1).astype(np.float32)
    n_out = int(len(x) * SR / rate)
    return np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x).astype(np.float32)


def _use_soundcard():
    return not IS_WIN or os.environ.get("AMTSB_AUDIO") == "soundcard"


def list_devices():
    """[(nazwa, (rodzaj, id))] – najpierw dźwięk z komputera (loopback), potem mikrofony."""
    out = []
    if _use_soundcard():
        import soundcard as sc
        try:
            spk = sc.default_speaker().name
        except Exception:
            spk = ""
        mics = sc.all_microphones(include_loopback=True)
        loops = [m for m in mics if getattr(m, "isloopback", False)]
        loops.sort(key=lambda m: 0 if spk and spk in m.name else 1)
        for m in loops:
            out.append((f"Dźwięk komputera: {m.name.replace('Monitor of ', '')}", ("sc", m.id)))
        for m in mics:
            if not getattr(m, "isloopback", False):
                out.append((f"Mikrofon: {m.name}", ("sc", m.id)))
        return out
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    try:
        try:
            d = p.get_default_wasapi_loopback()
            out.append((f"Dźwięk komputera: {d['name'].replace(' [Loopback]', '')} (domyślne)", ("pa", d["index"])))
        except Exception:
            d = None
        for x in p.get_loopback_device_info_generator():
            if not d or x["index"] != d["index"]:
                out.append((f"Dźwięk komputera: {x['name'].replace(' [Loopback]', '')}", ("pa", x["index"])))
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)["index"]
        for i in range(p.get_device_count()):
            x = p.get_device_info_by_index(i)
            if x["hostApi"] == wasapi and x["maxInputChannels"] > 0 and not x.get("isLoopbackDevice"):
                out.append((f"Mikrofon: {x['name']}", ("pa", i)))
    finally:
        p.terminate()
    return out


class QueueSource:
    """Wspólne: kolejka z kawałkami dźwięku 16 kHz mono."""

    def __init__(self):
        self.q = queue.Queue()
        self.finished = False

    def read(self, timeout=0.2):
        try:
            chunks = [self.q.get(timeout=timeout)]
        except queue.Empty:
            return None
        while True:
            try:
                chunks.append(self.q.get_nowait())
            except queue.Empty:
                break
        return np.concatenate(chunks)


class WasapiSource(QueueSource):
    """Windows: nagrywa z urządzenia WASAPI (loopback = to, co słychać w głośnikach/słuchawkach)."""

    def __init__(self, index):
        super().__init__()
        self.index = index

    def start(self):
        import pyaudiowpatch as pa
        self.pa = pa
        self.p = pa.PyAudio()
        d = self.p.get_default_wasapi_loopback() if self.index is None else self.p.get_device_info_by_index(self.index)
        self.ch = int(d["maxInputChannels"])
        self.rate = int(d["defaultSampleRate"])
        self.stream = self.p.open(format=pa.paFloat32, channels=self.ch, rate=self.rate, input=True,
                                  input_device_index=d["index"], frames_per_buffer=int(self.rate * 0.1),
                                  stream_callback=self._cb)
        self.stream.start_stream()

    def _cb(self, in_data, frame_count, time_info, status):
        self.q.put(to_mono16k(np.frombuffer(in_data, dtype=np.float32), self.ch, self.rate))
        return (None, self.pa.paContinue)

    def stop(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.p.terminate()


class SoundcardSource(QueueSource):
    """macOS / Linux: biblioteka soundcard (Linux: „Monitor of …” = dźwięk komputera z PulseAudio/PipeWire;
    macOS: dźwięk komputera przez wirtualne urządzenie, np. BlackHole)."""
    RATE = 48000

    def __init__(self, dev_id):
        super().__init__()
        self.dev_id = dev_id
        self._stop = threading.Event()

    def start(self):
        import soundcard as sc
        mic = sc.get_microphone(self.dev_id, include_loopback=True)
        ch = min(2, int(getattr(mic, "channels", 2) or 2))

        def run():
            try:
                with mic.recorder(samplerate=self.RATE, channels=ch, blocksize=self.RATE // 10) as rec:
                    while not self._stop.is_set():
                        data = rec.record(numframes=self.RATE // 10)
                        x = data.mean(axis=1) if data.ndim > 1 else data
                        self.q.put(to_mono16k(x.astype(np.float32), 1, self.RATE))
            except Exception:
                traceback.print_exc()
            finally:
                self.finished = True

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._stop.set()


class FileFeedSource(QueueSource):
    """Do testów: podaje plik audio w czasie rzeczywistym, jakby leciał z głośników."""

    def __init__(self, path):
        super().__init__()
        self.path = path
        self._stop = threading.Event()

    def start(self):
        from faster_whisper import decode_audio
        audio = decode_audio(self.path, sampling_rate=SR)

        def feed():
            step = int(0.1 * SR)
            for i in range(0, len(audio), step):
                if self._stop.is_set():
                    break
                self.q.put(audio[i:i + step])
                time.sleep(0.1)
            time.sleep(1.0)
            self.finished = True

        threading.Thread(target=feed, daemon=True).start()

    def stop(self):
        self._stop.set()


def make_source(dev):
    kind, ident = dev
    return SoundcardSource(ident) if kind == "sc" else WasapiSource(ident)


class LiveEngine(threading.Thread):
    """Ze słuchu: wycina z dźwięku kolejne wypowiedzi (VAD), rozpoznaje je i tłumaczy."""
    MAX_SEG = 7.0
    END_SILENCE = 0.35

    def __init__(self, cfg, source, events):
        super().__init__(daemon=True)
        self.cfg, self.source, self.ev = cfg, source, events
        self.stop_evt = threading.Event()
        self.paused = threading.Event()
        self.lang = None if cfg["src"] == "auto" else cfg["src"]
        self.votes = collections.deque(maxlen=5)

    def status(self, text):
        self.ev.put(("status", text))

    def run(self):
        try:
            self._run()
        except Exception as e:
            traceback.print_exc()
            self.ev.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            try:
                self.source.stop()
            except Exception:
                pass
            self.ev.put(("engine_stopped", self))

    def _run(self):
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        self.model = load_whisper(self.cfg["whisper"], self.status)
        self.tr = get_translator(self.cfg["nllb"], self.status) if self.cfg["tgt"] else None
        self.vad = VadOptions(threshold=0.5, min_silence_duration_ms=300, speech_pad_ms=150,
                              min_speech_duration_ms=200)
        self._vad_fn = get_speech_timestamps
        self.source.start()
        self.status("Słucham… Puść film.")
        self.ev.put(("ready",))
        buf = np.zeros(0, np.float32)
        since = 0
        while not self.stop_evt.is_set():
            chunk = self.source.read(0.2)
            if chunk is None:
                if self.source.finished:
                    if len(buf) > SR // 2:
                        self._process(buf, 0.0)
                    break
                continue
            if self.paused.is_set():
                buf = buf[:0]
                continue
            if len(chunk) > 12 * SR:  # komputer nie nadążał – odrzucamy zaległości
                chunk = chunk[-6 * SR:]
                buf = buf[:0]
                self.status("Komputer nie nadąża – wybierz mniejszy model rozpoznawania (np. base).")
            buf = np.concatenate([buf, chunk])
            since += len(chunk)
            if since < int(0.3 * SR):
                continue
            since = 0
            seg, cut = self._pick(buf)
            if seg is not None:
                self._process(seg, (len(buf) - cut) / SR)
            buf = buf[cut:]

    def _pick(self, buf):
        n = len(buf)
        ts = self._vad_fn(buf, self.vad)
        if not ts:
            return None, max(0, n - SR // 2)
        start, end = ts[0]["start"], ts[-1]["end"]
        if n - end >= int(self.END_SILENCE * SR):
            return buf[start:end], end
        if n - start >= self.MAX_SEG * SR:
            cut = None
            for a, b in zip(ts, ts[1:]):
                mid = (a["end"] + b["start"]) // 2
                if mid - start >= 1.5 * SR:
                    cut = mid
            cut = cut or n
            return buf[start:cut], cut
        return None, start

    def _process(self, audio, tail_s):
        t0 = time.time()
        segments, info = self.model.transcribe(
            audio, language=self.lang, beam_size=1, best_of=1, temperature=0.0,
            condition_on_previous_text=False, without_timestamps=True, vad_filter=False)
        texts = []
        for s in segments:
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            t = clean_text(s.text)
            if t:
                texts.append(t)
        text = " ".join(texts).strip()
        src = self.lang or info.language
        if not self.lang:
            self._vote(info.language, info.language_probability)
        if not text:
            return
        trans = None
        tgt = self.cfg["tgt"]
        if self.tr and tgt and src != tgt and src in NLLB_CODE:
            trans = self.tr.translate([text], src, tgt, beam=2)[0]
        self.ev.put(("cue", text, trans, src, tail_s + time.time() - t0))

    def _vote(self, lang, prob):
        if prob < 0.6:
            return
        self.votes.append(lang)
        best, cnt = collections.Counter(self.votes).most_common(1)[0]
        if cnt >= 3:
            self.lang = best
            self.status(f"Wykryty język filmu: {lang_label(best)}")


# ================================================================= OCR – odczyt napisów z obrazu

# litery dozwolone w danym języku – reszta znaków spoza ASCII to zwykle błąd OCR
OCR_LETTERS = {
    "en": "", "de": "äöüßÄÖÜ", "fr": "àâæçéèêëîïôœùûüÿÀÂÇÉÈÊËÎÏÔŒÙÛÜ", "es": "áéíóúñü¡¿ÁÉÍÓÚÑÜ",
    "it": "àèéìíòóùÀÈÉÌÒÙ", "pt": "ãõáàâéêíóôúçÃÕÁÀÂÉÊÍÓÔÚÇ", "pl": "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "cs": "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ", "sk": "áäčďéíĺľňóôŕšťúýžÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ",
    "nl": "éëïöüÉËÏÖÜ", "sv": "åäöÅÄÖé", "no": "æøåÆØÅé", "da": "æøåÆØÅé", "fi": "äöåÄÖÅ",
    "hu": "áéíóöőúüűÁÉÍÓÖŐÚÜŰ", "ro": "ăâîșțşţĂÂÎȘȚŞŢ", "tr": "çğıöşüÇĞİÖŞÜ", "hr": "čćđšžČĆĐŠŽ",
}
TESS_LANG = {"en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "pt": "por", "pl": "pol", "cs": "ces",
             "sk": "slk", "nl": "nld", "sv": "swe", "no": "nor", "da": "dan", "fi": "fin", "hu": "hun", "ro": "ron",
             "tr": "tur", "hr": "hrv", "ru": "rus", "uk": "ukr", "bg": "bul", "el": "ell", "ja": "jpn",
             "ko": "kor", "zh": "chi_sim", "ar": "ara", "he": "heb"}
VISION_LANG = {"en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "it": "it-IT", "pt": "pt-BR",
               "zh": "zh-Hans", "ja": "ja-JP", "ko": "ko-KR", "ru": "ru-RU", "uk": "uk-UA"}


def text_mask(bgra):
    """Piksele wyglądające na tekst napisów: jasne i mało kolorowe (białe) albo żółte."""
    px = bgra[..., :3].astype(np.int16)
    mx, mn = px.max(-1), px.min(-1)
    white = (mx > 190) & (mx - mn < 60)
    yellow = (px[..., 2] > 180) & (px[..., 1] > 160) & (px[..., 0] < 120)
    return white | yellow


class WinOcr:
    """Windows: OCR wbudowany w system (Windows.Media.Ocr) – 10–30 ms na klatkę."""
    variants = ("raw", "bin")

    def __init__(self, lang):
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter
        self._sb, self._dw = SoftwareBitmap, DataWriter
        self._fmt, self._alpha = BitmapPixelFormat.BGRA8, BitmapAlphaMode.PREMULTIPLIED
        eng = None
        regions = {"en": ["en-US", "en-GB"], "de": ["de-DE"], "fr": ["fr-FR"], "es": ["es-ES"], "it": ["it-IT"],
                   "pt": ["pt-PT", "pt-BR"], "ru": ["ru-RU"], "uk": ["uk-UA"], "cs": ["cs-CZ"], "nl": ["nl-NL"]}
        for tag in ([lang] + regions.get(lang, [])) if lang else []:  # najlepiej OCR dla języka napisów
            try:
                if OcrEngine.is_language_supported(Language(tag)):
                    eng = OcrEngine.try_create_from_language(Language(tag))
                    break
            except Exception:
                traceback.print_exc()
        if eng is None:
            eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            langs = list(OcrEngine.available_recognizer_languages)
            if langs:
                eng = OcrEngine.try_create_from_language(langs[0])
        if eng is None:
            raise RuntimeError("W systemie Windows nie ma modułu rozpoznawania tekstu (OCR).")
        self.eng = eng
        self.name = f"OCR Windows ({eng.recognizer_language.language_tag})"
        self.loop = asyncio.new_event_loop()

    def read(self, bgra):
        h, w = bgra.shape[:2]
        dw = self._dw()
        dw.write_bytes(np.ascontiguousarray(bgra).tobytes())
        sb = self._sb.create_copy_from_buffer(dw.detach_buffer(), self._fmt, w, h, self._alpha)

        async def go():
            return await self.eng.recognize_async(sb)

        res = self.loop.run_until_complete(go())
        out = []
        for line in res.lines:
            rects = [wd.bounding_rect for wd in line.words]
            if not rects:
                continue
            x0, y0 = min(r.x for r in rects), min(r.y for r in rects)
            x1 = max(r.x + r.width for r in rects)
            y1 = max(r.y + r.height for r in rects)
            hs = sorted(r.height for r in rects)
            out.append(dict(text=line.text.strip(), x=x0, y=y0, w=x1 - x0, h=y1 - y0, wh=hs[len(hs) // 2]))
        return out


class MacVisionOcr:
    """macOS: Apple Vision (VNRecognizeTextRequest) – wbudowany w system, działa lokalnie."""
    variants = ("raw",)

    def __init__(self, lang):
        try:
            import Quartz
            import Vision
        except ImportError as e:
            raise RuntimeError("Brak bibliotek Apple Vision (pyobjc). Uruchom AMTSB.command jeszcze raz – "
                               "doinstaluje je samo.") from e
        self.Q, self.V = Quartz, Vision
        self.langs = [VISION_LANG[lang]] if lang in VISION_LANG else None
        self.name = "Apple Vision"

    def read(self, bgra):
        Q, V = self.Q, self.V
        h, w = bgra.shape[:2]
        data = np.ascontiguousarray(bgra).tobytes()
        provider = Q.CGDataProviderCreateWithData(None, data, len(data), None)
        img = Q.CGImageCreate(w, h, 8, 32, w * 4, Q.CGColorSpaceCreateDeviceRGB(),
                              Q.kCGBitmapByteOrder32Little | Q.kCGImageAlphaPremultipliedFirst,
                              provider, None, False, Q.kCGRenderingIntentDefault)
        req = V.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(V.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(True)
        if self.langs:
            req.setRecognitionLanguages_(self.langs)
        handler = V.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
        handler.performRequests_error_([req], None)
        out = []
        for obs in req.results() or []:
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            bb = obs.boundingBox()  # 0..1, początek w lewym dolnym rogu
            bw, bh = bb.size.width * w, bb.size.height * h
            out.append(dict(text=str(cands[0].string()).strip(), x=bb.origin.x * w,
                            y=(1 - bb.origin.y - bb.size.height) * h, w=bw, h=bh, wh=bh * 0.75))
        return out


class TesseractOcr:
    """Linux: Tesseract (sudo apt install tesseract-ocr; inne języki: tesseract-ocr-pol itp.)."""
    variants = ("bin",)

    def __init__(self, lang):
        try:
            import pytesseract
        except ImportError as e:
            raise RuntimeError("Brak biblioteki pytesseract. Uruchom amtsb.sh jeszcze raz – doinstaluje ją.") from e
        self.pt = pytesseract
        try:
            avail = set(pytesseract.get_languages(config=""))
        except Exception as e:
            raise RuntimeError("Nie znaleziono programu Tesseract. Zainstaluj go: sudo apt install tesseract-ocr") from e
        want = TESS_LANG.get(lang or "en", "eng")
        self.lang = want if want in avail else ("eng" if "eng" in avail else sorted(avail)[0])
        self.name = f"Tesseract ({self.lang})"

    def read(self, bgra):
        from PIL import Image
        img = Image.fromarray(np.ascontiguousarray(bgra[..., [2, 1, 0]]))
        d = self.pt.image_to_data(img, lang=self.lang, config="--psm 6", output_type=self.pt.Output.DICT)
        lines = {}
        for i, word in enumerate(d["text"]):
            if word.strip() and float(d["conf"][i]) >= 30:
                lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(i)
        out = []
        for idx in lines.values():
            idx.sort(key=lambda i: d["left"][i])
            x0 = min(d["left"][i] for i in idx)
            y0 = min(d["top"][i] for i in idx)
            x1 = max(d["left"][i] + d["width"][i] for i in idx)
            y1 = max(d["top"][i] + d["height"][i] for i in idx)
            hs = sorted(d["height"][i] for i in idx)
            out.append(dict(text=" ".join(d["text"][i] for i in idx), x=x0, y=y0, w=x1 - x0, h=y1 - y0,
                            wh=hs[len(hs) // 2]))
        return out


def make_ocr(lang):
    if IS_WIN:
        return WinOcr(lang)
    if IS_MAC:
        return MacVisionOcr(lang)
    return TesseractOcr(lang)


def _plausible(line, region_h):
    t = line["text"]
    letters = sum(ch.isalpha() for ch in t)
    nonspace = sum(not ch.isspace() for ch in t)
    if letters < 2 or not nonspace or letters / nonspace < 0.55:
        return False
    if re.search(r"\d{1,2}:\d{2}", t):  # czas na pasku odtwarzacza
        return False
    return 8 <= line["h"] <= region_h * 0.45


def _lowest_block(lines):
    """Grupuje linie leżące blisko siebie i podobnej wielkości (linie jednego napisu mają tę samą
    czcionkę) i zwraca najniższą grupę – to zwykle napisy. Drobny tekst z kadru (np. etykieta) odpada."""
    if not lines:
        return []
    lines = sorted(lines, key=lambda l: l["y"])
    blocks, cur = [], [lines[0]]
    for ln in lines[1:]:
        prev = cur[-1]
        similar = min(prev["h"], ln["h"]) / max(1, max(prev["h"], ln["h"])) > 0.6
        if similar and ln["y"] - (prev["y"] + prev["h"]) < 1.3 * max(prev["h"], ln["h"]):
            cur.append(ln)
        else:
            blocks.append(cur)
            cur = [ln]
    blocks.append(cur)
    # napisy to zwykle największy tekst w dolnej części ekranu – drobny tekst (pasek wyszukiwania,
    # etykiety w kadrze, przyciski odtwarzacza) pomijamy, nawet jeśli leży niżej
    biggest = max(l["h"] for l in lines)
    big = [b for b in blocks if max(l["h"] for l in b) >= 0.6 * biggest]
    return big[-1]


def _ocr_score(lines, src):
    allowed = OCR_LETTERS.get(src)
    s = 0
    for ln in lines:
        for ch in ln["text"]:
            if ch.isascii():
                s += 1 if ch.isalnum() else (-2 if ch in ";|_~^`{}<>\\" else 0)
            elif allowed is not None:
                s += 1 if ch in allowed else -3
            else:
                s += 1
    return s


# OCR dla innego języka (np. tylko polski moduł w Windows) wstawia w angielski tekst swoje litery:
# „Ił says” zamiast „It says”. Litery, których nie ma w języku napisów, zamieniamy na podobne.
OCR_LOOKALIKE = {"ł": "t", "Ł": "L", "ś": "s", "Ś": "S", "ć": "c", "Ć": "C", "ń": "n", "Ń": "N", "ą": "a",
                 "ę": "e", "ż": "z", "ź": "z", "Ż": "Z", "Ź": "Z", "ó": "o", "Ó": "O", "Ț": "T", "ț": "t",
                 "Ș": "S", "ș": "s", "Ă": "A", "ă": "a", "î": "i", "Î": "I", "ĺ": "l", "ľ": "l"}


def _fix_ocr(t, src="en"):
    allowed = OCR_LETTERS.get(src)
    if allowed is not None:
        t = "".join(OCR_LOOKALIKE.get(ch, ch) if ch not in allowed else ch for ch in t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\bl(?=['’](m|ll|d|ve)\b)", "I", t)   # l'm -> I'm
    t = re.sub(r"(?<=\s)l(?=\s)", "I", t)               # " l " -> " I "
    t = t.replace("|", "I")
    if src == "en":
        t = re.sub(r"(?<![\w'])(II|1)(?= [a-z])", "I", t)  # "II am" / "-1 come" -> "I am" / "-I come"
    t = re.sub(r"(?:(?<=\s)|^)[.,']?[rJ](?=\s|$)", "♪", t)    # nutka z piosenek czytana jako ".r" / "J"
    t = re.sub(r";$", ".", t)
    return t


def _norm(t):
    return re.sub(r"[^\w]+", " ", (t or "").lower()).strip()


def taskbar_rect():
    """Windows: prostokąt paska zadań, jeśli jest widoczny (gdy aktywne okno nie zajmuje całego ekranu)."""
    if not IS_WIN:
        return None
    try:
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        r = wt.RECT()
        fg = u.GetForegroundWindow()
        if fg and u.GetWindowRect(fg, ctypes.byref(r)):
            if r.left <= 0 and r.top <= 0 and r.right >= u.GetSystemMetrics(0) and r.bottom >= u.GetSystemMetrics(1):
                return None  # film w pełnym ekranie – pasek zadań jest schowany
        tb = u.FindWindowW("Shell_TrayWnd", None)
        if tb and u.IsWindowVisible(tb) and u.GetWindowRect(tb, ctypes.byref(r)):
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        traceback.print_exc()
    return None


class ScreenEngine(threading.Thread):
    """Co 0,1 s robi zrzut obszaru napisów, przy zmianie odczytuje tekst (OCR),
    tłumaczy go i wysyła do paska z tłumaczeniem."""
    INTERVAL = 0.1

    def __init__(self, cfg, region, events):
        super().__init__(daemon=True)
        self.cfg, self.region, self.ev = cfg, [int(v) for v in region], events
        self.stop_evt = threading.Event()
        self.paused = threading.Event()
        self.src = None if cfg["src"] == "auto" else cfg["src"]
        self.guesses = collections.deque(maxlen=15)
        self.cache = {}
        self._mask = []          # prostokąty (współrzędne ekranu) do zamazania przed OCR – nasz własny pasek
        self._mask_lock = threading.Lock()

    def set_mask(self, rects):
        with self._mask_lock:
            self._mask = list(rects)

    def status(self, text):
        self.ev.put(("status", text))

    def run(self):
        try:
            self._run()
        except Exception as e:
            traceback.print_exc()
            self.ev.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            self.ev.put(("engine_stopped", self))

    def _run(self):
        import mss
        self.tr = get_translator(self.cfg["nllb"], self.status)
        self.ocr = make_ocr(self.src or "en")
        x, y, w, h = self.region
        mon = {"left": x, "top": y, "width": w, "height": h}
        self.status(f"Szukam napisów na ekranie ({self.ocr.name})… Puść film z napisami.")
        self.ev.put(("ready",))
        last_sig, last_key, last_box, shown = None, None, None, False
        box_ref = None  # miejsce aktualnego napisu na zrzucie i jego wygląd (maska jasnych pikseli)
        pending = None  # nowy tekst czekający na potwierdzenie kolejnym odczytem (napisy bywają animowane)
        empty, misses, last_ocr = 0, 0, 0.0

        def clear():
            nonlocal shown, last_key, last_box, box_ref, pending
            if shown:
                self.ev.put(("screen_clear",))
            shown, last_key, last_box, box_ref, pending = False, None, None, None, None

        with (getattr(mss, "MSS", None) or mss.mss)() as sct:
            while not self.stop_evt.is_set():
                t0 = time.time()
                if self.paused.is_set():
                    clear()
                    time.sleep(0.2)
                    continue
                img = np.array(sct.grab(mon))
                k = img.shape[1] / w  # piksele zrzutu na punkt ekranu (macOS Retina: 2)
                with self._mask_lock:
                    hide = list(self._mask)        # nasz własny pasek (tryb „nad oryginałem”)
                tb = taskbar_rect()
                if tb:
                    hide.append(tb)                # pasek zadań Windows, gdy film nie jest na pełnym ekranie
                for mx, my, mw, mh in hide:
                    x0, y0 = max(0, int((mx - x) * k)), max(0, int((my - y) * k))
                    x1, y1 = int((mx + mw - x) * k), int((my + mh - y) * k)
                    if x1 > 0 and y1 > 0:
                        img[y0:y1, x0:x1, :3] = 0
                # napis już jest na ekranie: dopóki jego miejsce wygląda tak samo, nie czytamy go od nowa
                # (inaczej ruch obrazu za napisem powoduje ciągłe ponowne odczyty i „mruganie” tłumaczenia)
                force = False
                if shown and box_ref is not None:
                    bx0, by0, bx1, by1, ref = box_ref
                    cur = text_mask(img[by0:by1:2, bx0:bx1:2])
                    same_look = cur.shape == ref.shape and (
                        np.count_nonzero(cur ^ ref) / max(1, np.count_nonzero(cur | ref))) < 0.15
                    if same_look and t0 - last_ocr < 3.0 and not pending:
                        time.sleep(max(0.0, self.INTERVAL - (time.time() - t0)))
                        continue
                    force = True
                force = force or pending is not None
                sig = text_mask(img[::3, ::3])
                if np.count_nonzero(sig) < 12:           # brak jasnego tekstu w obszarze
                    empty += 1
                    if empty >= 2:
                        clear()
                    last_sig = None
                else:
                    empty = 0
                    changed = last_sig is None or (
                        np.count_nonzero(sig ^ last_sig) / max(1, np.count_nonzero(sig | last_sig)) > 0.18)
                    if force or changed or (not shown and t0 - last_ocr > 0.4):
                        last_sig, last_ocr = sig, t0
                        found = self._read(img)
                        if not found:
                            misses += 1
                            if misses >= 2:
                                clear()
                        else:
                            misses = 0
                            lines, box, line_h = found
                            key = _norm(" ".join(lines))
                            m = int(line_h * 0.3) + 2
                            bx0, by0 = max(0, int(box[0]) - m), max(0, int(box[1]) - m)
                            bx1, by1 = int(box[0] + box[2]) + m, int(box[1] + box[3]) + m
                            box_ref = (bx0, by0, bx1, by1, text_mask(img[by0:by1:2, bx0:bx1:2]))
                            if self._same(key, last_key, box, last_box, line_h):
                                pending = None              # wciąż ten sam napis co na ekranie
                            elif pending and difflib.SequenceMatcher(None, key, pending).ratio() > 0.85:
                                pending = None              # drugi odczyt potwierdził nowy napis – pokaż
                                trans = self._translate(lines)
                                last_key, last_box, shown = key, box, True
                                gbox = (x + box[0] / k, y + box[1] / k, box[2] / k, box[3] / k)
                                self.ev.put(("screen_sub", lines, trans, gbox, line_h / k, self.region))
                            else:
                                pending = key               # nowy tekst – poczekaj na potwierdzenie
                time.sleep(max(0.0, self.INTERVAL - (time.time() - t0)))

    @staticmethod
    def _same(key, last_key, box, last_box, line_h):
        """Czy to ten sam napis odczytany trochę inaczej (szum OCR), a nie nowy napis."""
        if not last_key:
            return False
        r = difflib.SequenceMatcher(None, key, last_key).ratio()
        if r > 0.9:
            return True
        if last_box is None:
            return False
        near = abs(box[1] - last_box[1]) < 0.6 * line_h and abs(box[3] - last_box[3]) < 0.6 * line_h
        similar_len = abs(len(key) - len(last_key)) <= 0.2 * max(len(key), len(last_key))
        return near and similar_len and r > 0.75

    def _read(self, img):
        H, W = img.shape[:2]
        mask = text_mask(img)
        binimg = np.full_like(img, 255)
        binimg[mask, :3] = 0                       # czarny tekst na białym tle – czytelniej dla OCR
        best = None
        src = self.src or "en"
        for name in self.ocr.variants:
            variant = img if name == "raw" else binimg
            lines = self.ocr.read(variant)
            for l in lines:
                l["text"] = _fix_ocr(l["text"], src)
            block = _lowest_block([l for l in lines if _plausible(l, H)])
            if not block:
                continue
            # napisy to 1–3 linie (wyśrodkowane albo, jak np. w South Park Studios, wyrównane do lewej);
            # więcej linii to raczej menu albo strona www
            bx0 = int(min(l["x"] for l in block))
            bx1 = int(max(l["x"] + l["w"] for l in block))
            by0 = int(min(l["y"] for l in block))
            by1 = int(max(l["y"] + l["h"] for l in block))
            if len(block) > 3:
                continue
            # napisy filmowe = jasne litery na ciemnym tle; ciemny tekst na jasnym tle to okno/strona
            if mask[max(0, by0):by1, max(0, bx0):bx1].mean() > 0.6:
                continue
            allowed = OCR_LETTERS.get(self.src or "en")
            if allowed is not None and sum(
                    ch.isalpha() and not ch.isascii() and ch not in allowed for l in block for ch in l["text"]) >= 3:
                continue
            sc = _ocr_score(block, self.src or "en")
            if best is None or sc > best[0]:
                best = (sc, block)
        if not best:
            return None
        block = best[1]
        x0 = min(l["x"] for l in block)
        y0 = min(l["y"] for l in block)
        x1 = max(l["x"] + l["w"] for l in block)
        y1 = max(l["y"] + l["h"] for l in block)
        hs = sorted(l["wh"] for l in block)
        return [l["text"] for l in block], (x0, y0, x1 - x0, y1 - y0), hs[len(hs) // 2]

    def _translate(self, lines):
        tgt = self.cfg["tgt"]
        if self.src:
            src = self.src
        else:
            self.guesses.append(guess_lang(lines, default=""))
            votes = collections.Counter(g for g in self.guesses if g)
            src = votes.most_common(1)[0][0] if votes else "en"
        dialog = len(lines) > 1 and all(l.startswith(("-", "–")) for l in lines)
        units = [l.lstrip("-– ").strip() for l in lines] if dialog else [" ".join(lines)]
        music = any("♪" in u for u in units)          # piosenka: nutki zostawiamy, tłumaczymy sam tekst
        units = [re.sub(r"\s*♪\s*", " ", u).strip() for u in units]
        if src != tgt and src in NLLB_CODE:
            if len(self.cache) > 3000:
                self.cache.clear()
            todo = [u for u in units if u and u not in self.cache]
            if todo:
                for u, r in zip(todo, self.tr.translate(todo, src, tgt, beam=2)):
                    self.cache[u] = r
            units = [self.cache.get(u, u) for u in units]
        if music:
            units = [f"♪ {u} ♪" if u else "♪" for u in units]
        return ["- " + u for u in units] if dialog else units


# ================================================================= skróty klawiszowe

class Hotkeys:
    """Globalne skróty Ctrl+Alt+S (start/stop) i Ctrl+Alt+P (pauza) – działają też w pełnym ekranie.
    Windows: RegisterHotKey; macOS/Linux: pynput (macOS wymaga zgody na „Dostępność”)."""

    def __init__(self, events):
        self.ev = events
        self.ok = False
        self._tid = None
        self._listener = None

    def start(self):
        if IS_WIN:
            threading.Thread(target=self._win_loop, daemon=True).start()
        else:
            try:
                from pynput import keyboard
                self._listener = keyboard.GlobalHotKeys({
                    "<ctrl>+<alt>+s": lambda: self.ev.put(("hotkey", 1)),
                    "<ctrl>+<alt>+p": lambda: self.ev.put(("hotkey", 2))})
                self._listener.start()
                self.ok = True
            except Exception:
                traceback.print_exc()

    def _win_loop(self):
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        self._tid = threading.get_native_id()
        got = [user32.RegisterHotKey(None, hid, 0x0001 | 0x0002 | 0x4000, vk)  # Alt | Ctrl | bez powtórzeń
               for hid, vk in ((1, ord("S")), (2, ord("P")))]
        self.ok = all(got)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                self.ev.put(("hotkey", int(msg.wParam)))
        for hid in (1, 2):
            user32.UnregisterHotKey(None, hid)

    def stop(self):
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass


# ================================================================= okna nad filmem

import tkinter as tk  # noqa: E402
import tkinter.font as tkfont  # noqa: E402
from tkinter import messagebox  # noqa: E402

import customtkinter as ctk  # noqa: E402

from amt_core import Dropdown  # noqa: E402

ACCENT, ACCENT_HOVER = ACCENTS["amtsb"]


def _ns_window(win):
    """macOS: obiekt NSWindow dla okna Tk (szukany po unikalnym tytule)."""
    try:
        from AppKit import NSApp
    except ImportError:
        return None
    try:
        win.update_idletasks()
        title = win.title()
        for w in NSApp.windows():
            if str(w.title()) == title:
                return w
    except Exception:
        traceback.print_exc()
    return None


def set_capture_excluded(win, on):
    """Okno niewidoczne dla zrzutów ekranu – OCR widzi to, co jest pod nim (oryginalne napisy)."""
    try:
        if IS_WIN:
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd_of(win), 0x11 if on else 0)  # WDA_EXCLUDEFROMCAPTURE
        elif IS_MAC:
            w = _ns_window(win)
            if w is not None:
                w.setSharingType_(0 if on else 1)  # NSWindowSharingNone / ReadOnly
    except Exception:
        traceback.print_exc()


def make_overlay_window(win, click_through=True):
    """Okno nad filmem: kliknięcia przechodzą do odtwarzacza, widoczne także nad pełnym ekranem."""
    try:
        if IS_WIN and click_through:
            u = ctypes.windll.user32
            h = hwnd_of(win)
            u.SetWindowLongW(h, -20, u.GetWindowLongW(h, -20) | 0x80000 | 0x20 | 0x80 | 0x08000000)
        elif IS_MAC:
            w = _ns_window(win)
            if w is not None:
                w.setLevel_(1000)                         # nad aplikacjami w pełnym ekranie
                w.setCollectionBehavior_(1 | 16 | 256)    # wszystkie biurka, stały, pomocniczy pełnego ekranu
                if click_through:
                    w.setIgnoresMouseEvents_(True)
    except Exception:
        traceback.print_exc()


def capture_exclusion_works(root):
    """Sprawdza, czy system pozwala ukryć okno przed zrzutem ekranu (Windows 10 2004+, macOS).
    Jeśli nie (np. Linux), tłumaczenie pokaże się nad oryginalnymi napisami zamiast je zakrywać."""
    if os.environ.get("AMTSB_NO_EXCLUDE") == "1" or IS_LINUX:
        return False
    try:
        import mss
        t = tk.Toplevel(root)
        t.title(f"AMTSB-test-{id(t)}")
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg="#ff00ff")
        x, y = 40, 40
        t.geometry(f"160x100+{x}+{y}")
        t.update()
        set_capture_excluded(t, True)
        t.update()
        time.sleep(0.3)
        t.update()
        with (getattr(mss, "MSS", None) or mss.mss)() as sct:
            img = np.array(sct.grab({"left": x + 20, "top": y + 20, "width": 100, "height": 50}))
        t.destroy()
        magenta = (img[..., 2] > 230) & (img[..., 1] < 40) & (img[..., 0] > 230)
        return magenta.mean() < 0.3
    except Exception:
        traceback.print_exc()
        return False


def default_region(root):
    """Dolne 45% ekranu, aż do samej krawędzi (niektóre odtwarzacze pokazują drugą linię przy samym dole)."""
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    top = int(sh * 0.55)
    return [int(sw * 0.03), top, int(sw * 0.94), sh - top]


class CoverBox(tk.Toplevel):
    """Czarny pasek z tłumaczeniem. mode='cover' – zakrywa oryginał (okno ukryte przed zrzutem);
    mode='above' – leży tuż nad oryginałem (gdy systemu nie da się poprosić o ukrycie okna)."""

    def __init__(self, master, scale=1.0, mode="cover", debug=False, on_rect=None):
        super().__init__(master)
        self.title(f"AMTSB-cover-{id(self)}")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg="#000000")
        self.attributes("-alpha", 0.0)
        self.geometry("10x10+0+0")
        self.c = tk.Canvas(self, bg="#000000", highlightthickness=0, bd=0)
        self.c.pack(fill="both", expand=True)
        self.scale, self.mode, self.debug, self.on_rect = scale, mode, debug, on_rect
        self.fonts = {}
        self.visible = False
        self.update()
        if mode == "cover":
            set_capture_excluded(self, True)
        make_overlay_window(self)
        self.after(1000, self._keep_on_top)

    def _font(self, px):
        if px not in self.fonts:
            self.fonts[px] = tkfont.Font(family=UI_FONT, size=-px, weight="bold")
        return self.fonts[px]

    @staticmethod
    def _wrap(text, font, maxw):
        out, cur = [], ""
        for word in text.split():
            cand = f"{cur} {word}".strip()
            if cur and font.measure(cand) > maxw:
                out.append(cur)
                cur = word
            else:
                cur = cand
        if cur:
            out.append(cur)
        return out or [""]

    def _wrap_balanced(self, text, font, maxw):
        """Linie podobnej długości (bez samotnego słowa w ostatniej linii)."""
        lines = self._wrap(text, font, maxw)
        if len(lines) < 2:
            return lines
        lo, hi = 50, maxw
        while hi - lo > 8:
            mid = (lo + hi) // 2
            if len(self._wrap(text, font, mid)) <= len(lines):
                hi = mid
            else:
                lo = mid
        return self._wrap(text, font, hi)

    def show(self, box, line_h, lines, region):
        x, y, w, h = box
        rw = region[2]
        px0 = max(14, min(130, int(line_h * 1.3 * self.scale)))
        n_orig = max(1, round(h / max(1.0, line_h * 1.35)))
        px = px0
        while True:
            f = self._font(px)
            pad_x = int(px * 0.45)
            out = []
            for ln in lines:
                out += self._wrap_balanced(ln, f, rw - 2 * pad_x)
            if len(out) <= max(n_orig + 1, len(lines)) or px <= px0 * 0.65:
                break
            px = int(px * 0.9)
        ls = f.metrics("linespace")
        pad_y = int(px * 0.2)
        ox, oy = int(line_h * 0.5), int(line_h * 0.45)  # zapas na obwódkę i cień oryginału
        tw = max(f.measure(o) for o in out) + 2 * pad_x
        th = len(out) * ls + 2 * pad_y
        if self.mode == "cover":
            bw, bh = int(max(tw, w + 2 * ox)), int(max(th, h + 2 * oy))
            top = int(y + h + oy - bh)
        else:
            bw, bh = int(tw), int(th)
            top = int(y - oy - line_h * 0.3 - bh)
        left = int(x + w / 2 - bw / 2)
        self.geometry(f"{bw}x{bh}+{left}+{top}")
        self.c.delete("all")
        y0 = (bh - len(out) * ls) / 2
        for i, o in enumerate(out):
            self.c.create_text(bw / 2, y0 + i * ls, text=o, font=f, fill="#ffffff", anchor="n")
        if not self.visible:
            self.attributes("-alpha", 1.0)
            self.visible = True
        self.lift()
        if self.on_rect:
            self.on_rect([(left, top, bw, bh)])
        if self.debug:
            print(f"COVER t={time.time():.2f} {left} {top} {bw} {bh} orig={box} | {' / '.join(out)}", flush=True)

    def hide(self):
        if self.visible:
            self.attributes("-alpha", 0.0)
            self.visible = False
            if self.on_rect:
                self.on_rect([])
            if self.debug:
                print(f"COVER t={time.time():.2f} hide", flush=True)

    def _keep_on_top(self):
        try:
            if self.visible:
                self.lift()
                self.attributes("-topmost", True)
            self.after(1000, self._keep_on_top)
        except tk.TclError:
            pass


class Overlay(tk.Toplevel):
    """Tryb ze słuchu: napisy na dole ekranu, zawsze na wierzchu.
    Windows/macOS: przezroczyste tło; Linux: półprzezroczysty ciemny pas."""
    KEY = "#010203"

    def __init__(self, master, font_size, show_orig, on_pause, on_stop, on_change):
        super().__init__(master)
        self.title(f"AMTSB-subs-{id(self)}")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        bg = self.KEY
        if IS_WIN:
            self.configure(bg=bg)
            self.attributes("-transparentcolor", bg)
        elif IS_MAC:
            bg = "systemTransparent"
            self.configure(bg=bg)
            self.attributes("-transparent", True)
        else:
            bg = "#000000"
            self.configure(bg=bg)
            self.attributes("-alpha", 0.8)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.W, self.H = int(sw * 0.86), int(sh * 0.36)
        self.c = tk.Canvas(self, width=self.W, height=self.H, bg=bg, highlightthickness=0, bd=0)
        self.c.pack(fill="both", expand=True)
        self.reset_pos()
        self.font_size = font_size
        self.show_orig = tk.BooleanVar(value=show_orig)
        self.on_change = on_change
        self.cues = []
        self.status_text = ""
        self._shown = None
        self.c.bind("<ButtonPress-1>", self._press)
        self.c.bind("<B1-Motion>", self._drag)
        self.c.bind("<MouseWheel>", lambda e: self.change_size(2 if e.delta > 0 else -2))
        self.c.bind("<Button-4>", lambda e: self.change_size(2))   # Linux: kółko myszy
        self.c.bind("<Button-5>", lambda e: self.change_size(-2))
        self.c.bind("<Button-2>" if IS_MAC else "<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Pauza / wznów", command=on_pause)
        m.add_command(label="Większe napisy (kółko myszy)", command=lambda: self.change_size(2))
        m.add_command(label="Mniejsze napisy", command=lambda: self.change_size(-2))
        m.add_checkbutton(label="Pokazuj też oryginał", variable=self.show_orig, command=self._orig_toggled)
        m.add_command(label="Przenieś na dół ekranu", command=self.reset_pos)
        m.add_separator()
        m.add_command(label="Zakończ", command=on_stop)
        self.menu = m
        self.update()
        make_overlay_window(self, click_through=False)
        self.after(1000, self._keep_on_top)
        self.after(250, self._tick)
        self.redraw()

    def _press(self, e):
        self._dx, self._dy = e.x_root - self.winfo_x(), e.y_root - self.winfo_y()

    def _drag(self, e):
        self.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def reset_pos(self):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{self.W}x{self.H}+{(sw - self.W) // 2}+{sh - self.H - int(sh * 0.05)}")

    def change_size(self, d):
        self.font_size = max(12, min(80, self.font_size + d))
        self.on_change(font=self.font_size)
        self.redraw()

    def _orig_toggled(self):
        self.on_change(show_orig=self.show_orig.get())
        self.redraw()

    def _keep_on_top(self):
        try:
            self.lift()
            self.attributes("-topmost", True)
            self.after(1000, self._keep_on_top)
        except tk.TclError:
            pass

    def set_status(self, text):
        self.status_text = text
        self.redraw()

    def add_cue(self, orig, trans):
        main = trans or orig
        self.cues.append(dict(orig=orig, trans=trans, until=time.time() + min(10.0, max(3.5, 2.5 + len(main) / 14))))
        self.cues = self.cues[-2:]
        self.redraw()

    def _tick(self):
        try:
            now = time.time()
            if tuple(id(c) for c in self.cues if c["until"] > now) != self._shown:
                self.redraw()
            self.after(250, self._tick)
        except tk.TclError:
            pass

    def _text(self, x, y, text, size, fill, weight, tag):
        font = (UI_FONT, size, weight)
        w = self.W - 60
        r = max(1, round(size / 13))
        for dx in (-r, 0, r):
            for dy in (-r, 0, r):
                if dx or dy:
                    self.c.create_text(x + dx, y + dy, text=text, font=font, fill="#000000", width=w,
                                       justify="center", anchor="s", tags=tag)
        it = self.c.create_text(x, y, text=text, font=font, fill=fill, width=w, justify="center",
                                anchor="s", tags=tag)
        return self.c.bbox(it)

    def _block(self, y, cue, newest, tag):
        size = self.font_size
        while True:
            self.c.delete(tag)
            yy = y
            if self.show_orig.get() and cue["trans"] and cue["orig"]:
                bb = self._text(self.W // 2, yy, cue["orig"], max(11, int(size * 0.6)), "#ffe7a0", "normal", tag)
                yy = bb[1] - 2
            bb = self._text(self.W // 2, yy, cue["trans"] or cue["orig"], size,
                            "#ffffff" if newest else "#cfcfcf", "bold", tag)
            if bb[1] >= 0 or size <= 14 or not newest:
                return bb[1]
            size -= 2

    def redraw(self):
        self.c.delete("all")
        now = time.time()
        cues = [c for c in self.cues if c["until"] > now]
        self._shown = tuple(id(c) for c in cues)
        y = self.H - 6
        if not cues:
            if self.status_text:
                self._text(self.W // 2, y, self.status_text, max(11, self.font_size // 2), "#e8e8e8", "normal", "st")
            return
        for i, cue in enumerate(reversed(cues)):
            tag = f"cue{i}"
            top = self._block(y, cue, i == 0, tag)
            if top < 0 and i > 0:
                self.c.delete(tag)
                break
            y = top - 12


class RegionPicker(tk.Toplevel):
    """Przyciemnia ekran; użytkownik przeciąga prostokąt w miejscu napisów."""

    def __init__(self, master, current, on_done):
        super().__init__(master)
        self.on_done = on_done
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.45)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{sw}x{sh}+0+0")
        self.c = tk.Canvas(self, bg="#000000", highlightthickness=0, cursor="crosshair")
        self.c.pack(fill="both", expand=True)
        s = ui_scaling(master)
        self.c.create_text(sw // 2, sh // 3, fill="#ffffff", justify="center", font=(UI_FONT, -int(26 * s), "bold"),
                           text="Przeciągnij myszką prostokąt tam, gdzie w filmie pojawiają się napisy\n"
                                "(najlepiej trochę większy niż same napisy).\n\nEsc = anuluj")
        if current:
            x, y, w, h = current
            self.c.create_rectangle(x, y, x + w, y + h, outline="#ffcc00", width=3, dash=(8, 6))
        self.rect = None
        self.c.bind("<ButtonPress-1>", self._press)
        self.c.bind("<B1-Motion>", self._drag)
        self.c.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(50, self._grab)

    def _grab(self):
        try:
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass

    def _press(self, e):
        self.x0, self.y0 = e.x, e.y
        if self.rect:
            self.c.delete(self.rect)
        self.rect = self.c.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3030", width=4)

    def _drag(self, e):
        if self.rect:
            self.c.coords(self.rect, self.x0, self.y0, e.x, e.y)

    def _release(self, e):
        x0, x1 = sorted((self.x0, e.x))
        y0, y1 = sorted((self.y0, e.y))
        if x1 - x0 >= 60 and y1 - y0 >= 30:
            ox, oy = self.winfo_rootx(), self.winfo_rooty()
            self.destroy()
            self.on_done([ox + x0, oy + y0, x1 - x0, y1 - y0])


def flash_region(master, region, ms=1600):
    """Na chwilę pokazuje ramkę obszaru napisów (4 cienkie okna – działa na każdym systemie)."""
    x, y, w, h = region
    t = max(4, int(4 * ui_scaling(master)))
    wins = []
    for gx, gy, gw, gh in ((x, y, w, t), (x, y + h - t, w, t), (x, y, t, h), (x + w - t, y, t, h)):
        e = tk.Toplevel(master)
        e.overrideredirect(True)
        e.attributes("-topmost", True)
        e.configure(bg=COLORS["warn"])
        e.geometry(f"{gw}x{gh}+{gx}+{gy}")
        wins.append(e)
    master.after(ms, lambda: [e.destroy() for e in wins])


# ================================================================= główne okno

def card(master, **kw):
    return ctk.CTkFrame(master, fg_color=COLORS["card"], corner_radius=14, border_width=1,
                        border_color=COLORS["card_border"], **kw)


class AMTSBApp:
    MODES = [("screen", "Napisy z ekranu",
              "Film ma napisy w innym języku – zakryję je tłumaczeniem, ok. 0,5 s po pojawieniu się napisu."),
             ("speech", "Mowę ze słuchu",
              "Film nie ma żadnych napisów – słucham dźwięku i pokazuję napisy (opóźnienie 2–3 s).")]

    def __init__(self, root, args):
        self.root, self.args = root, args
        self.s = load_settings(APP, DEFAULTS)
        if args.get("screen_region"):
            self.s["region"] = args["screen_region"]
            self.s["mode"] = "screen"
        if args.get("feed"):
            self.s["mode"] = "speech"
        self.events = queue.Queue()
        self.engine = None
        self.overlay = None
        self.devices = []
        self.f = make_fonts()
        self.wrap = AutoWrap()
        root.title("AMTSB – Automatic Movie Translator SuBtitles")
        set_window_icon(root, os.path.join(ASSETS, "amtsb.ico"), os.path.join(ASSETS, "amtsb_256.png"))
        fit_geometry(root, 1100, 660)
        root.minsize(900, 560)
        self._build()
        self.hotkeys = Hotkeys(self.events)
        self.hotkeys.start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._poll)
        root.after(1200, self._hotkey_check)
        if args.get("screen_region") or args.get("feed"):
            root.after(400, self.toggle)
        if args.get("quit_after"):
            root.after(int(args["quit_after"] * 1000), self._on_close)

    # ------------------------------------------------------------- budowa okna
    def _label(self, master, text="", font="body", color="text", **kw):
        return ctk.CTkLabel(master, text=text, font=self.f[font], text_color=COLORS[color], **kw)

    def _menu(self, master, opts, current, width, command=None):
        names = [o[1] for o in opts]
        keys = [o[0] for o in opts]
        var = tk.StringVar(value=names[keys.index(current)] if current in keys else (names[0] if names else ""))
        m = Dropdown(master, values=names, variable=var, width=width, command=command, accent=ACCENT)
        m._keys, m._names, m._var = keys, names, var
        return m

    @staticmethod
    def _key(m):
        return m._keys[m._names.index(m._var.get())] if m._var.get() in m._names else None

    def _button(self, master, text, command, width=110, accent=False, **kw):
        return ctk.CTkButton(master, text=text, command=command, width=width, height=36, font=self.f["small"],
                             fg_color=ACCENT if accent else COLORS["input"],
                             hover_color=ACCENT_HOVER if accent else COLORS["input_hover"], **kw)

    def _build(self):
        r = self.root
        r.grid_columnconfigure(0, weight=1, uniform="col")
        r.grid_columnconfigure(1, weight=1, uniform="col")
        r.grid_rowconfigure(1, weight=1)
        r.grid_propagate(False)

        head = ctk.CTkFrame(r, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=26, pady=(14, 4))
        try:
            from PIL import Image
            self.logo = ctk.CTkImage(Image.open(os.path.join(ASSETS, "amtsb_256.png")), size=(54, 54))
            ctk.CTkLabel(head, text="", image=self.logo).pack(side="left", padx=(0, 16))
        except Exception:
            traceback.print_exc()
        t = ctk.CTkFrame(head, fg_color="transparent")
        t.pack(side="left")
        self._label(t, "AMTSB", "title").pack(anchor="w")
        self._label(t, "Automatic Movie Translator SuBtitles  ·  tłumaczy napisy filmu na żywo, na Twoim komputerze",
                    "small", "muted").pack(anchor="w")
        ctk.CTkButton(head, text="Ustawienia", width=120, height=36, font=self.f["body"], fg_color=COLORS["input"],
                      hover_color=COLORS["input_hover"], border_width=1, border_color=COLORS["card_border"],
                      command=self._open_settings).pack(side="right")

        left = ctk.CTkFrame(r, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=(26, 10), pady=(8, 20))
        right = ctk.CTkFrame(r, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew", padx=(10, 26), pady=(8, 20))

        # --- stan + start
        st = card(left)
        st.pack(fill="x")
        st.grid_columnconfigure(1, weight=1)
        s = ui_scaling(r)
        self.dot = tk.Canvas(st, width=int(22 * s), height=int(22 * s), bg=COLORS["card"], highlightthickness=0)
        self.dot.grid(row=0, column=0, padx=(20, 12), pady=(20, 0), sticky="n")
        self.v_state = tk.StringVar(value="Wyłączone")
        self._label(st, "", "big", textvariable=self.v_state).grid(row=0, column=1, sticky="w", pady=(16, 0))
        self.v_status = tk.StringVar(value="Kliknij Start, a potem puść film.")
        self.wrap(ctk.CTkLabel(st, textvariable=self.v_status, font=self.f["small"], text_color=COLORS["muted"],
                               anchor="w", justify="left"), st, 80).grid(row=1, column=1, sticky="w", padx=(0, 16))
        bf = ctk.CTkFrame(st, fg_color="transparent")
        bf.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(14, 6))
        self.b_start = ctk.CTkButton(bf, text="▶  Start", height=48, font=self.f["button"], fg_color=ACCENT,
                                     hover_color=ACCENT_HOVER, corner_radius=12, command=self.toggle)
        self.b_start.pack(side="left", fill="x", expand=True)
        self.b_pause = ctk.CTkButton(bf, text="Pauza", width=120, height=48, font=self.f["button"],
                                     fg_color=COLORS["input"], hover_color=COLORS["input_hover"], corner_radius=12,
                                     command=self.toggle_pause, state="disabled")
        self.b_pause.pack(side="left", padx=(10, 0))
        self.v_keys = tk.StringVar(value="Skróty: " + KEYS_LABEL)
        self._label(st, "", "tiny", "faint", textvariable=self.v_keys).grid(row=3, column=0, columnspan=2,
                                                                          sticky="w", padx=20, pady=(0, 16))
        self._set_dot("off")

        # --- tryb
        md = card(left)
        md.pack(fill="x", pady=(14, 0))
        md.grid_columnconfigure(0, weight=1)
        self._label(md, "Co tłumaczyć", "h2").grid(row=0, column=0, sticky="w", padx=20, pady=(16, 8))
        self.v_mode = tk.StringVar(value=self.s.get("mode", "screen"))
        self.v_mode_btn = tk.StringVar()
        self.seg_mode = ctk.CTkSegmentedButton(
            md, values=[t for _, t, _ in self.MODES], variable=self.v_mode_btn, height=40, font=self.f["body"],
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER, unselected_color=COLORS["input"],
            unselected_hover_color=COLORS["input_hover"],
            command=lambda t: (self.v_mode.set(next(k for k, tt, _ in self.MODES if tt == t)), self._mode_changed()))
        self.seg_mode.grid(row=1, column=0, sticky="ew", padx=20)
        self.v_mode_desc = tk.StringVar()
        self.wrap(ctk.CTkLabel(md, textvariable=self.v_mode_desc, font=self.f["small"], text_color=COLORS["muted"],
                               anchor="w", justify="left"), md, 50).grid(row=2, column=0, sticky="w", padx=20,
                                                                         pady=(8, 16))

        # --- języki
        lg = card(left)
        lg.pack(fill="x", pady=(14, 0))
        lg.grid_columnconfigure((0, 1), weight=1)
        self._label(lg, "Języki", "h2").grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(16, 6))
        self._label(lg, "Język filmu", "small", "muted").grid(row=1, column=0, sticky="w", padx=(20, 6))
        self._label(lg, "Tłumacz na", "small", "muted").grid(row=1, column=1, sticky="w", padx=(6, 20))
        src_opts = [("auto", "wykryj automatycznie")] + [(c, n) for n, c, _ in LANGS if c != "pl"] + [("pl", "polski")]
        self.m_src = self._menu(lg, src_opts, self.s["src"], 200)
        self.m_src.grid(row=2, column=0, sticky="ew", padx=(20, 6), pady=(2, 18))
        self.m_tgt = self._menu(lg, [(c, n) for n, c, _ in LANGS], self.s["tgt"], 200)
        self.m_tgt.grid(row=2, column=1, sticky="ew", padx=(6, 20), pady=(2, 18))

        # --- ustawienia trybu z ekranu
        self.card_screen = sc = card(right)
        sc.grid_columnconfigure(1, weight=1)
        self._label(sc, "Napisy z ekranu", "h2").grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(16, 6))
        self._label(sc, "Obszar napisów", "body", "muted").grid(row=1, column=0, sticky="w", padx=(20, 12))
        self.v_region = tk.StringVar()
        self._label(sc, "", "body", textvariable=self.v_region).grid(row=1, column=1, sticky="w")
        rb = ctk.CTkFrame(sc, fg_color="transparent")
        rb.grid(row=2, column=0, columnspan=2, sticky="w", padx=20, pady=(6, 0))
        self._button(rb, "Zaznacz…", self.pick_region).pack(side="left")
        self._button(rb, "Pokaż", lambda: flash_region(self.root, self._region())).pack(side="left", padx=8)
        self._button(rb, "Domyślny", self.reset_region).pack(side="left")
        self._label(sc, "Wielkość tłumaczenia", "body", "muted").grid(row=3, column=0, sticky="w", padx=(20, 12),
                                                                      pady=(14, 18))
        self.v_scale = tk.IntVar(value=int(self.s.get("screen_scale", 100)))
        self._slider(sc, self.v_scale, 50, 200, 15, "%").grid(row=3, column=1, sticky="w", pady=(14, 18), padx=(0, 20))
        self._show_region()

        # --- ustawienia trybu ze słuchu
        self.card_speech = sp = card(right)
        sp.grid_columnconfigure(1, weight=1)
        self._label(sp, "Mowa ze słuchu", "h2").grid(row=0, column=0, columnspan=3, sticky="w", padx=20, pady=(16, 6))
        self._label(sp, "Źródło dźwięku", "small", "muted").grid(row=1, column=0, columnspan=3, sticky="w", padx=20)
        self.m_dev = self._menu(sp, [], "", 340)
        self.m_dev.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(20, 8), pady=(2, 8))
        self._button(sp, "Odśwież", self._refresh_devices, width=100).grid(row=2, column=2, padx=(0, 20), pady=(2, 8))
        self._label(sp, "Rozpoznawanie mowy", "body", "muted").grid(row=3, column=0, sticky="w", padx=(20, 12), pady=4)
        self.m_wh = self._menu(sp, [(k, v[1]) for k, v in WHISPER_MODELS.items()], self.s["whisper"], 300)
        self.m_wh.grid(row=3, column=1, columnspan=2, sticky="w", padx=(0, 20), pady=4)
        self._label(sp, "Wielkość napisów", "body", "muted").grid(row=4, column=0, sticky="w", padx=(20, 12), pady=4)
        self.v_font = tk.IntVar(value=int(self.s.get("font", 30)))
        self._slider(sp, self.v_font, 14, 70, 28, " pt").grid(row=4, column=1, columnspan=2, sticky="w", pady=4)
        self.v_orig = tk.BooleanVar(value=self.s["show_orig"])
        ctk.CTkSwitch(sp, text="Pokazuj też oryginał", variable=self.v_orig, font=self.f["body"],
                      progress_color=ACCENT).grid(row=5, column=0, columnspan=3, sticky="w", padx=20, pady=(6, 4))
        hint = ("macOS nie udostępnia dźwięku komputera wprost – zainstaluj darmowe BlackHole i wybierz je tutaj."
                if IS_MAC else "Wybierz „Dźwięk komputera”, żeby słuchać tego, co gra w głośnikach lub słuchawkach.")
        self.wrap(ctk.CTkLabel(sp, text=hint, font=self.f["tiny"], text_color=COLORS["faint"], anchor="w",
                               justify="left"), sp, 50).grid(row=6, column=0, columnspan=3, sticky="w", padx=20,
                                                             pady=(4, 16))
        self._refresh_devices()

        # --- historia
        self.card_hist = hist = card(right)
        self._label(hist, "Historia napisów", "h2").pack(anchor="w", padx=20, pady=(16, 6))
        self.t_hist = ctk.CTkTextbox(hist, font=self.f["small"], fg_color=COLORS["input"], text_color=COLORS["text"],
                                     wrap="word", corner_radius=10, border_width=0)
        self.t_hist.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.t_hist.tag_config("orig", foreground=COLORS["muted"])
        self.t_hist.tag_config("time", foreground=COLORS["faint"])
        self.t_hist.configure(state="disabled")
        self._mode_changed()

    def _slider(self, parent, var, lo, hi, steps, unit):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        lbl = self._label(f, f"{var.get()}{unit}", "body", width=60)

        def changed(v):
            var.set(int(round(float(v))))
            lbl.configure(text=f"{var.get()}{unit}")

        sl = ctk.CTkSlider(f, from_=lo, to=hi, number_of_steps=steps, width=200, progress_color=ACCENT,
                           button_color=ACCENT, button_hover_color=ACCENT_HOVER, command=changed)
        sl.set(var.get())
        sl.pack(side="left")
        lbl.pack(side="left", padx=(10, 0))
        return f

    def _mode_changed(self):
        mode = self.v_mode.get()
        for k, t, d in self.MODES:
            if k == mode:
                self.v_mode_btn.set(t)
                self.v_mode_desc.set(d)
        for c in (self.card_screen, self.card_speech, self.card_hist):
            c.pack_forget()
        (self.card_screen if mode == "screen" else self.card_speech).pack(fill="x")
        self.card_hist.pack(fill="both", expand=True, pady=(14, 0))

    def _set_dot(self, state):
        col = {"off": "#6d7078", "on": COLORS["ok"], "pause": COLORS["warn"], "load": ACCENT}[state]
        s = ui_scaling(self.root)
        self.dot.configure(width=int(22 * s), height=int(22 * s))
        self.dot.delete("all")
        self.dot.create_oval(3 * s, 3 * s, 19 * s, 19 * s, fill=col, outline="")

    def _refresh_devices(self):
        try:
            self.devices = list_devices()
        except Exception as e:
            traceback.print_exc()
            self.devices = []
            self.v_status.set(f"Nie mogę odczytać urządzeń dźwięku: {e}")
        names = [d[0] for d in self.devices] or ["(brak urządzeń dźwięku)"]
        self.m_dev.configure(values=names)
        self.m_dev._names, self.m_dev._keys = names, list(range(len(names)))
        want = self.s.get("device") or ""
        self.m_dev._var.set(want if want in names else names[0])

    # ------------------------------------------------------------- okno ustawień
    def _open_settings(self):
        if getattr(self, "_settings_win", None) and self._settings_win.winfo_exists():
            self._settings_win.focus()
            return
        w = ctk.CTkToplevel(self.root, fg_color=COLORS["bg"])
        self._settings_win = w
        w.title("AMTSB – ustawienia")
        set_window_icon(w, os.path.join(ASSETS, "amtsb.ico"), os.path.join(ASSETS, "amtsb_256.png"))
        w.transient(self.root)
        w.resizable(False, False)
        c = card(w)
        c.pack(fill="both", expand=True, padx=18, pady=18)
        self._label(c, "Ustawienia", "h2").grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(16, 6))
        self._label(c, "Model tłumaczenia", "body", "muted").grid(row=1, column=0, sticky="w", padx=(20, 16), pady=8)
        self.m_nl = self._menu(c, [(k, v[1]) for k, v in NLLB_MODELS.items()], self.s["nllb"], 340)
        self.m_nl.grid(row=1, column=1, sticky="w", padx=(0, 20))
        self._label(c, "Wielkość okna i tekstu", "body", "muted").grid(row=2, column=0, sticky="w", padx=(20, 16),
                                                                      pady=8)
        ctk.CTkSegmentedButton(c, values=list(UI_SCALES), font=self.f["small"], selected_color=ACCENT,
                               selected_hover_color=ACCENT_HOVER, command=self._set_ui_scale,
                               variable=tk.StringVar(value=self.s.get("ui_scale", "100%"))).grid(
            row=2, column=1, sticky="w", padx=(0, 20))
        self._label(c, "Wielkość dopasowuje się też do powiększenia ustawionego w systemie.", "small", "faint").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=20)
        ctk.CTkButton(c, text="Gotowe", width=120, height=38, font=self.f["button"], fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, command=lambda: (self._cfg(), w.destroy())).grid(
            row=4, column=0, columnspan=2, sticky="e", padx=20, pady=(14, 18))
        w.protocol("WM_DELETE_WINDOW", lambda: (self._cfg(), w.destroy()))
        w.after(100, w.lift)

    def _set_ui_scale(self, value):
        self.s["ui_scale"] = value
        ctk.set_widget_scaling(UI_SCALES[value])
        ctk.set_window_scaling(UI_SCALES[value])
        self.root.after(200, lambda: self._set_dot("on" if self.engine else "off"))
        save_settings(APP, self.s)

    # ------------------------------------------------------------- obszar napisów
    def _region(self):
        r = self.s.get("region")
        return r if r and len(r) == 4 else default_region(self.root)

    def _show_region(self):
        r = self.s.get("region")
        self.v_region.set(f"zaznaczony ({r[2]} × {r[3]})" if r and len(r) == 4 else "dół ekranu (domyślny)")

    def pick_region(self):
        def done(r):
            self.s["region"] = r
            save_settings(APP, self.s)
            self._show_region()
            flash_region(self.root, r, 1000)
        RegionPicker(self.root, self._region(), done)

    def reset_region(self):
        self.s["region"] = None
        save_settings(APP, self.s)
        self._show_region()

    # ------------------------------------------------------------- start / stop
    def _cfg(self):
        s = self.s
        s["mode"] = self.v_mode.get()
        s["src"], s["tgt"] = self._key(self.m_src), self._key(self.m_tgt)
        s["whisper"] = self._key(self.m_wh)
        if getattr(self, "m_nl", None) and self.m_nl.winfo_exists():
            s["nllb"] = self._key(self.m_nl)
        s["screen_scale"], s["font"], s["show_orig"] = self.v_scale.get(), self.v_font.get(), self.v_orig.get()
        if self.devices:
            s["device"] = self.m_dev._var.get()
        if not (self.args.get("screen_region") or self.args.get("feed")):  # testy nie zmieniają ustawień
            save_settings(APP, s)
        return dict(s)

    def _lock(self, running):
        st = "disabled" if running else "normal"
        for m in (self.m_src, self.m_tgt, self.m_wh, self.m_dev, self.seg_mode):
            m.configure(state=st)
        self.b_start.configure(text="■  Stop" if running else "▶  Start")
        self.b_pause.configure(state="normal" if running else "disabled", text="Pauza")

    def toggle(self):
        if self.engine:
            self.stop()
            return
        cfg = self._cfg()
        if cfg["mode"] == "screen":
            excl = capture_exclusion_works(self.root)
            self.engine = ScreenEngine(cfg, self._region(), self.events)
            # tryb „nad oryginałem”: silnik wymazuje z obrazu nasz pasek, żeby nie czytać własnego tłumaczenia
            self.overlay = CoverBox(self.root, cfg["screen_scale"] / 100, mode="cover" if excl else "above",
                                    debug=bool(self.args.get("screen_region")),
                                    on_rect=None if excl else self.engine.set_mask)
            if excl:
                set_capture_excluded(self.root, True)  # OCR nie czyta okna programu
            else:
                self._add_history("Ten system nie pozwala ukryć okna przed zrzutem ekranu – tłumaczenie pokaże się "
                                  "nad oryginalnymi napisami, zamiast je zakrywać.", None)
        else:
            if self.args.get("feed"):
                source = FileFeedSource(self.args["feed"])
            else:
                i = self.m_dev._names.index(self.m_dev._var.get()) if self.m_dev._var.get() in self.m_dev._names else 0
                if not self.devices:
                    messagebox.showerror("AMTSB", "Nie znaleziono żadnego urządzenia dźwięku.")
                    return
                source = make_source(self.devices[i][1])
            self.overlay = Overlay(self.root, cfg["font"], cfg["show_orig"], self.toggle_pause, self.stop,
                                   self._overlay_changed)
            self.overlay.set_status("Ładowanie modeli…")
            self.engine = LiveEngine(cfg, source, self.events)
        self.v_state.set("Uruchamianie…")
        self._set_dot("load")
        self.engine.start()
        self._lock(True)

    def stop(self):
        if self.engine:
            self.engine.stop_evt.set()
        self.engine = None
        if self.overlay:
            try:
                self.overlay.destroy()
            except tk.TclError:
                pass
            self.overlay = None
        set_capture_excluded(self.root, False)
        self._lock(False)
        self._set_dot("off")
        self.v_state.set("Wyłączone")
        self.v_status.set("Kliknij Start, a potem puść film.")

    def toggle_pause(self):
        if not self.engine:
            return
        if self.engine.paused.is_set():
            self.engine.paused.clear()
            self.b_pause.configure(text="Pauza")
            self._set_dot("on")
            self.v_state.set(self._running_label())
        else:
            self.engine.paused.set()
            self.b_pause.configure(text="Wznów")
            self._set_dot("pause")
            self.v_state.set("Pauza")
            if isinstance(self.overlay, CoverBox):
                self.overlay.hide()

    def _running_label(self):
        return "Działa – tłumaczę napisy" if isinstance(self.engine, ScreenEngine) else "Działa – słucham filmu"

    def _overlay_changed(self, **kw):
        self.s.update(kw)
        if "font" in kw:
            self.v_font.set(kw["font"])
        if "show_orig" in kw:
            self.v_orig.set(kw["show_orig"])
        save_settings(APP, self.s)

    def _add_history(self, orig, trans):
        t = self.t_hist
        t.configure(state="normal")
        t.insert("end", time.strftime("%H:%M:%S  "), "time")
        if trans and trans != orig:
            t.insert("end", f"{trans}\n")
            t.insert("end", f"            {orig}\n", "orig")
        else:
            t.insert("end", f"{orig}\n")
        if float(t.index("end")) > 3000:
            t.delete("1.0", "500.0")
        t.see("end")
        t.configure(state="disabled")

    def _hotkey_check(self):
        if not self.hotkeys.ok:
            self.v_keys.set("Skróty klawiszowe są niedostępne (zajęte przez inny program albo brak uprawnień).")

    # ------------------------------------------------------------- zdarzenia
    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "status":
                    self.v_status.set(ev[1])
                    if isinstance(self.overlay, Overlay):
                        self.overlay.set_status(ev[1] if not ev[1].startswith("Słucham") else
                                                "Słucham… (przeciągnij napisy myszką · prawy klik = menu)")
                elif kind == "ready":
                    if self.engine:
                        self._set_dot("on")
                        self.v_state.set(self._running_label())
                    if self.args.get("geom_file") and isinstance(self.overlay, Overlay):
                        o = self.overlay
                        with open(self.args["geom_file"], "w") as f:
                            f.write(f"{o.winfo_x()} {o.winfo_y()} {o.W} {o.H}")
                elif kind == "screen_sub":
                    _, orig, trans, box, line_h, region = ev
                    if isinstance(self.overlay, CoverBox) and self.engine and not self.engine.paused.is_set():
                        self.overlay.show(box, line_h, trans, region)
                    self._add_history(" ".join(orig), " ".join(trans))
                elif kind == "screen_clear":
                    if isinstance(self.overlay, CoverBox):
                        self.overlay.hide()
                elif kind == "cue":
                    _, orig, trans, src, delay = ev
                    if isinstance(self.overlay, Overlay):
                        self.overlay.add_cue(orig, trans)
                    self._add_history(orig, trans)
                    self.v_status.set(f"Język: {lang_label(src)}  ·  opóźnienie ok. {delay:.1f} s")
                    if self.args.get("feed"):
                        print(f"CUE delay={delay:.1f}s | {orig} | {trans}", flush=True)
                elif kind == "engine_stopped":
                    if ev[1] is self.engine:
                        msg = self.v_status.get()
                        self.stop()
                        if msg.startswith("Błąd"):
                            self.v_status.set(msg)
                elif kind == "hotkey":
                    if ev[1] == 1:
                        self.toggle()
                    elif ev[1] == 2:
                        self.toggle_pause()
                elif kind == "error":
                    self.v_status.set("Błąd: " + ev[1])
                    if self.args.get("quit_after"):
                        print("ERROR", ev[1], flush=True)
                    else:
                        messagebox.showerror("AMTSB – błąd", f"{ev[1]}\n\nSzczegóły w pliku:\n{LOG_FILE}")
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    def _on_close(self):
        if self.engine:
            self.engine.stop_evt.set()
        self.hotkeys.stop()
        try:
            self._cfg()
        except Exception:
            traceback.print_exc()
        self.root.destroy()


def main():
    argv = sys.argv[1:]
    args = {}
    if "--screen" in argv:  # test: od razu tryb z ekranu w obszarze x,y,w,h
        args["screen_region"] = [int(v) for v in argv[argv.index("--screen") + 1].split(",")]
    if "--feed" in argv:    # test: tryb ze słuchu z pliku audio zamiast głośników
        args["feed"] = argv[argv.index("--feed") + 1]
    if "--geom" in argv:
        args["geom_file"] = argv[argv.index("--geom") + 1]
    if "--quit-after" in argv:
        args["quit_after"] = float(argv[argv.index("--quit-after") + 1])
    set_app_id("AMT.AutomaticMovieTranslatorSubtitles")
    init_ui(UI_SCALES.get(load_settings(APP, DEFAULTS).get("ui_scale"), 1.0))
    root, _ = make_root(dnd=False)
    AMTSBApp(root, args)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        try:
            messagebox.showerror("AMTSB", traceback.format_exc())
        except Exception:
            pass
        raise
