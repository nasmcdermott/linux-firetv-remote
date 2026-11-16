#!/usr/bin/env python3
"""Intent parser + action executor for voice commands.

Takes a transcript string and decides whether it is:
  * a command ("open firefox", "search for my notes") → execute a system action
  * dictation text ("type this into the field") → type into the focused window

Actions:
  launch_app   → open/activate an app by name (KDE-aware via kstart5, with
                 fallbacks: gtk-launch, xdg-open, direct Exec, wmctrl/xdotool)
  search_file  → search the filesystem for a file, or type into Waydroid
  type_text    → type the text into the focused window (ydotool/wtype/xdotool)

Config lives in config/voice_assistant.yaml (+ optional .local override).

App vocabulary: all installed .desktop app names are auto-discovered and fed
to Whisper via initial_prompt so STT recognizes them correctly. Refresh with
--refresh-vocab or delete config/app_vocab.cache.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
VOCAB_CACHE = REPO_ROOT / "config" / "app_vocab.cache"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
VOCAB_CACHE_MAX_AGE_HOURS = 24
DESKTOP_DIRS = (
    "/usr/share/applications",
    os.path.expanduser("~/.local/share/applications"),
)


def load_config() -> dict:
    """Load config/voice_assistant.yaml, optionally overlaid by .local."""
    path = REPO_ROOT / "config" / "voice_assistant.yaml"
    local = REPO_ROOT / "config" / "voice_assistant.local.yaml"
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    if local.exists():
        local_cfg = yaml.safe_load(local.read_text()) or {}
        _deep_merge(cfg, local_cfg)
    return cfg


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# --------------------------------------------------------------------------- app vocabulary

def _desktop_name(desktop: Path) -> str | None:
    """Extract the plain (non-localized) Name= from a .desktop file."""
    try:
        content = desktop.read_text(errors="ignore")
    except OSError:
        return None

    # Skip NoDisplay entries and KCM/settings modules
    if "nodisplay=true" in content.lower():
        return None

    plain_name = ""
    localized_name = ""
    for line in content.splitlines():
        ls = line.strip()
        ll = ls.lower()
        if ll.startswith("name="):
            plain_name = ls.split("=", 1)[1].strip()
            break  # plain Name= wins — stop looking
        if ll.startswith("name[") and "]=" in ll and not localized_name:
            localized_name = ls.split("=", 1)[1].strip()
    if not plain_name:
        plain_name = localized_name
    return plain_name or None


def _desktop_dirs_changed_since(cache_mtime: float) -> bool:
    """Return True if any .desktop directory has been modified since cache_mtime."""
    for d in DESKTOP_DIRS:
        dp = Path(d)
        if not dp.is_dir():
            continue
        try:
            if dp.stat().st_mtime > cache_mtime:
                return True
            # Also check if any individual .desktop file is newer than the cache
            for desktop in dp.glob("*.desktop"):
                if desktop.stat().st_mtime > cache_mtime:
                    return True
        except OSError:
            continue
    return False


def _desktop_name_safe(desktop: Path) -> str | None:
    """Extract the plain (non-localized) Name= from a .desktop file.

    Wrapped in try/except so one malformed file can't hang the whole scan.
    """
    try:
        return _desktop_name(desktop)
    except Exception:  # noqa: BLE001
        return None


def discover_app_names(force_refresh: bool = False) -> list[str]:
    """Return sorted unique app names from all .desktop files.

    Results are cached in config/app_vocab.cache. The cache is refreshed when:
      * force_refresh is True, or
      * the cache is older than refresh_vocab_hours, or
      * any .desktop file has been modified since the cache was written.

    Per-file errors are caught so a single malformed .desktop can't hang the scan.
    """
    cfg = load_config()
    vocab_cfg = cfg.get("vocab", {})
    max_age_hours = vocab_cfg.get("refresh_vocab_hours", VOCAB_CACHE_MAX_AGE_HOURS)

    if not force_refresh and VOCAB_CACHE.exists():
        try:
            cache_mtime = VOCAB_CACHE.stat().st_mtime
            age_hours = (time.time() - cache_mtime) / 3600.0
            if age_hours < max_age_hours and not _desktop_dirs_changed_since(cache_mtime):
                return [line.strip() for line in VOCAB_CACHE.read_text().splitlines()
                        if line.strip()]
        except OSError:
            pass

    names: set[str] = set()
    for d in DESKTOP_DIRS:
        dp = Path(d)
        if not dp.is_dir():
            continue
        for desktop in sorted(dp.glob("*.desktop")):
            name = _desktop_name_safe(desktop)
            if name:
                names.add(name)

    result = sorted(names)
    try:
        VOCAB_CACHE.write_text("\n".join(result) + "\n")
    except OSError:
        pass
    return result


def build_initial_prompt(custom_words: str) -> str | None:
    """Build a short Whisper initial_prompt from a few custom words.

    NOTE: Only use a SHORT list here. Feeding Whisper a large vocabulary
    (e.g. every installed app name) via initial_prompt causes it to hallucinate
    those words into unrelated speech. Keep this to a handful of terms that
    Whisper consistently mishears, or leave it empty.
    """
    words = [w.strip() for w in custom_words.split(",") if w.strip()]
    if not words:
        return None
    return "Words: " + ", ".join(words) + "."


# Whisper appends sentence punctuation ("Open Steam." not "Open Steam"). That
# trailing period breaks .desktop/app-name lookups, so strip it before routing.
_TRAILING_PUNCT = re.compile(r"[\s.,!?;:]+$")


def normalize_transcript(text: str) -> str:
    """Strip surrounding whitespace and trailing sentence punctuation."""
    return _TRAILING_PUNCT.sub("", text.strip())


# ------------------------------------------------------------------ dictation normalization
#
# Whisper frequently transcribes spoken punctuation as the WORD ("hyphen",
# "period", "question mark") instead of the symbol. This map converts those
# spoken forms back to the symbols a user expects when dictating. Matching is
# case-insensitive and word-boundary aware; longer phrases are tried first so
# "question mark" wins over "mark".
#
# This is applied to dictation text before typing. It is intentionally
# conservative: only unambiguous punctuation words are included by default.
# Numbers are NOT converted by default (see convert_spoken_numbers) because
# "one" in "I have one dog" should stay a word.
_PUNCTUATION_MAP: list[tuple[str, str]] = [
    ("question mark", "?"),
    ("exclamation mark", "!"),
    ("exclamation point", "!"),
    ("full stop", "."),
    ("semicolon", ";"),
    ("apostrophe", "'"),
    ("single quote", "'"),
    ("double quote", '"'),
    ("quotation mark", '"'),
    ("open parenthesis", "("),
    ("close parenthesis", ")"),
    ("left paren", "("),
    ("right paren", ")"),
    ("open bracket", "["),
    ("close bracket", "]"),
    ("open brace", "{"),
    ("close brace", "}"),
    ("hyphen", "-"),
    ("dash", "-"),
    ("underscore", "_"),
    ("backslash", "\\"),
    ("slash", "/"),
    ("at sign", "@"),
    ("hash", "#"),
    ("pound sign", "#"),
    ("percent", "%"),
    ("ampersand", "&"),
    ("asterisk", "*"),
    ("plus", "+"),
    ("equals", "="),
    ("colon", ":"),
    ("comma", ","),
    ("period", "."),
    ("ellipsis", "..."),
]

# Optional spoken-number → digit conversion (opt-in via config).
_NUMBER_WORDS: dict[str, str] = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19",
}
_TENS_WORDS: dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def _spoken_number_to_digits(phrase: str) -> str | None:
    """Convert a simple spoken number phrase to digits, or None if not a number.

    Handles 0-19, tens (twenty..ninety), and compounds ("twenty three",
    "twenty-three"). Returns None for anything else so normal words are
    never mangled.
    """
    phrase = phrase.strip().lower().replace("-", " ")
    parts = phrase.split()
    if not parts:
        return None
    total = 0
    for p in parts:
        if p in _NUMBER_WORDS:
            total += int(_NUMBER_WORDS[p])
        elif p in _TENS_WORDS:
            total += _TENS_WORDS[p]
        else:
            return None
    return str(total)


def normalize_dictation(text: str, convert_numbers: bool = False) -> str:
    """Normalize spoken dictation into typed text.

    - Converts spoken punctuation words ("hyphen", "period", "question mark")
      to their symbols.
    - Optionally converts spoken number phrases to digits (opt-in).
    """
    if not text:
        return text

    # Build alternation, longest first.
    keys = sorted({k for k, _ in _PUNCTUATION_MAP}, key=len, reverse=True)
    alt = "|".join(re.escape(k) for k in keys)
    punct_re = re.compile(r"\b(" + alt + r")\b", re.IGNORECASE)
    lookup = {k.lower(): v for k, v in _PUNCTUATION_MAP}

    # Whisper often inserts punctuation *and* keeps the spoken word:
    # "Yes, comma install", "monitor, period Run". Drop the automatic mark
    # before a spoken punctuation word so the replacement doesn't produce
    # "Yes,," / "monitor,.".
    text = re.sub(
        r"([.,!?;:])\s*\b(" + alt + r")\b",
        r" \2",
        text,
        flags=re.IGNORECASE,
    )

    def _replace_punct(m: re.Match) -> str:
        return lookup[m.group(1).lower()]

    text = punct_re.sub(_replace_punct, text)

    if convert_numbers:
        # Replace maximal runs of number words with digits.
        # Match sequences of number words (possibly hyphenated).
        num_tokens = list(_NUMBER_WORDS.keys()) + list(_TENS_WORDS.keys())
        num_re = re.compile(
            r"\b(" + "|".join(re.escape(t) for t in num_tokens) + r")"
            r"(?:[\s-]+(" + "|".join(re.escape(t) for t in num_tokens) + r"))*",
            re.IGNORECASE,
        )

        def _replace_num(m: re.Match) -> str:
            digits = _spoken_number_to_digits(m.group(0))
            return digits if digits is not None else m.group(0)

        text = num_re.sub(_replace_num, text)

    # Clean up spacing around punctuation: remove spaces before punctuation
    # so "hello world ." becomes "hello world.".
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    # Collapse repeated identical punctuation ("Yes,," -> "Yes,").
    text = re.sub(r"([.,!?;:])(?:\s*\1)+", r"\1", text)
    # Collapse multiple spaces.
    text = re.sub(r" {2,}", " ", text)

    return text.strip()


# --------------------------------------------------------------------------- keystroke injection
#
# ydotool is the only reliable injector on KWin/Wayland (wtype needs the
# virtual-keyboard protocol, which KWin does not provide). It must be the
# *only* typer used for a given utterance: falling through to wtype/xdotool
# after a partial ydotool timeout duplicates the text and leaves the key that
# was down stuck in uinput.
#
# Default ydotool timing is 20 ms delay + 20 ms hold = 40 ms/char. A 300-char
# dictation then takes 12 s; the old hard 10 s timeout killed it mid-key. Use a
# shorter, still reliable, pair and a length-aware timeout.
YDOTOOL_KEY_DELAY_MS = 10
YDOTOOL_KEY_HOLD_MS = 10
TYPE_TIMEOUT_MIN = 30.0
TYPE_TIMEOUT_MAX = 120.0
TYPE_TIMEOUT_PER_CHAR = 0.05


def _typing_timeout(text: str) -> float:
    return min(TYPE_TIMEOUT_MAX, max(TYPE_TIMEOUT_MIN,
                                    len(text) * TYPE_TIMEOUT_PER_CHAR + 10.0))


def release_all_keys() -> None:
    """Release every standard key through ydotool.

    Recovery path after a typer is killed/fails mid-string. Without this the
    key that was down keeps auto-repeating and every later press of that key
    is swallowed until something sends its key-up.
    """
    if shutil.which("ydotool") is None:
        return
    keys = [f"{code}:0" for code in range(1, 256)]
    try:
        subprocess.run(
            ["ydotool", "key", "-d", "0", *keys],
            capture_output=True, text=True, timeout=10, check=False,
        )
        log(">> typing recovery: released all keys")
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"!! typing recovery failed: {e}")


# --------------------------------------------------------------------------- command handler

class CommandHandler:
    def __init__(self, config: dict | None = None, app_names: list[str] | None = None):
        self.cfg = config or load_config()
        self.app_names = app_names or discover_app_names()
        self.patterns = self.cfg.get("command", {}).get("patterns", [])
        self.compiled = [(re.compile(p["regex"], re.IGNORECASE), p["action"])
                         for p in self.patterns]
        self.host_cfg = self.cfg.get("host", {})
        self.waydroid_cfg = self.cfg.get("waydroid", {})

    # ------------------------------------------------------------------ public

    def handle(self, text: str) -> str:
        """Parse *text* and execute the matching action. Returns a human log line.

        Matching runs against a punctuation-stripped copy so Whisper's trailing
        period ("Open Steam.") doesn't break app-name lookup. The original text
        is kept for dictation so typed sentences keep their punctuation.
        """
        raw = text.strip()
        if not raw:
            return "(empty)"

        normalized = normalize_transcript(raw)
        for pattern, action in self.compiled:
            m = pattern.match(normalized)
            if m:
                return self._run_action(action, m, raw)

        return f"(no match) {raw}"

    # ------------------------------------------------------------------ actions

    def _run_action(self, action: str, m: re.Match, text: str) -> str:
        groups = m.groups()
        if action == "launch_app":
            target = groups[0] if groups else text
            return self.launch_app(target)
        if action == "search_file":
            query = groups[0] if groups else text
            return self.search_file(query)
        if action == "type_text":
            body = groups[-1] if groups else text
            body = re.sub(r"^(?:type|write|dictate)\s+", "", body, flags=re.I).strip()
            return self.type_text(body)
        return f"(unknown action {action}) {text}"

    # ------------------------------------------------------------ launch_app

    def launch_app(self, name: str) -> str:
        """Open or activate an application by name.

        On KDE/Wayland, kstart5 is preferred because it can activate an already
        running window as well as launch a new one. Falls back to gtk-launch,
        xdg-open, direct Exec execution, and finally wmctrl/xdotool focusing.
        """
        name = normalize_transcript(name)

        # 1. Check desktop_redirects override (e.g. "files" → "org.kde.dolphin")
        redirects = self.cfg.get("desktop_redirects", {})
        redirect_key = name.lower()
        if redirect_key in redirects:
            desktop_stem = redirects[redirect_key]
            # Use the redirect directly, bypassing .desktop discovery
            return self._launch_by_desktop_stem(desktop_stem, name)

        # 2. Waydroid app map override
        if self.waydroid_cfg.get("enabled"):
            wd_app = self._waydroid_launch(name)
            if wd_app:
                return wd_app

        # 3. Resolve the .desktop file for this name (exact + substring)
        desktop_stem = self._find_desktop(name)

        # 4. Fuzzy match against known app names (safety net for misheard
        #    proper nouns: "Open Stm" → "Steam", "Open Steam" → "Steam").
        if desktop_stem is None and self.app_names:
            name_l = name.lower()
            name_map = {n.lower(): n for n in self.app_names}
            matches = difflib.get_close_matches(name_l, list(name_map.keys()),
                                                n=1, cutoff=0.5)
            if matches:
                matched = name_map[matches[0]]
                desktop_stem = self._find_desktop(matched)
                if desktop_stem:
                    log(f">> fuzzy matched '{name}' → '{matched}'")

        if not desktop_stem:
            # 5. Fallback: run as a shell command
            if self.host_cfg.get("fallback_command", True):
                try:
                    subprocess.Popen(name.split(),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return f">> ran command: {name}"
                except FileNotFoundError:
                    return f"!! could not find app or command: {name}"
            return f"!! no .desktop match for: {name}"

        return self._launch_by_desktop_stem(desktop_stem, name)

    def _launch_by_desktop_stem(self, desktop_stem: str, spoken_name: str) -> str:
        """Launch/activate a .desktop file by stem, trying kstart5 first (KDE)."""
        # Try kstart5 first (KDE: launches or activates existing window on Wayland)
        if shutil.which("kstart5"):
            try:
                r = subprocess.run(
                    ["kstart5", "--application", desktop_stem],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                if r.returncode == 0:
                    return f">> activated {spoken_name} ({desktop_stem})"
            except (subprocess.TimeoutExpired, OSError):
                pass

        # Try gtk-launch
        if shutil.which("gtk-launch"):
            try:
                subprocess.Popen(["gtk-launch", desktop_stem],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f">> launched {spoken_name} ({desktop_stem})"
            except FileNotFoundError:
                pass

        # Try gio launch on the .desktop file (GNOME and other glib desktops)
        desktop_path = self._find_desktop_path(desktop_stem)
        if desktop_path and shutil.which("gio"):
            try:
                result = subprocess.run(
                    ["gio", "launch", str(desktop_path)],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                if result.returncode == 0:
                    return f">> launched {spoken_name} via gio ({desktop_stem})"
            except (subprocess.TimeoutExpired, OSError):
                pass

        # Try xdg-open on the .desktop file
        if desktop_path and shutil.which("xdg-open"):
            try:
                subprocess.Popen(["xdg-open", str(desktop_path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f">> launched {spoken_name} via xdg-open ({desktop_stem})"
            except FileNotFoundError:
                pass

        # Fallback: run the spoken name as a shell command
        if self.host_cfg.get("fallback_command", True):
            try:
                subprocess.Popen(spoken_name.split(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f">> ran command: {spoken_name}"
            except FileNotFoundError:
                return f"!! could not launch {spoken_name} ({desktop_stem})"

        return f"!! could not launch {spoken_name} ({desktop_stem})"

    def _find_desktop(self, name: str) -> str | None:
        """Find a .desktop file stem matching *name*.

        Prefers exact matches on stem or Name=; then substring matches,
        skipping KCM/settings modules and NoDisplay entries.
        """
        name_l = name.lower()
        substring_matches: list[str] = []

        for d in DESKTOP_DIRS:
            dp = Path(d)
            if not dp.is_dir():
                continue
            for desktop in sorted(dp.glob("*.desktop")):
                try:
                    content = desktop.read_text(errors="ignore")
                except OSError:
                    continue

                if "nodisplay=true" in content.lower():
                    continue

                desktop_name = _desktop_name_safe(desktop)
                stem_l = desktop.stem.lower()
                # Remove trailing .desktop if double-extended
                if stem_l.endswith(".desktop"):
                    stem_l = stem_l[:-len(".desktop")]
                name_l_check = (desktop_name or "").lower()

                # Exact match on stem or Name
                if stem_l == name_l or name_l_check == name_l:
                    return desktop.stem

                # Substring match — skip KCM/settings modules
                is_kcm = "kcm" in stem_l or (desktop_name and "kcm" in desktop_name.lower())
                if name_l in stem_l or (desktop_name and name_l in name_l_check):
                    if is_kcm:
                        continue
                    substring_matches.append(desktop.stem)

        if substring_matches:
            return substring_matches[0]
        return None

    def _find_desktop_path(self, name_or_stem: str) -> Path | None:
        """Return the full Path to a matching .desktop file, or None."""
        for d in DESKTOP_DIRS:
            dp = Path(d)
            if not dp.is_dir():
                continue
            for desktop in sorted(dp.glob("*.desktop")):
                stem = desktop.stem
                if stem.endswith(".desktop"):
                    stem = stem[:-len(".desktop")]
                if stem == name_or_stem or desktop.stem == name_or_stem:
                    return desktop
        return None

    def _focus_existing_window(self, name: str) -> str | None:
        """Bring an already-running window to front (X11/XWayland best-effort).

        On native Wayland this is compositor-dependent; kstart5 (tried first in
        launch_app) handles KDE/Wayland activation. This is an X11 fallback.
        """
        name_l = name.lower()

        if shutil.which("wmctrl"):
            try:
                r = subprocess.run(
                    ["wmctrl", "-lx"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                for line in r.stdout.splitlines():
                    parts = line.split(None, 3)
                    if len(parts) < 4:
                        continue
                    win_class = parts[2].lower()
                    win_title = parts[3].lower() if len(parts) > 3 else ""
                    if name_l in win_class or name_l in win_title:
                        subprocess.run(
                            ["wmctrl", "-ia", parts[0]],
                            capture_output=True, timeout=5, check=False,
                        )
                        return f">> brought {name} to front"
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

        if shutil.which("xdotool"):
            try:
                r = subprocess.run(
                    ["xdotool", "search", "--name", name],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                for win_id in r.stdout.splitlines():
                    if win_id.strip():
                        subprocess.run(
                            ["xdotool", "windowactivate", win_id.strip()],
                            capture_output=True, timeout=5, check=False,
                        )
                        return f">> brought {name} to front"
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

        return None

    def _waydroid_launch(self, name: str) -> str | None:
        """Launch an app in Waydroid via adb, using config app_map if present."""
        app_map = self.waydroid_cfg.get("app_map", {})
        key = name.lower().strip()
        if key in app_map:
            target = app_map[key]
        else:
            target = name
        if "/" not in target:
            target = f"{target}/{target}"
        try:
            subprocess.Popen(["adb", "shell", "am", "start", "-n", target],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f">> launched Waydroid app: {name}"
        except FileNotFoundError:
            return "!! adb not found (install android-tools)"
        except Exception as e:
            return f"!! Waydroid launch failed: {e}"

    # ------------------------------------------------------------ search_file

    def search_file(self, query: str) -> str:
        """Search for a file matching *query* and open the first result."""
        query = query.strip()
        if not query:
            return "(empty search query)"

        if self.waydroid_cfg.get("enabled"):
            return self._waydroid_type(query)

        for cmd in (["fd", "--type", "f", query],
                    ["locate", "-i", "-l", "5", query],
                    ["find", os.path.expanduser("~"), "-iname", f"*{query}*"]):
            if not shutil.which(cmd[0]):
                continue
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                lines = [line for line in r.stdout.splitlines() if line.strip()]
                if lines:
                    first = lines[0]
                    path = first.split()[0] if " " in first else first
                    subprocess.Popen(["xdg-open", path],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return f">> opened {path}"
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue
        return f"!! no file found matching: {query}"

    def _waydroid_type(self, text: str) -> str:
        """Type text into the focused Waydroid field, optionally press Enter."""
        try:
            subprocess.run(["adb", "shell", "input", "text", text],
                           check=False, capture_output=True)
            if self.waydroid_cfg.get("type_then_enter", True):
                subprocess.run(["adb", "shell", "input", "keyevent", "KEYCODE_ENTER"],
                               check=False, capture_output=True)
            return f">> typed into Waydroid: {text}"
        except FileNotFoundError:
            return "!! adb not found (install android-tools)"

    # ------------------------------------------------------------ type_text

    def type_text(self, text: str) -> str:
        """Type *text* into the currently focused window (host)."""
        text = text.strip()
        if not text:
            return "(empty)"

        # Normalize spoken punctuation/numbers into typed symbols.
        convert_numbers = bool(
            self.cfg.get("command", {}).get("dictation", {}).get("convert_numbers", False)
        )
        text = normalize_dictation(text, convert_numbers=convert_numbers)
        timeout = _typing_timeout(text)

        # Single typer per utterance. ydotool is first because it works on
        # KWin/Wayland; stdin avoids argv escaping/limits. Never fall through
        # after a timeout: that is what left a key stuck and double-typed.
        if shutil.which("ydotool"):
            try:
                r = subprocess.run(
                    ["ydotool", "type",
                     f"--key-delay={YDOTOOL_KEY_DELAY_MS}",
                     f"--key-hold={YDOTOOL_KEY_HOLD_MS}",
                     "--file", "-"],
                    input=text, capture_output=True, text=True,
                    timeout=timeout, check=False,
                )
                if r.returncode == 0:
                    return f">> typed: {text[:60]}"
                log(f"ydotool type failed rc={r.returncode}: "
                    f"{(r.stderr or '').strip()[:200]}")
            except subprocess.TimeoutExpired:
                log(f"ydotool type timed out after {timeout:.0f}s "
                    f"(len={len(text)}); releasing keys")
            except OSError as e:
                log(f"ydotool type could not start: {e}")
            release_all_keys()
            return f"!! typing failed (ydotool; text length {len(text)})"

        # Fallbacks only when ydotool is absent. Use one, never a chain after
        # it has started producing input.
        if shutil.which("wtype") and os.environ.get("WAYLAND_DISPLAY"):
            try:
                r = subprocess.run(["wtype", "-"], input=text,
                                   capture_output=True, text=True,
                                   timeout=timeout, check=False)
                if r.returncode == 0:
                    return f">> typed: {text[:60]}"
                log(f"wtype failed rc={r.returncode}")
            except (subprocess.TimeoutExpired, OSError) as e:
                log(f"wtype failed: {e}")
            return f"!! typing failed (wtype; text length {len(text)})"

        if shutil.which("xdotool") and os.environ.get("DISPLAY"):
            try:
                r = subprocess.run(
                    ["xdotool", "type", "--clearmodifiers", text],
                    capture_output=True, text=True, timeout=timeout, check=False,
                )
                if r.returncode == 0:
                    return f">> typed: {text[:60]}"
                log(f"xdotool failed rc={r.returncode}")
            except (subprocess.TimeoutExpired, OSError) as e:
                log(f"xdotool failed: {e}")
            return f"!! typing failed (xdotool; text length {len(text)})"

        return "!! no typer found (ydotool | wtype | xdotool)"


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Parse a voice transcript and act on it.")
    ap.add_argument("text", nargs="?", help="transcript to handle (stdin if omitted)")
    ap.add_argument("--config", help="path to voice_assistant.yaml override")
    ap.add_argument("--refresh-vocab", action="store_true",
                    help="force re-scan of .desktop files for app vocabulary")
    args = ap.parse_args()

    cfg = load_config()
    app_names = discover_app_names(force_refresh=args.refresh_vocab)
    handler = CommandHandler(cfg, app_names)

    text = args.text if args.text is not None else sys.stdin.read().strip()
    print(handler.handle(text))


if __name__ == "__main__":
    main()