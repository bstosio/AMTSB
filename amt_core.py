# -*- coding: utf-8 -*-
"""
amt_core – wspólny rdzeń programów AMT i AMTSB.

* rozpoznawanie mowy: Whisper (faster-whisper / CTranslate2)
* tłumaczenie:        NLLB-200 (CTranslate2)

Wszystko działa lokalnie (Windows, macOS, Linux). Modele pobierają się tylko raz do ~/.amt/models
(w Windows: %USERPROFILE%\\.amt\\models; inny folder: zmienna środowiskowa AMT_HOME).
"""
import ctypes
import json
import math
import os
import re
import sys
import textwrap
import threading
import traceback

APP_DIR = os.environ.get("AMT_HOME") or os.path.join(os.path.expanduser("~"), ".amt")
MODELS_DIR = os.path.join(APP_DIR, "models")
os.makedirs(MODELS_DIR, exist_ok=True)
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

SR = 16000  # częstotliwość próbkowania, której używa Whisper


def setup_logging(name):
    """Pod pythonw (bez konsoli) komunikaty i błędy trafiają do pliku <name>.log."""
    path = os.path.join(APP_DIR, f"{name}.log")
    if sys.stdout is None or sys.stderr is None:
        f = open(path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
    return path


# ----------------------------------------------------------------- języki

# (nazwa, kod Whisper, kod NLLB)
LANGS = [
    ("polski", "pl", "pol_Latn"), ("angielski", "en", "eng_Latn"),
    ("niemiecki", "de", "deu_Latn"), ("francuski", "fr", "fra_Latn"),
    ("hiszpański", "es", "spa_Latn"), ("włoski", "it", "ita_Latn"),
    ("portugalski", "pt", "por_Latn"), ("rosyjski", "ru", "rus_Cyrl"),
    ("ukraiński", "uk", "ukr_Cyrl"), ("czeski", "cs", "ces_Latn"),
    ("słowacki", "sk", "slk_Latn"), ("niderlandzki", "nl", "nld_Latn"),
    ("szwedzki", "sv", "swe_Latn"), ("norweski", "no", "nob_Latn"),
    ("duński", "da", "dan_Latn"), ("fiński", "fi", "fin_Latn"),
    ("węgierski", "hu", "hun_Latn"), ("rumuński", "ro", "ron_Latn"),
    ("grecki", "el", "ell_Grek"), ("turecki", "tr", "tur_Latn"),
    ("arabski", "ar", "arb_Arab"), ("hebrajski", "he", "heb_Hebr"),
    ("hindi", "hi", "hin_Deva"), ("japoński", "ja", "jpn_Jpan"),
    ("koreański", "ko", "kor_Hang"), ("chiński", "zh", "zho_Hans"),
    ("wietnamski", "vi", "vie_Latn"), ("tajski", "th", "tha_Thai"),
    ("indonezyjski", "id", "ind_Latn"), ("bułgarski", "bg", "bul_Cyrl"),
    ("serbski", "sr", "srp_Cyrl"), ("chorwacki", "hr", "hrv_Latn"),
    ("litewski", "lt", "lit_Latn"), ("łotewski", "lv", "lvs_Latn"),
    ("estoński", "et", "est_Latn"), ("białoruski", "be", "bel_Cyrl"),
]
LANG_NAME = {code: name for name, code, _ in LANGS}
NLLB_CODE = {code: nllb for _, code, nllb in LANGS}

# kody języków zapisywane w plikach filmów (ISO 639-2)
ISO3 = {"eng": "en", "pol": "pl", "ger": "de", "deu": "de", "fre": "fr", "fra": "fr",
        "spa": "es", "ita": "it", "por": "pt", "rus": "ru", "ukr": "uk", "cze": "cs",
        "ces": "cs", "slo": "sk", "slk": "sk", "dut": "nl", "nld": "nl", "swe": "sv",
        "nor": "no", "nob": "no", "dan": "da", "fin": "fi", "hun": "hu", "rum": "ro",
        "ron": "ro", "gre": "el", "ell": "el", "tur": "tr", "ara": "ar", "heb": "he",
        "hin": "hi", "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh", "vie": "vi",
        "tha": "th", "ind": "id", "bul": "bg", "srp": "sr", "hrv": "hr", "lit": "lt",
        "lav": "lv", "est": "et", "bel": "be"}
ISO3_OUT = {}
for _k, _v in ISO3.items():
    ISO3_OUT.setdefault(_v, _k)


def lang_label(code):
    return LANG_NAME.get(code, code or "nieznany")


# ----------------------------------------------------------------- modele

# klucz: (repozytorium, opis, przybliżony rozmiar w MB)
WHISPER_MODELS = {
    "small": ("Systran/faster-whisper-small", "small – zalecany (szybki, dobra jakość)", 484),
    "base": ("Systran/faster-whisper-base", "base – najszybszy, słabsza jakość", 145),
    "medium": ("Systran/faster-whisper-medium", "medium – lepsza jakość, ok. 3× wolniej", 1530),
    "large-v3-turbo": ("mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                       "large-v3-turbo – najlepsza jakość, najwolniejszy", 1620),
}
WHISPER_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]

NLLB_MODELS = {
    "600M": ("JustFrederik/nllb-200-distilled-600M-ct2-int8", "NLLB 600M – szybki (zalecany)", 627),
    "1.3B": ("JustFrederik/nllb-200-distilled-1.3B-ct2-int8", "NLLB 1.3B – większy, wolniejszy", 1390),
}
NLLB_PATTERNS = ["config.json", "model.bin", "sentencepiece.bpe.model", "shared_vocabulary.txt"]


def cpu_threads():
    return max(2, (os.cpu_count() or 4) // 2)


def dir_size(path):
    total = 0
    for r, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return total


def ensure_model(repo, patterns, label, size_mb, status):
    """Pobiera model z Hugging Face (tylko za pierwszym razem) i zwraca jego folder."""
    path = os.path.join(MODELS_DIR, repo.split("/")[1])
    marker = os.path.join(path, ".gotowe")
    if os.path.exists(marker):
        return path
    from huggingface_hub import snapshot_download

    done = threading.Event()

    def watch():
        while not done.wait(1.0):
            status(f"Pobieranie modelu {label}: {dir_size(path) / 1e6:.0f} / ~{size_mb} MB (tylko za pierwszym razem)")

    status(f"Pobieranie modelu {label} (tylko za pierwszym razem)…")
    threading.Thread(target=watch, daemon=True).start()
    try:
        snapshot_download(repo, local_dir=path, allow_patterns=patterns)
    finally:
        done.set()
    with open(marker, "w") as f:
        f.write(repo)
    return path


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def load_whisper(key, status):
    """Model rozpoznawania mowy (wczytany raz i trzymany w pamięci)."""
    with _CACHE_LOCK:
        if ("whisper", key) not in _CACHE:
            repo, _, size = WHISPER_MODELS[key]
            path = ensure_model(repo, WHISPER_PATTERNS, f"rozpoznawania mowy ({key})", size, status)
            status(f"Ładowanie modelu rozpoznawania mowy ({key})…")
            from faster_whisper import WhisperModel
            _CACHE[("whisper", key)] = WhisperModel(path, device="cpu", compute_type="int8",
                                                    cpu_threads=cpu_threads())
        return _CACHE[("whisper", key)]


def get_translator(key, status):
    """Model tłumaczenia (wczytany raz i trzymany w pamięci)."""
    with _CACHE_LOCK:
        if ("nllb", key) not in _CACHE:
            _CACHE[("nllb", key)] = Translator(key, status)
        return _CACHE[("nllb", key)]


# ----------------------------------------------------------------- tekst

HALLUCINATIONS = [
    "amara.org", "napisy stworzone przez", "napisy wykonane przez", "thanks for watching",
    "thank you for watching", "subtitles by", "please subscribe", "like and subscribe",
    "transcribed by", "transcription by", "untertitel", "sous-titrage", "sous-titres réalisés",
    "subskrybuj", "dziękuję za obejrzenie", "www.", ".com", "copyright",
]
TAG_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|[♪♫]+")
SENT_END = re.compile(r"[.!?…][\"'”»)\]]*$")
ABBREV = re.compile(r"\b(Mr|Mrs|Ms|Dr|St|Jr|Sr|Prof|vs|etc|Lt|Sgt|Capt|Col|Gen)\.$", re.I)


def clean_text(t):
    """Usuwa opisy dźwięków ([MUZYKA], (śmiech)), nuty i typowe „halucynacje” rozpoznawania mowy."""
    t = TAG_RE.sub(" ", t or "")
    t = re.sub(r"\s+", " ", t).strip(" -–—")
    if not re.search(r"\w", t):
        return ""
    low = t.lower()
    if any(h in low for h in HALLUCINATIONS):
        return ""
    return t


def split_sentences(text):
    parts = [p for p in re.split(r"(?<=[.!?…])\s+(?=\S)", text.strip()) if p]
    out = []
    for p in parts:
        if out and (ABBREV.search(out[-1]) or len(out[-1]) < 4):
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def fix_translation(src, out):
    """NLLB uczył się m.in. na napisach i czasem dopisuje od siebie drugą kwestię dialogu
    („O czym mówisz? - Nie wiem.”) albo powtarza zdanie („W porządku. W porządku.”)."""
    out = out.strip()
    if not src.lstrip().startswith(("-", "–")):
        out = re.sub(r"^[-–]\s*", "", out)
    if not re.search(r"[.!?…]\s+[-–]\s", src):
        m = re.search(r"[.!?…]\s+[-–]\s", out)
        if m:
            out = out[:m.start() + 1]
    dedup = []
    for s in split_sentences(out):
        if not dedup or s.lower().rstrip(".!?… ") != dedup[-1].lower().rstrip(".!?… "):
            dedup.append(s)
    out = " ".join(dedup) or out
    out = re.sub(r"([.!?…])\s+[!?.]+$", r"\1", out)  # "O czym mówisz? !" -> "O czym mówisz?"
    return out


# Krótkie wtrącenia – na nich model tłumaczenia myli się najczęściej („Okay.” -> „Nie, nie.”).
SHORT_EN_PL = {
    "okay": "Dobrze", "ok": "Dobrze", "yeah": "Tak", "yes": "Tak", "yep": "Tak", "no": "Nie",
    "nope": "Nie", "what": "Co", "hey": "Hej", "hi": "Cześć", "hello": "Cześć", "thanks": "Dzięki",
    "thank you": "Dziękuję", "sorry": "Przepraszam", "i'm sorry": "Przepraszam",
    "come on": "No dalej", "wait": "Czekaj", "please": "Proszę", "really": "Naprawdę",
    "right": "Racja", "sure": "Jasne", "fine": "Dobrze", "oh my god": "O mój Boże",
    "my god": "Mój Boże", "let's go": "Chodźmy", "help": "Pomocy", "shut up": "Zamknij się",
    "good": "Dobrze", "great": "Świetnie", "why": "Dlaczego", "where": "Gdzie", "who": "Kto",
    "how": "Jak", "oh": "Och", "wow": "Łał", "damn": "Cholera", "shit": "Cholera",
    "i know": "Wiem", "i don't know": "Nie wiem", "excuse me": "Przepraszam", "of course": "Oczywiście",
    "all right": "Dobrze", "alright": "Dobrze", "be careful": "Uważaj", "go away": "Odejdź",
    "get out": "Wynoś się", "uh-huh": "Aha", "hmm": "Hmm", "mm-hmm": "Aha", "bye": "Cześć",
    "goodbye": "Do widzenia", "good night": "Dobranoc", "good morning": "Dzień dobry",
}


def phrasebook(sent, src, tgt):
    if src != "en" or tgt != "pl":
        return None
    m = re.fullmatch(r"\s*(.*?)([.!?…]*)\s*", sent)
    key = m.group(1).lower().strip(" ,")
    return SHORT_EN_PL[key] + (m.group(2) or ".") if key in SHORT_EN_PL else None


def wrap_sub(text, width=42):
    """Łamie napis na równe linie (do ~42 znaków)."""
    if len(text) <= width:
        return text
    n = math.ceil(len(text) / width)
    for w in range(math.ceil(len(text) / n), len(text) + 1):
        lines = textwrap.wrap(text, width=w)
        if len(lines) <= n:
            return "\n".join(lines)
    return text


STOPWORDS = {
    "en": "the you and to is it that of what i this in me don't not be have are we was your",
    "de": "der die und ich du das nicht ist es sie wir zu was ein mit den mir",
    "fr": "le la les et je tu vous est pas que de un une ce qui il",
    "es": "el la los que y de no es un una por qué lo me te se",
    "it": "il la che e di non è un una per mi ti lo questo sono",
    "pt": "o a que de não é um uma eu você os para com isso",
    "nl": "de het een en ik je niet is dat wat van we zijn",
    "pl": "nie się to że jest co na w jak ja ty mnie tak",
    "cs": "je to že se na ne jsem co jak ty já tak by",
    "sv": "och att det är jag du inte en som på vi har",
}
_STOPSETS = {k: set(v.split()) for k, v in STOPWORDS.items()}


def guess_lang(texts, default="en"):
    """Zgrubne rozpoznanie języka tekstu (po alfabecie i najczęstszych słowach)."""
    sample = " ".join(texts)[:20000]
    if not sample.strip():
        return default
    if re.search(r"[぀-ヿ]", sample):
        return "ja"
    if re.search(r"[가-힯]", sample):
        return "ko"
    if re.search(r"[一-鿿]", sample):
        return "zh"
    cyr = len(re.findall(r"[Ѐ-ӿ]", sample))
    if cyr > len(sample) * 0.3:
        return "uk" if len(re.findall(r"[іїєґ]", sample.lower())) > cyr * 0.02 else "ru"
    for pat, code in ((r"[؀-ۿ]", "ar"), (r"[֐-׿]", "he"), (r"[Ͱ-Ͽ]", "el")):
        if re.search(pat, sample):
            return code
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", sample.lower())
    scores = {k: sum(w in s for w in words) for k, s in _STOPSETS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else default


class Cancelled(Exception):
    pass


class Translator:
    """Tłumaczenie NLLB-200 przez CTranslate2 (int8, procesor)."""

    def __init__(self, key, status):
        repo, _, size = NLLB_MODELS[key]
        path = ensure_model(repo, NLLB_PATTERNS, f"tłumaczenia ({key})", size, status)
        status(f"Ładowanie modelu tłumaczenia ({key})…")
        import ctranslate2
        import sentencepiece as spm
        self.tr = ctranslate2.Translator(path, device="cpu", compute_type="int8",
                                         intra_threads=cpu_threads())
        self.sp = spm.SentencePieceProcessor(model_file=os.path.join(path, "sentencepiece.bpe.model"))

    def translate(self, texts, src, tgt, beam=2):
        """Tłumaczy listę tekstów; każdy tekst jest dzielony na zdania."""
        s, t = NLLB_CODE[src], NLLB_CODE[tgt]
        pieces = []  # [indeks_tekstu, zdanie, tłumaczenie|None]
        for i, txt in enumerate(texts):
            for sent in split_sentences(txt):
                pieces.append([i, sent, phrasebook(sent, src, tgt)])
        todo = [p for p in pieces if p[2] is None]
        if todo:
            batch = [[s] + self.sp.encode(p[1], out_type=str)[:400] + ["</s>"] for p in todo]
            maxlen = min(320, max(len(b) for b in batch) * 2 + 12)
            res = self.tr.translate_batch(batch, target_prefix=[[t]] * len(batch), beam_size=beam,
                                          max_decoding_length=maxlen, max_batch_size=32)
            for p, r in zip(todo, res):
                toks = r.hypotheses[0]
                if toks and toks[0] == t:
                    toks = toks[1:]
                p[2] = fix_translation(p[1], self.sp.decode(toks))
        outs = [""] * len(texts)
        for i, _, o in pieces:
            outs[i] = (outs[i] + " " + o).strip()
        return outs


# ----------------------------------------------------------------- drobiazgi

def srt_time(t):
    ms = max(0, int(round(t * 1000)))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def fmt_dur(s):
    s = int(s)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h} h {m} min" if h else (f"{m} min {s} s" if m else f"{s} s")


def fmt_clock(s):
    s = int(s or 0)
    return f"{s // 3600}:{s % 3600 // 60:02}:{s % 60:02}"


def load_settings(name, defaults):
    """Ustawienia z %USERPROFILE%\\.amt\\<name>.json (albo ze starej wersji programu)."""
    s = dict(defaults)
    for fn in (f"{name}.json", "ustawienia.json"):
        p = os.path.join(APP_DIR, fn)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    s.update({k: v for k, v in json.load(f).items() if k in defaults})
                break
            except Exception:
                traceback.print_exc()
    return s


def save_settings(name, s):
    try:
        with open(os.path.join(APP_DIR, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        traceback.print_exc()


# ----------------------------------------------------------------- system

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
NO_WINDOW = 0x08000000 if IS_WIN else 0  # Windows: bez czarnego okienka konsoli przy ffmpeg
UI_FONT = "Segoe UI" if IS_WIN else ("Helvetica Neue" if IS_MAC else "DejaVu Sans")


def open_path(path):
    """Otwiera plik domyślnym programem (np. film w odtwarzaczu)."""
    import subprocess
    if IS_WIN:
        os.startfile(path)
    else:
        subprocess.Popen(["open" if IS_MAC else "xdg-open", path])


def reveal_path(path):
    """Pokazuje plik w Eksploratorze / Finderze / menedżerze plików."""
    import subprocess
    if IS_WIN:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif IS_MAC:
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(os.path.abspath(path))])


def set_app_id(app_id):
    """Windows: własna ikona na pasku zadań (zamiast ikony Pythona)."""
    if IS_WIN:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass


def hwnd_of(win):
    """Windows: uchwyt okna najwyższego poziomu."""
    return int(win.wm_frame(), 16)


# ----------------------------------------------------------------- wygląd (CustomTkinter)

COLORS = dict(bg="#17181b", card="#212226", card_border="#303238", input="#2a2c31", input_hover="#34363c",
              text="#eceef1", muted="#a1a4ab", faint="#6d7078", ok="#4cc38a", warn="#f5c84c", err="#ff7a85")
ACCENTS = {"amt": ("#5b6cff", "#4857e0"), "amtsb": ("#13a3aa", "#0e858b")}
UI_SCALES = {"90%": 0.9, "100%": 1.0, "110%": 1.1, "125%": 1.25}


def init_ui(ui_scale=1.0):
    """Ciemny motyw. CustomTkinter sam dopasowuje wszystko do powiększenia ekranu
    ustawionego w systemie (np. 150% w Windows); ui_scale to dodatkowe powiększenie."""
    if IS_WIN:  # przed utworzeniem okna – inaczej Tk zapamięta rozmiar ekranu bez powiększenia
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    import customtkinter as ctk
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    ctk.set_widget_scaling(ui_scale)
    ctk.set_window_scaling(ui_scale)
    return ctk


def make_root(dnd=False):
    """Główne okno; z dnd=True obsługuje też przeciąganie plików (tkinterdnd2)."""
    import customtkinter as ctk
    if dnd:
        try:
            from tkinterdnd2 import TkinterDnD

            class Root(ctk.CTk, TkinterDnD.DnDWrapper):
                def __init__(self):
                    super().__init__(fg_color=COLORS["bg"])
                    self.TkdndVersion = TkinterDnD._require(self)

            return Root(), True
        except Exception:
            traceback.print_exc()
    return ctk.CTk(fg_color=COLORS["bg"]), False


def make_fonts():
    """Czcionki (rozmiary w punktach CustomTkinter – skalują się z powiększeniem ekranu)."""
    import customtkinter as ctk

    def f(size, weight="normal"):
        return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)

    return dict(body=f(15), small=f(13), tiny=f(12), h2=f(17, "bold"), big=f(20, "bold"),
                title=f(30, "bold"), button=f(16, "bold"), mono=f(13))


def ui_scaling(widget):
    """Łączny współczynnik powiększenia (system × ustawienie programu)."""
    try:
        import customtkinter as ctk
        return ctk.ScalingTracker.get_widget_scaling(widget)
    except Exception:
        return 1.0


def fit_geometry(root, w, h):
    """Ustawia rozmiar okna (w jednostkach logicznych), ale nie większy niż ekran."""
    try:
        import customtkinter as ctk
        s = ctk.ScalingTracker.get_window_scaling(root)
    except Exception:
        s = 1.0
    sw, sh = root.winfo_screenwidth() / s, root.winfo_screenheight() / s
    w, h = int(min(w, sw - 40)), int(min(h, sh - 90))
    root.geometry(f"{w}x{h}+{int(max(0, (sw - w) / 2) * s)}+{int(max(0, (sh - h) / 2 - 20) * s)}")


def set_window_icon(win, ico=None, png=None):
    """Ikona okna i paska zadań. W Windows ładowana z .ico dokładnie w rozmiarze
    dla bieżącego powiększenia ekranu – dzięki temu jest ostra (a nie rozciągnięta 32 px)."""
    if IS_WIN and ico and os.path.exists(ico):
        try:
            win.iconbitmap(ico)  # zastępuje domyślną ikonę CustomTkinter
        except Exception:
            traceback.print_exc()

        def apply():
            try:
                import ctypes.wintypes as wt
                u = ctypes.WinDLL("user32", use_last_error=True)
                u.LoadImageW.restype = ctypes.c_void_p
                u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
                u.SendMessageW.restype = ctypes.c_ssize_t
                u.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_void_p]
                h = hwnd_of(win)
                try:
                    u.GetDpiForWindow.argtypes = [wt.HWND]
                    dpi = u.GetDpiForWindow(h) or 96
                    sizes = ((1, u.GetSystemMetricsForDpi(11, dpi)), (0, u.GetSystemMetricsForDpi(49, dpi)))
                except Exception:
                    sizes = ((1, u.GetSystemMetrics(11)), (0, u.GetSystemMetrics(49)))
                for which, size in sizes:  # ICON_BIG (pasek zadań, Alt+Tab), ICON_SMALL (pasek tytułu)
                    hicon = u.LoadImageW(None, ico, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
                    if hicon:
                        u.SendMessageW(h, 0x0080, which, hicon)  # WM_SETICON
            except Exception:
                traceback.print_exc()

        win.after(300, apply)  # po tym, jak CustomTkinter skończy ustawiać okno
    elif png and os.path.exists(png):
        try:
            import tkinter as tk
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._amt_icon = img
        except Exception:
            traceback.print_exc()


class AutoWrap:
    """Etykiety CTkLabel, które łamią tekst do szerokości swojego kontenera."""

    def __init__(self):
        self.items = []

    def __call__(self, label, container, margin=30):
        self.items.append(label)

        def on(e):
            s = ui_scaling(label)
            label.configure(wraplength=max(150, int(e.width / s) - margin))

        container.bind("<Configure>", on, add="+")
        return label


# ----------------------------------------------------------------- rozwijana lista (jak w Windows 11)

def __getattr__(name):
    """Klasy okienek tworzone dopiero przy pierwszym użyciu (tryb bez okna nie wczytuje tkintera)."""
    if name == "Dropdown":
        cls = _make_dropdown_class()
        globals()["Dropdown"] = cls
        return cls
    raise AttributeError(name)


def _round_corners(win):
    """Windows 11: zaokrąglone rogi okienka (jak w menu systemowych)."""
    if IS_WIN:
        try:
            v = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd_of(win), 33, ctypes.byref(v), 4)
        except Exception:
            pass


def _mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(a, b))


def _make_dropdown_class():
    import tkinter as tk
    import tkinter.font as tkfont
    import customtkinter as ctk

    class Dropdown(ctk.CTkFrame):
        """Pole wyboru z listą, która płynnie się wysuwa, ma pasek przewijania i działa kółkiem myszy
        oraz klawiaturą (strzałki, Enter, Esc, pierwsza litera). Zgodne z CTkOptionMenu w zakresie,
        którego używają AMT i AMTSB: values, variable, command, configure(values=…, state=…)."""
        MAX_ROWS = 9
        ROW_H = 34
        _open_one = None

        def __init__(self, master, values, variable, width=200, height=36, command=None, accent="#5b6cff",
                     font_size=15, **kw):
            super().__init__(master, width=width, height=height, corner_radius=8, border_width=1,
                             fg_color=COLORS["input"], border_color=COLORS["card_border"], **kw)
            self.values, self.var, self.command, self.accent = list(values), variable, command, accent
            self.font_size, self.state = font_size, "normal"
            self.popup = None
            self.grid_propagate(False)
            self.pack_propagate(False)
            self.lbl = ctk.CTkLabel(self, textvariable=variable, anchor="w", text_color=COLORS["text"],
                                    font=ctk.CTkFont(family=UI_FONT, size=font_size))
            self.lbl.pack(side="left", fill="both", expand=True, padx=(12, 0))
            self.arrow = tk.Canvas(self, width=10, height=10, highlightthickness=0, bd=0, bg=COLORS["input"])
            self.arrow.pack(side="right", padx=(6, 12))
            self.after(10, self._draw_arrow)
            for w in (self, self.lbl, self.arrow):
                w.bind("<Button-1>", lambda e: self.toggle(), add="+")
                w.bind("<Enter>", lambda e: self._hover(True), add="+")
                w.bind("<Leave>", lambda e: self._hover(False), add="+")
            self.bind("<Configure>", lambda e: self._draw_arrow(), add="+")

        # --- wygląd pola
        def _draw_arrow(self, up=False):
            s = ui_scaling(self)
            size = int(12 * s)
            self.arrow.configure(width=size, height=size)
            self.arrow.delete("all")
            col = COLORS["faint"] if self.state == "disabled" else COLORS["muted"]
            y0, y1 = (size * 0.65, size * 0.35) if up else (size * 0.35, size * 0.65)
            self.arrow.create_line(size * 0.15, y0, size * 0.5, y1, size * 0.85, y0, fill=col,
                                   width=max(2, int(1.8 * s)), capstyle="round", joinstyle="round")

        def _bg(self, color):
            self.configure(fg_color=color)
            self.arrow.configure(bg=color)

        def _hover(self, on):
            if self.state != "disabled" and not self.popup:
                self._bg(COLORS["input_hover"] if on else COLORS["input"])

        def configure(self, require_redraw=False, **kw):
            if "values" in kw:
                self.values = list(kw.pop("values"))
            if "state" in kw:
                self.state = kw.pop("state")
                self.lbl.configure(text_color=COLORS["faint"] if self.state == "disabled" else COLORS["text"])
                self._draw_arrow()
                if self.state == "disabled":
                    self.close()
            if kw or require_redraw:
                super().configure(require_redraw=require_redraw, **kw)

        def get(self):
            return self.var.get()

        def set(self, value):
            self.var.set(value)

        # --- lista
        def toggle(self):
            if self.popup:
                self.close()
            else:
                self.open()

        def open(self):
            if self.state == "disabled" or not self.values:
                return
            if Dropdown._open_one and Dropdown._open_one is not self:
                Dropdown._open_one.close()
            Dropdown._open_one = self
            root = self.winfo_toplevel()
            if not getattr(root, "_amt_dropdown_hook", False):
                root.bind_all("<ButtonPress>", Dropdown._global_click, add="+")
                root._amt_dropdown_hook = True
            s = ui_scaling(self)
            row_h = int(self.ROW_H * s)
            n = len(self.values)
            rows = min(n, self.MAX_ROWS)
            pad = int(4 * s)
            full_h = rows * row_h + 2 * pad + 2
            w = self.winfo_width()
            x = self.winfo_rootx()
            below = self.winfo_rooty() + self.winfo_height() + int(4 * s)
            up = below + full_h > self.winfo_screenheight() - int(40 * s)
            self._geo = (w, full_h, x, below, self.winfo_rooty() - int(4 * s), up)

            p = tk.Toplevel(root)
            self.popup = p
            p.overrideredirect(True)
            p.attributes("-topmost", True)
            p.configure(bg=COLORS["card_border"])
            p.geometry(f"{w}x1+{x}+{below}")
            inner = tk.Frame(p, bg=COLORS["card"])
            inner.pack(fill="both", expand=True, padx=1, pady=1)
            cv = tk.Canvas(inner, bg=COLORS["card"], highlightthickness=0, bd=0, yscrollincrement=row_h)
            self.cv = cv
            if n > rows:
                sb = ctk.CTkScrollbar(inner, command=cv.yview, width=int(12), fg_color=COLORS["card"],
                                      bg_color=COLORS["card"], button_color="#4a4c53", button_hover_color="#5c5f67")
                sb.pack(side="right", fill="y", padx=(0, 2), pady=pad)
                cv.configure(yscrollcommand=sb.set)
            cv.pack(side="left", fill="both", expand=True, padx=(pad, 0), pady=pad)
            frame = tk.Frame(cv, bg=COLORS["card"])
            cv.create_window((0, 0), window=frame, anchor="nw", tags="f")
            cv.bind("<Configure>", lambda e: cv.itemconfigure("f", width=e.width))
            font = (UI_FONT, -int(self.font_size * s))
            ypad = max(0, (row_h - tkfont.Font(font=font).metrics("linespace")) // 2)
            sel_bg = _mix(COLORS["card"], self.accent, 0.35)
            cur = self.var.get()
            self.rows = []
            for i, v in enumerate(self.values):
                sel = v == cur
                r = tk.Label(frame, text=v, anchor="w", font=font, bd=0, padx=int(12 * s), pady=ypad,
                             bg=sel_bg if sel else COLORS["card"], fg="#ffffff" if sel else COLORS["text"])
                r.pack(fill="x")
                r._sel = sel
                r.bind("<Enter>", lambda e, r=r: r.configure(bg=COLORS["input_hover"]) if not r._sel else None)
                r.bind("<Leave>", lambda e, r=r: r.configure(bg=COLORS["card"]) if not r._sel else None)
                r.bind("<ButtonRelease-1>", lambda e, v=v: self._choose(v))
                self.rows.append(r)
            frame.update_idletasks()
            cv.configure(scrollregion=(0, 0, 0, n * row_h))
            for wdg in [cv, frame] + self.rows:
                wdg.bind("<MouseWheel>", self._wheel)
                wdg.bind("<Button-4>", lambda e: cv.yview_scroll(-1, "units"))
                wdg.bind("<Button-5>", lambda e: cv.yview_scroll(1, "units"))
            self.cursor = self.values.index(cur) if cur in self.values else 0
            if n > rows:
                cv.yview_moveto(max(0, self.cursor - rows // 2) / n)
            p.bind("<Escape>", lambda e: self.close())
            p.bind("<Return>", lambda e: self._choose(self.values[self.cursor]))
            p.bind("<Up>", lambda e: self._move(-1))
            p.bind("<Down>", lambda e: self._move(1))
            p.bind("<Key>", self._letter)
            p.update_idletasks()
            _round_corners(p)
            self._bg(COLORS["input_hover"])
            self._draw_arrow(up=True)
            self._animate(0)
            p.focus_force()
            self.after(250, self._watch_focus)

        def _animate(self, step, steps=7):
            if not self.popup:
                return
            w, full_h, x, below, above, up = self._geo
            t = (step + 1) / steps
            h = max(1, int(full_h * (1 - (1 - t) ** 3)))  # łagodne zwolnienie na końcu
            y = above - h if up else below
            self.popup.geometry(f"{w}x{h}+{x}+{y}")
            if step + 1 < steps:
                self.after(14, lambda: self._animate(step + 1, steps))

        def _wheel(self, e):
            d = e.delta
            if IS_MAC:
                step = -1 if d > 0 else 1
            else:
                step = -max(1, abs(d) // 120) if d > 0 else max(1, abs(d) // 120)
            self.cv.yview_scroll(step, "units")
            return "break"

        def _move(self, d):
            self._mark(min(len(self.values) - 1, max(0, self.cursor + d)))

        def _mark(self, i):
            for j, r in enumerate(self.rows):
                if not r._sel:
                    r.configure(bg=COLORS["input_hover"] if j == i else COLORS["card"])
            self.cursor = i
            n, rows = len(self.values), min(len(self.values), self.MAX_ROWS)
            top = self.cv.yview()[0] * n
            if i < top:
                self.cv.yview_moveto(i / n)
            elif i >= top + rows:
                self.cv.yview_moveto((i - rows + 1) / n)

        def _letter(self, e):
            ch = (e.char or "").lower()
            if not ch.isalpha():
                return
            order = list(range(self.cursor + 1, len(self.values))) + list(range(0, self.cursor + 1))
            for i in order:
                if self.values[i].lower().startswith(ch):
                    self._mark(i)
                    return

        def _choose(self, v):
            self.close()
            self.var.set(v)
            if self.command:
                self.command(v)

        def _watch_focus(self):
            if not self.popup:
                return
            try:
                if self.popup.focus_get() is None:  # kliknięto poza programem
                    self.close()
                    return
            except Exception:
                pass
            self.after(250, self._watch_focus)

        @staticmethod
        def _global_click(e):
            d = Dropdown._open_one
            if not d or not d.popup:
                return
            path = str(e.widget)
            if path.startswith(str(d.popup)) or path.startswith(str(d)):
                return
            d.close()

        def close(self):
            if self.popup:
                try:
                    self.popup.destroy()
                except Exception:
                    pass
                self.popup = None
                self._bg(COLORS["input"])
                self._draw_arrow()
            if Dropdown._open_one is self:
                Dropdown._open_one = None

        def destroy(self):
            self.close()
            super().destroy()

    return Dropdown
