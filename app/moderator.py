"""Wordlist-based moderation with severity tiers."""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

log = logging.getLogger("voiceguard.moderator")

WORDLIST_PATH = Path("wordlist.txt")


@dataclass
class Verdict:
    flagged: bool
    severity: str  # "none" | "low" | "medium" | "high" | "critical"
    matched: list[str] = field(default_factory=list)


# Severity priority: critical > high (mute) > medium > low (profanity)
SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


class Moderator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.profanity: set[str] = set()  # LOW severity
        self.mute: set[str] = set()       # HIGH severity (auto-mute)
        self.critical: set[str] = set()   # CRITICAL severity (auto-ban / urgent)
        self.medium: set[str] = set()     # MEDIUM (warn only)
        self.stats_flagged = 0

        # Compiled regex per tier (built after load)
        self._patterns: dict[str, re.Pattern | None] = {}

        self._load_wordlist()

    def _load_wordlist(self):
        if not WORDLIST_PATH.exists():
            log.warning("wordlist.txt not found, creating example")
            self._write_example()

        current = None
        with WORDLIST_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].upper()
                    # Format: EN-PROFANITY / EN-MEDIUM / EN-MUTE / EN-CRITICAL
                    if section.endswith("-PROFANITY"):
                        current = self.profanity
                    elif section.endswith("-MEDIUM"):
                        current = self.medium
                    elif section.endswith("-MUTE") or section.endswith("-HIGH"):
                        current = self.mute
                    elif section.endswith("-CRITICAL"):
                        current = self.critical
                    else:
                        current = None
                    continue
                if current is not None:
                    word = line if self.cfg.moderation.case_sensitive else line.lower()
                    current.add(word)

        self._compile_patterns()

    def _write_example(self):
        example = """# VoiceGuard wordlist
# Sections: [EN-PROFANITY] [EN-MEDIUM] [EN-MUTE/HIGH] [EN-CRITICAL]
# Lines starting with # are ignored.

# LOW - Logged only (mild)
[EN-PROFANITY]
damn
hell
crap

# MEDIUM - In-game warning, Discord log
[EN-MEDIUM]
shit
bitch
asshole

# HIGH - Auto-mute + Discord alert
[EN-MUTE]
fuck
fucker
motherfucker
cunt
dick

# CRITICAL - Urgent alert, ping staff, ban-worthy
# Examples: slurs, threats. Customise to your community policy.
[EN-CRITICAL]
# add here
"""
        WORDLIST_PATH.write_text(example, encoding="utf-8")

    def _compile_patterns(self):
        flags = 0 if self.cfg.moderation.case_sensitive else re.IGNORECASE
        for tier, words in [
            ("profanity", self.profanity),
            ("medium", self.medium),
            ("mute", self.mute),
            ("critical", self.critical),
        ]:
            if not words:
                self._patterns[tier] = None
                continue
            if self.cfg.moderation.whole_word:
                pattern = r"\b(" + "|".join(re.escape(w) for w in words) + r")\b"
            else:
                pattern = "(" + "|".join(re.escape(w) for w in words) + ")"
            self._patterns[tier] = re.compile(pattern, flags)

    def update_wordlists(self, profanity=None, mute=None, critical=None, medium=None):
        """Hot-reload wordlists from the plugin (e.g., /voiceguard reload)."""
        if profanity is not None:
            self.profanity = set(w.lower() for w in profanity) if not self.cfg.moderation.case_sensitive else set(profanity)
        if medium is not None:
            self.medium = set(w.lower() for w in medium) if not self.cfg.moderation.case_sensitive else set(medium)
        if mute is not None:
            self.mute = set(w.lower() for w in mute) if not self.cfg.moderation.case_sensitive else set(mute)
        if critical is not None:
            self.critical = set(w.lower() for w in critical) if not self.cfg.moderation.case_sensitive else set(critical)
        self._compile_patterns()

    def evaluate(self, text: str) -> Verdict:
        """Highest-severity match wins. Returns deduped matched words."""
        matches_by_tier = {}
        for tier in ("profanity", "medium", "mute", "critical"):
            pat = self._patterns.get(tier)
            if pat is None:
                continue
            found = pat.findall(text)
            if found:
                # Dedup case-insensitively
                seen = []
                lowered = set()
                for m in found:
                    key = m if self.cfg.moderation.case_sensitive else m.lower()
                    if key not in lowered:
                        lowered.add(key)
                        seen.append(m)
                matches_by_tier[tier] = seen

        if not matches_by_tier:
            return Verdict(flagged=False, severity="none", matched=[])

        # Pick highest severity tier present
        tier_to_severity = {
            "profanity": "low",
            "medium": "medium",
            "mute": "high",
            "critical": "critical",
        }
        # Iterate critical -> profanity, pick first
        for tier in ("critical", "mute", "medium", "profanity"):
            if tier in matches_by_tier:
                self.stats_flagged += 1
                # Include ALL matched words across tiers in the response (helpful context)
                all_matched = []
                seen = set()
                for t in ("critical", "mute", "medium", "profanity"):
                    for w in matches_by_tier.get(t, []):
                        if w.lower() not in seen:
                            seen.add(w.lower())
                            all_matched.append(w)
                return Verdict(
                    flagged=True,
                    severity=tier_to_severity[tier],
                    matched=all_matched,
                )

        return Verdict(flagged=False, severity="none", matched=[])
