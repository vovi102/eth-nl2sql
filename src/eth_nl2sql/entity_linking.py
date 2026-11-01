"""Schema/entity linking without an LLM: steps B1-B5 of docs/proposed-method.md §4.

`EntityLinker.link` turns a question into the record of docs/linking-labels.md: mentions
(entity, address, token, class, literal_address, nil) with E1, E2, ... placeholders,
property senses, the time interval, unsupported senses and warnings. Each mention also
carries its top-5 `candidates` with scores, for recall@5.

- B1: words of the question, each normalized with normalize_alias (the form of alias_norm).
- B2: regexes first (0x addresses, tx hashes, block numbers, time), then word n-grams
  matched exactly against alias_norm, then fuzzily (rapidfuzz). The longest mention wins.
  Type words ("sàn", "ví", "token", ...) next to a mention are not looked up but hint its type.
- B3: score = string match + type compatibility + activity prior (log of token callers or of
  September calls), the prior normalized within each type of a mention's candidates. Below the
  threshold, or for a name the reviewed knowledge base lacks, the mention is NIL.
- B4: a class mention keeps its target and top-N; `class_members` ranks the members.
- B5: phrases of property_phrases -> senses, the sense chosen by the linked entity types, plus
  implicit senses; unsupported senses go to `unsupported`.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from eth_nl2sql.entity_knowledge_base import normalize_alias
from eth_nl2sql.entity_linking_resources import LinkingResources, Target
from eth_nl2sql.entity_linking_time import fold, parse_time

MAX_NGRAM = 5
TOP_K = 5  # candidates kept per mention
DEFAULT_TOP_N = 5  # "major DEXs" without a number (linking-labels.md §2)
LINK_THRESHOLD = 0.9  # string-match score under which a mention is NIL
FUZZY_CUTOFF = 80  # rapidfuzz ratio for a fuzzy candidate at all
FUZZY_MIN_CHARS = 5
HINT_WEIGHT = 0.5  # a type word next to the mention
CONTEXT_WEIGHT = 0.3  # the property senses of the question
PRIOR_WEIGHT = 0.2
TYPE_BONUS = {"entity": 0.1, "class": 0.1, "address": 0.05, "token": 0.0}
TYPE_ORDER = {"entity": 0, "class": 1, "address": 2, "token": 3}

# Words that are names only with case evidence: function words, question words, units, and
# common words that are also token symbols ("send", "one") or entity names ("curve").
STOPWORDS = frozenset(
    """
    a about all an and any are as at be been being between by can could count daily day days
    did do does during each eth ether for from gas gave get give had has have how i if in into
    is it its last list many may me more most much my no not number of on one or our over per
    please sent send show so some than that the their them then there these they this those to
    top total transaction transactions transfer transfers until us value was we week were what
    when where which who whom why will with would you your amount volume fee fees average sum
    max min first second third fourth largest biggest major main big large small leading
    token tokens coin coins wallet wallets address addresses contract contracts block blocks
    swap link base set fund safe origin anyone ink blast scroll across lighter compound curve
    circle sky spark gate maker blur spire titan null usd nft nfts erc eip utc mev dex cex
    ofac l2 rwa tx txs gwei wei id ids time data find return received receive receives
    co bao nhieu so cua va trong ngay thang tuan nam nhan gui vi san dong la cac nhung nao gi
    bi duoc cho voi tu den toi tren duoi tong luong lon nhat hay liet ke biet tinh dem thong
    moi ai khi neu thi ma mot hai ba bon nam sau bay tam chin muoi cao thap trung binh hang
    dau cuoi tien giao dich luot chuyen dia chi hop phi gia tri the nhu bang tai sao vao ra
    di den ve o con chua hoac hon it nhieu toan bo phan tram ty le dung nguoi ket qua
    da se dang roi nay kia ay qua lai cung van rat khac nhau lan
    """.split()
)
# Type words: not looked up as names, but hint the type of the neighbouring mention.
TYPE_HINTS = {
    ("token",): "token", ("dong",): "token", ("coin",): "token", ("stablecoin",): "token",
    ("san",): "entity", ("exchange",): "entity", ("giao", "thuc"): "entity",
    ("protocol",): "entity", ("cong", "ty"): "entity", ("du", "an"): "entity",
    ("company",): "entity", ("project",): "entity", ("platform",): "entity",
    ("nen", "tang"): "entity",
    ("vi",): "address", ("wallet",): "address", ("contract",): "address",
    ("hop", "dong"): "address", ("dia", "chi"): "address", ("address",): "address",
}  # fmt: skip
HINT_WORDS = {w for key in TYPE_HINTS for w in key}
# Right before a name, a quantity word or a transfer verb makes it a token: "nhận bao nhiêu
# Ondo", "chuyển SUSHI". A quantity word is then also the token_amount phrase (benchmark
# gold: "bao nhiêu", "How much", "Lượng").
AMOUNT_BEFORE = {("bao", "nhieu"), ("how", "much"), ("luong",), ("tong", "luong")}
TOKEN_BEFORE = AMOUNT_BEFORE | {("chuyen",), ("transfer",), ("transferred",), ("transferring",)}
HINT_COMPATIBLE = {
    "token": {"token"},
    "entity": {"entity", "class"},
    "address": {"address", "entity"},
}
CONTEXT_COMPATIBLE = {"token": {"token"}, "entity": {"entity", "address", "class"}}
TOKEN_SENSE_TARGETS = {"token", "nft"}  # applies_to of senses about token transfers
ENTITY_SENSE_TARGETS = {"eth", "contract", "address", "transaction"}

# Class size words: "major DEXs", "top 5 DEXs", "5 DEX lớn nhất".
CLASS_BEFORE = {"major", "largest", "biggest", "top", "leading", "main", "big", "large"}
CLASS_AFTER = [("lon", "nhat"), ("hang", "dau"), ("lon",), ("chinh",)]
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10,
}  # fmt: skip

# "số lượt chuyển" also reads "bao nhiêu lượt chuyển"; "number of transactions" also
# "how many transactions" (aggregate senses only).
QUANTIFIERS = [
    ("so",), ("bao", "nhieu"), ("tong", "so"),
    ("number", "of"), ("the", "number", "of"), ("how", "many"), ("total", "number", "of"),
]  # fmt: skip
IRREGULAR = {"sent": "send", "spent": "spend", "paid": "pay", "got": "get"}

ADDRESS_RE = re.compile(r"(?<![0-9a-zA-Z])0x[0-9a-fA-F]{40}(?![0-9a-zA-Z])")
TX_HASH_RE = re.compile(r"(?<![0-9a-zA-Z])0x[0-9a-fA-F]{64}(?![0-9a-zA-Z])")
BLOCK_RE = re.compile(r"(?<!\w)(?:block|khoi)\s+(?:so\s+)?#?\s*(\d{1,9})(?!\w)")
LONG_NUMBER_RE = re.compile(r"(?<![\w.,])\d{7,9}(?![\w.,])")  # a bare block number
VI_LETTERS = set("ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ")
VI_MARKERS = {
    "bao", "nhieu", "cua", "trong", "ngay", "thang", "tuan", "nhan", "gui", "cac", "nhung",
    "duoc", "nao", "giao", "dich", "luot", "chuyen", "nhat", "lon", "bang", "khong",
}  # fmt: skip


def singular(word: str) -> str:
    """English plural -> singular ('DEXs', 'L2s', 'bridges', 'addresses'); else unchanged."""
    if not (word.isascii() and word.isalnum()) or len(word) < (4 if word.isalpha() else 3):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def stem(word: str) -> str:
    """A crude English stem, the same on both sides: 'received', 'receive' -> 'receiv'.

    Vietnamese syllables never end in -s, -ed or -ing, so they only lose a final 'e'.
    """
    word = IRREGULAR.get(word, word)
    if not (word.isascii() and word.isalpha()):
        return word
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    word = singular(word)
    return word[:-1] if word.endswith("e") and len(word) >= 4 else word


@dataclass(frozen=True)
class Word:
    start: int  # character span in the question
    end: int
    text: str
    norm: str


def tokenize(question: str) -> list[Word]:
    """Words of the question: runs of characters that normalize_alias does not turn into space."""
    words, start = [], None
    for i in range(len(question) + 1):
        c = question[i] if i < len(question) else " "
        if normalize_alias(c) or unicodedata.combining(c):
            start = i if start is None else start
        elif start is not None:
            text = question[start:i]
            words.append(Word(start, i, text, normalize_alias(text)))
            start = None
    return words


def detect_lang(question: str, words: list[Word]) -> str:
    if any(c in VI_LETTERS for c in question.lower()):
        return "vi"
    return "vi" if sum(w.norm in VI_MARKERS for w in words) >= 2 else "en"


@dataclass
class Candidate:
    """A span of words that may be a mention, with its targets and string-match scores."""

    start: int  # word index
    end: int  # word index, exclusive
    kind: str  # alias | fuzzy | unreviewed | nil
    targets: dict[Target, float] = field(default_factory=dict)
    key: str = ""

    def overlaps(self, other: Candidate) -> bool:
        return self.start < other.end and other.start < self.end


KIND_RANK = {"alias": 3, "fuzzy": 2, "unreviewed": 1, "nil": 0}


def _upper_evidence(text: str) -> bool:
    return any(c.isupper() for c in text) and not any(c.islower() for c in text)


class EntityLinker:
    """Holds the resources and the indexes built from them; `link` is cheap per question."""

    def __init__(self, resources: LinkingResources) -> None:
        self.resources = resources
        self.parent = {c: p for p, kids in resources.children.items() for c in kids}

        self.class_aliases: dict[tuple[str, ...], set[str]] = defaultdict(set)
        for norm, targets in resources.aliases.items():
            for t in targets:
                if t.type == "class":
                    self.class_aliases[tuple(singular(w) for w in norm.split())].add(t.id)
        # Aliases ending in a number ("coinbase 10"): "coinbase 99" is then a missing wallet.
        self.numbered = {
            m.group(1)
            for norm, targets in resources.aliases.items()
            if any(t.type == "address" for t in targets)
            for m in [re.fullmatch(r"(.+) \d+", norm)]
            if m
        }
        self.fuzzy_choices = sorted(
            norm
            for norm, targets in resources.aliases.items()
            if len(norm) >= FUZZY_MIN_CHARS and any(t.type != "token" for t in targets)
        )

        senses_of: dict[tuple[str, ...], set[str]] = defaultdict(set)
        for p in resources.phrases:
            sense = resources.senses.get(p["sense_id"])
            if sense is None:
                continue
            words = tuple(stem(w) for w in p["phrase_norm"].split())
            senses_of[words].add(p["sense_id"])
            if sense["kind"] == "aggregate":
                head = words
                for q in sorted(QUANTIFIERS, key=len, reverse=True):
                    q = tuple(stem(w) for w in q)
                    if words[: len(q)] == q and len(words) > len(q):
                        head = words[len(q) :]
                        break
                for q in QUANTIFIERS:
                    senses_of[tuple(stem(w) for w in q) + head].add(p["sense_id"])
        self.phrase_senses = dict(senses_of)
        self.max_phrase = max((len(k) for k in senses_of), default=1)

    # ---- B2: mention candidates -------------------------------------------------------

    def _blocked(self, question: str, time: dict | None) -> tuple[list, list[tuple[int, int]]]:
        """Literal addresses, and character spans that no other mention may touch."""
        folded = fold(question)
        literals = [(m.start(), m.end(), m.group().lower()) for m in ADDRESS_RE.finditer(question)]
        spans = [(s, e) for s, e, _ in literals]
        spans += [m.span() for m in TX_HASH_RE.finditer(question)]
        spans += [m.span(1) for m in BLOCK_RE.finditer(folded)]
        spans += [m.span() for m in LONG_NUMBER_RE.finditer(question)]
        if time:
            spans.append(tuple(time["span"]))
        return literals, spans

    def _hint(self, words: list[Word], start: int, end: int) -> str | None:
        """The type word right before or right after words[start:end], if any."""
        for n in (2, 1):
            before = tuple(w.norm for w in words[max(0, start - n) : start])
            after = tuple(w.norm for w in words[end : end + n])
            for key in (before, after):
                if len(key) == n and key in TYPE_HINTS:
                    return TYPE_HINTS[key]
        for n in (2, 1):
            if start >= n and tuple(w.norm for w in words[start - n : start]) in TOKEN_BEFORE:
                return "token"
        return None

    def _keep_target(self, target: Target, words: list[Word], start: int, end: int) -> bool:
        """A target named by common words only needs case evidence ("SEND", "Curve", "USD Coin")."""
        if target.type == "class":
            return True
        if all(w.norm in HINT_WORDS for w in words[start:end]):
            return False  # type words are not looked up ("công ty", "đồng")
        if not all(w.norm in STOPWORDS for w in words[start:end]):
            return True
        if end - start > 1 and target.type != "token":
            return True  # several common words together are a name ("null address")
        text = " ".join(w.text for w in words[start:end])
        if target.type == "token":
            before = words[start - 1].norm if start else ""
            title = end - start > 1 and all(w.text[:1].isupper() for w in words[start:end])
            return _upper_evidence(text) or title or before in ("token", "dong", "coin")
        return text[:1].isupper()

    def _candidates(self, words: list[Word], free: list[bool], property_words: set[int]):
        res = self.resources
        out: list[Candidate] = []
        exact_spans = []
        for i in range(len(words)):
            for n in range(1, MAX_NGRAM + 1):
                j = i + n
                if j > len(words) or not all(free[i:j]):
                    break
                key = " ".join(w.norm for w in words[i:j])
                targets = {t: 1.0 for t in res.aliases.get(key, ())}
                singular_key = tuple(singular(w.norm) for w in words[i:j])
                for class_id in self.class_aliases.get(singular_key, ()):
                    targets[Target("class", class_id)] = 1.0
                # Every token sharing the symbol or name competes on its prior (1,208 USDT).
                # A symbol with non-ASCII characters ('꒤5DT') is looked up even without an
                # alias, so a homoglyph copycat links to itself, never to the real token.
                homoglyph = n == 1 and not targets and not words[i].norm.isascii()
                if homoglyph or any(t.type == "token" for t in targets):
                    for address in res.tokens_by_norm.get(key, ()):
                        targets.setdefault(Target("token", address), 1.0)
                targets = {t: s for t, s in targets.items() if self._keep_target(t, words, i, j)}
                if targets:
                    out.append(Candidate(i, j, "alias", targets, key))
                    exact_spans.append((i, j))
                    continue
                if key in res.unreviewed and self._unreviewed_name(words, i, j):
                    out.append(Candidate(i, j, "unreviewed", {}, key))
                elif (
                    n >= 2
                    and words[j - 1].norm.isdigit()
                    and " ".join(w.norm for w in words[i : j - 1]) in self.numbered
                ):
                    out.append(Candidate(i, j, "nil", {}, key))  # "Coinbase 99"
        out += self._fuzzy(words, free, property_words, exact_spans)
        return out

    def _unreviewed_name(self, words: list[Word], i: int, j: int) -> bool:
        """Not a reviewed alias with a type word around it ("Uniswap Protocol")."""
        if not self._keep_target(Target("entity", ""), words, i, j):
            return False
        norms = [w.norm for w in words[i:j]]
        for lead in range(0, 3):
            for trail in range(0, 3):
                core = norms[lead : len(norms) - trail]
                stripped = [tuple(norms[:lead]), tuple(norms[len(norms) - trail :])]
                if (
                    core
                    and (lead or trail)
                    and all(not s or s in TYPE_HINTS for s in stripped)
                    and " ".join(core) in self.resources.aliases
                ):
                    return False
        return True

    def _fuzzy(self, words, free, property_words, exact_spans) -> list[Candidate]:
        out = []
        for i in range(len(words)):
            for j in range(i + 1, min(i + MAX_NGRAM, len(words)) + 1):
                ws = words[i:j]
                if not all(free[i:j]) or any(k in property_words for k in range(i, j)):
                    break
                if {ws[0].norm, ws[-1].norm} & (STOPWORDS | HINT_WORDS):
                    continue
                key = " ".join(w.norm for w in ws)
                if len(key) < FUZZY_MIN_CHARS or key in self.resources.aliases:
                    continue
                if key in self.resources.unreviewed:
                    continue  # an exact unreviewed name ("AirSwap") is NIL not_reviewed
                digits = re.findall(r"\d+", key)
                targets: dict[Target, float] = {}
                for choice, score, _ in process.extract(
                    key, self.fuzzy_choices, scorer=fuzz.ratio, score_cutoff=FUZZY_CUTOFF, limit=10
                ):
                    if re.findall(r"\d+", choice) != digits:
                        continue  # "Coinbase 99" is not "Coinbase 9"
                    if len(ws) > len(choice.split()):
                        continue  # a typo does not add a word ("jaredfromsubway.eth pay")
                    for t in self.resources.aliases[choice]:
                        if t.type != "token":
                            targets[t] = max(targets.get(t, 0.0), score / 100)
                if not targets:
                    continue
                # Under the threshold the candidate can only become NIL: not for a span that
                # holds an exact match ("Coinbase 10 nhan"), nor for words that are no name.
                contains_exact = any(i <= s and e <= j for s, e in exact_spans)
                if max(targets.values()) < LINK_THRESHOLD and (
                    contains_exact or not self._name_like(ws)
                ):
                    continue
                out.append(Candidate(i, j, "fuzzy", targets, key))
        return out

    @staticmethod
    def _name_like(ws: list[Word]) -> bool:
        return any(any(c.isupper() for c in w.text) and w.norm not in STOPWORDS for w in ws)

    # ---- B5: property phrases ---------------------------------------------------------

    def _phrases(self, words: list[Word], free: list[bool]) -> list[tuple[int, int, set[str]]]:
        stems = [stem(w.norm) for w in words]
        found = []
        for i in range(len(words)):
            for n in range(min(self.max_phrase, len(words) - i), 0, -1):
                if not all(free[i : i + n]):
                    continue
                senses = self.phrase_senses.get(tuple(stems[i : i + n]))
                if senses:
                    found.append((i, i + n, senses))
        # Longest first (then leftmost). A phrase overlapping a chosen one stays only if its
        # other words hold content: "số giao dịch" + "giao dịch blob" are both meant, while
        # "transfers of" only adds "of" to "number of transfers" (benchmark gold), and "nhận"
        # inside "số địa chỉ nhận" adds nothing.
        chosen: list[tuple[int, int, set[str]]] = []
        for p in sorted(found, key=lambda p: (p[0] - p[1], p[0])):
            taken = {k for q in chosen for k in range(q[0], q[1])}
            rest = [words[k].norm for k in range(p[0], p[1]) if k not in taken]
            if len(rest) == p[1] - p[0] or any(w not in STOPWORDS for w in rest):
                chosen.append(p)
        return sorted(chosen, key=lambda p: p[0])

    # ---- B3: ranking -----------------------------------------------------------------

    def _rank(self, c: Candidate, words: list[Word], context: str | None) -> list[dict]:
        hint = self._hint(words, c.start, c.end)
        best_prior: dict[str, float] = defaultdict(float)
        priors = {t: self.resources.prior(t) for t in c.targets}
        for t, p in priors.items():
            best_prior[t.type] = max(best_prior[t.type], p)
        ranked = []
        for t, match in c.targets.items():
            score = match + TYPE_BONUS[t.type]
            if best_prior[t.type]:
                score += PRIOR_WEIGHT * priors[t] / best_prior[t.type]
            if hint:
                score += HINT_WEIGHT if t.type in HINT_COMPATIBLE[hint] else -HINT_WEIGHT
            if context and t.type in CONTEXT_COMPATIBLE[context]:
                score += CONTEXT_WEIGHT
            ranked.append((score, match, t))
        ranked.sort(key=lambda r: (-r[0], TYPE_ORDER[r[2].type], r[2].id))
        return [
            {"type": t.type, "target": t.id, "score": round(score, 4), "match": match}
            for score, match, t in ranked
        ]

    # ---- the record ------------------------------------------------------------------

    def link(self, question: str, question_id: str | None = None) -> dict:
        question = unicodedata.normalize("NFC", question)
        words = tokenize(question)
        time = parse_time(question)
        literals, blocked = self._blocked(question, time)
        free = [not any(s < w.end and w.start < e for s, e in blocked) for w in words]

        phrases = self._phrases(words, free)
        property_words = {k for s, e, _ in phrases for k in range(s, e)}
        chosen = self._resolve(self._candidates(words, free, property_words))
        chosen = self._against_phrases(chosen, phrases, words)
        held = {k for c in chosen for k in range(c.start, c.end)}
        phrases = self._phrases(words, [f and k not in held for k, f in enumerate(free)])
        covered = {k for c in chosen for k in range(c.start, c.end)}
        covered |= {k for s, e, _ in phrases for k in range(s, e)}
        # a class named by a type word ("sàn") may still turn out to be that type word
        type_words = {
            k
            for c in chosen
            if tuple(w.norm for w in words[c.start : c.end]) in TYPE_HINTS
            for k in range(c.start, c.end)
        }
        chosen += self._nil_names(words, free, covered, covered - type_words)
        chosen = self._drop_class_hints(chosen, words)
        chosen.sort(key=lambda c: c.start)

        senses_in_question = {s for _, _, ss in phrases for s in ss}
        applies = {self.resources.senses[s]["applies_to"] for s in senses_in_question}
        token_context = bool(applies & TOKEN_SENSE_TARGETS)
        entity_context = bool(applies & ENTITY_SENSE_TARGETS)
        token_only = any(c.targets and {t.type for t in c.targets} == {"token"} for c in chosen)
        context = (
            "token"
            if token_context and not token_only
            else "entity"
            if entity_context or token_context
            else None
        )

        mentions = [self._mention(c, question, words, context) for c in chosen]
        for start, end, address in literals:
            mentions.append(
                {
                    "span": [start, end],
                    "text": question[start:end],
                    "type": "literal_address",
                    "target": address,
                    "candidates": [{"type": "literal_address", "target": address, "score": 1.0}],
                }
            )
        mentions.sort(key=lambda m: m["span"][0])
        _assign_placeholders(mentions)

        properties, unsupported = self._properties(question, words, phrases, mentions)
        warnings = []
        for m in mentions:
            if (
                m["type"] == "entity"
                and self.resources.entities[m["target"]]["address_set"] == "open"
            ):
                warning = f"open_address_set:{m['target']}"
                if warning not in warnings:
                    warnings.append(warning)
        record = {} if question_id is None else {"question_id": question_id}
        return record | {
            "question": question,
            "lang": detect_lang(question, words),
            "mentions": mentions,
            "properties": properties,
            "time": time,
            "unsupported": unsupported,
            "warnings": warnings,
        }

    def _resolve(self, candidates: list[Candidate]) -> list[Candidate]:
        """Longest first; on equal length the better kind, then the earlier span."""
        chosen: list[Candidate] = []
        for c in sorted(
            candidates,
            key=lambda c: (-(c.end - c.start), -KIND_RANK[c.kind], c.start, -len(c.key)),
        ):
            if not any(c.overlaps(o) for o in chosen):
                chosen.append(c)
        return chosen

    @staticmethod
    def _against_phrases(chosen: list[Candidate], phrases: list, words: list[Word]):
        """The mentions that a property phrase does not swallow.

        A phrase that covers a mention keeps its words ("bao nhiêu ETH"), except on equal spans
        for the name of an entity, address or class, or a token symbol in capitals ("SEND"). A
        phrase that only partly overlaps a mention gives way to it ("giá trị USD" / "USD Coin").
        """
        kept = []
        for c in chosen:
            text = " ".join(w.text for w in words[c.start : c.end])
            strong = c.kind in ("alias", "fuzzy") and (
                any(t.type != "token" for t in c.targets) or _upper_evidence(text)
            )
            swallowed = any(
                s <= c.start and c.end <= e and ((s, e) != (c.start, c.end) or not strong)
                for s, e, _ in phrases
            )
            if not swallowed:
                kept.append(c)
        return kept

    def _nil_names(self, words: list[Word], free: list[bool], covered: set[int], named: set[int]):
        """Runs of name-like words that matched nothing: NIL 'not_in_kb' ("Bybit").

        `named` are the covered words that belong to a name or phrase, not to a type word.
        """

        def name_like(k: int) -> bool:
            w = words[k]
            if not free[k] or k in covered or w.norm in STOPWORDS or w.norm in HINT_WORDS:
                return False
            if w.norm.isdigit():
                return False
            if any(c.isupper() for c in w.text) or not w.norm.isascii():
                return True
            # lower case after a free type word is still a name: "sàn bybit"
            return any(
                k >= n
                and tuple(x.norm for x in words[k - n : k]) in TYPE_HINTS
                and not named & set(range(k - n, k))
                for n in (1, 2)
            )

        out, k = [], 0
        while k < len(words):
            if name_like(k):
                end = k + 1
                while (
                    end < len(words)
                    and (name_like(end) or (words[end].norm.isdigit() and free[end]))
                    and end not in covered
                ):
                    end += 1
                out.append(Candidate(k, end, "nil", {}, ""))
                k = end
            else:
                k += 1
        return out

    @staticmethod
    def _drop_class_hints(chosen: list[Candidate], words: list[Word]) -> list[Candidate]:
        """ "sàn" right before a name is a type word, not the class cex ("sàn Binance")."""
        out = []
        for c in chosen:
            key = tuple(w.norm for w in words[c.start : c.end])
            is_hint = key in TYPE_HINTS and all(t.type == "class" for t in c.targets)
            if is_hint and any(
                o is not c and (o.start == c.end or o.end == c.start) for o in chosen
            ):
                continue
            out.append(c)
        return out

    def _mention(self, c: Candidate, question: str, words: list[Word], context: str | None):
        start, end = words[c.start].start, words[c.end - 1].end
        mention: dict = {"span": [start, end], "text": question[start:end]}
        if c.kind == "unreviewed":
            return mention | {"type": "nil", "nil_reason": "not_reviewed", "candidates": []}
        if c.kind == "nil":
            return mention | {"type": "nil", "nil_reason": "not_in_kb", "candidates": []}
        ranked = self._rank(c, words, context)
        best = ranked[0]
        candidates = [{k: r[k] for k in ("type", "target", "score")} for r in ranked[:TOP_K]]
        if best["match"] < LINK_THRESHOLD:
            return mention | {"type": "nil", "nil_reason": "not_in_kb", "candidates": candidates}
        mention |= {"type": best["type"], "target": best["target"]}
        if best["type"] == "class":
            first, last, top_n = self._class_extent(c, words)
            start, end = words[first].start, words[last - 1].end
            mention |= {"span": [start, end], "text": question[start:end]}
            mention |= {"top_n": top_n or DEFAULT_TOP_N}
            mention |= {"top_n_source": "explicit" if top_n else "default"}
        mention["candidates"] = candidates
        return mention

    @staticmethod
    def _class_extent(c: Candidate, words: list[Word]) -> tuple[int, int, int | None]:
        """Size words around a class mention belong to it: "top 5 DEXs", "DEX lớn nhất"."""
        first, top_n = c.start, None
        while first > 0:
            w = words[first - 1].norm
            if w in CLASS_BEFORE:
                first -= 1
            elif (w.isdigit() or w in NUMBER_WORDS) and top_n is None:
                top_n = int(w) if w.isdigit() else NUMBER_WORDS[w]
                first -= 1
            else:
                break
        last = c.end
        for after in CLASS_AFTER:
            if tuple(w.norm for w in words[last : last + len(after)]) == after:
                last += len(after)
                break
        return first, last, top_n

    def _properties(self, question: str, words: list[Word], phrases: list, mentions: list[dict]):
        res = self.resources
        types = {m["type"] for m in mentions}
        linked = types - {"nil"}
        properties, unsupported, seen = [], [], set()
        for s, e, senses in sorted(phrases, key=lambda p: p[0]):
            if len(senses) > 1:  # the linked entity's type decides ("giá trị")
                about_token = "token" in linked
                fitting = {
                    sid
                    for sid in senses
                    if (res.senses[sid]["applies_to"] in TOKEN_SENSE_TARGETS) == about_token
                    or res.senses[sid]["applies_to"] == "any"
                }
                senses = fitting or senses
            start, end = words[s].start, words[e - 1].end
            for sid in sorted(senses):
                if sid in seen:
                    continue
                seen.add(sid)
                item = {"sense_id": sid, "span": [start, end], "text": question[start:end]}
                if res.senses[sid]["kind"] == "unsupported":
                    unsupported.append(item)
                else:
                    properties.append(item | {"implicit": False})
        if "token_amount" not in seen and "token_amount" in res.senses:
            starts = {w.start: k for k, w in enumerate(words)}
            for m in mentions:
                k = starts.get(m["span"][0]) if m["type"] == "token" else None
                for n in (2, 1):
                    if k is None or k < n:
                        continue
                    if tuple(w.norm for w in words[k - n : k]) in AMOUNT_BEFORE:
                        start, end = words[k - n].start, words[k - 1].end
                        properties.append(
                            {"sense_id": "token_amount", "span": [start, end],
                             "text": question[start:end], "implicit": False}
                        )  # fmt: skip
                        seen.add("token_amount")
                        break
                if "token_amount" in seen:
                    break
        if unsupported:  # nothing will be answered, so no convention applies
            return properties, unsupported
        implicit = []
        if (
            linked
            and "eth_amount" in seen
            and seen & {"received_by", "sent_by", "involving"}
            and not seen & {"tx_success", "tx_failed"}
        ):
            implicit.append("tx_success")  # ETH received/sent counts successful transactions
        if "token" in linked and "of_token" not in seen:
            implicit.append("of_token")
        for sid in implicit:
            if sid in res.senses:
                properties.append({"sense_id": sid, "span": None, "text": None, "implicit": True})
        return properties, unsupported

    # ---- B4 and B8: placeholders -> addresses ----------------------------------------

    def class_members(self, class_id: str, top_n: int) -> list[tuple[str, list[str]]]:
        """Top-N members of a class with their addresses (linking-labels.md §2).

        Members carry the class at entity level; a group (MEV Bot) has no reviewed class, so it
        counts with its addresses that carry the class. A child rolls up to the highest
        ancestor that has the class too (Uniswap V3 -> Uniswap; Circle CCTP stays itself,
        since Circle is no bridge). Each entity with the class adds its own addresses to that
        member, so a member holds the addresses of the entities in its tree that have the
        class: Uniswap Protocol (Permit2) has none, by review, so it does not inflate Uniswap's
        rank. Token contracts are left out (decision c of linking-labels.md: USDC is no bridge
        address of Circle CCTP). Ranked by September calls (to_address), ties by id.
        """
        res = self.resources
        members: dict[str, set[str]] = defaultdict(set)
        for eid, e in res.entities.items():
            if e["kind"] == "group":
                addresses = {
                    a["address"]
                    for a in res.entity_addresses[eid]
                    if class_id in a["classes"] and a["address"] not in res.tokens
                }
                if addresses:
                    members[eid] |= addresses
            elif class_id in e["classes"]:
                top = eid
                while (
                    self.parent.get(top) in res.entities
                    and class_id in res.entities[self.parent[top]]["classes"]
                ):
                    top = self.parent[top]
                members[top] |= {
                    a["address"]
                    for a in res.entity_addresses[eid]
                    if a["address"] not in res.tokens
                }
        ranked = sorted(
            members.items(),
            key=lambda m: (-sum(res.address_calls.get(a, 0) for a in m[1]), m[0]),
        )
        return [(eid, sorted(addresses)) for eid, addresses in ranked[:top_n]]

    def placeholder_addresses(self, record: dict) -> dict[str, list[str]]:
        """{E1: [addresses]} for every placeholder of a linking record (gold or predicted)."""
        out: dict[str, list[str]] = {}
        for m in record["mentions"]:
            name = m.get("placeholder")
            if not name or name in out:
                continue
            if m["type"] == "entity":
                out[name] = self.resources.addresses_of(m["target"])
            elif m["type"] == "class":
                members = self.class_members(m["target"], m["top_n"])
                out[name] = sorted({a for _, addresses in members for a in addresses})
            else:
                out[name] = [m["target"]]
        return out


def _assign_placeholders(mentions: list[dict]) -> None:
    """E1, E2, ... in order of appearance; mentions of one target share a placeholder."""
    names: dict[tuple, str] = {}
    for m in mentions:
        if m["type"] in ("entity", "address", "token", "class"):
            key = (m["type"], m["target"], m.get("top_n"))
            names.setdefault(key, f"E{len(names) + 1}")
            m["placeholder"] = names[key]
    for m in mentions:  # field order of linking-labels.md
        order = ("span", "text", "type", "target", "placeholder", "top_n", "top_n_source")
        rest = {k: v for k, v in m.items() if k not in order}
        items = {k: m[k] for k in order if k in m} | rest
        m.clear()
        m.update(items)


def substitute_placeholders(sql: str, addresses: dict[str, list[str]]) -> str:
    """B8: {E1} -> ('0x…', '0x…'). An empty set becomes (NULL), which matches nothing."""
    for name, values in sorted(addresses.items(), key=lambda kv: -len(kv[0])):
        literal = "(" + ", ".join(f"'{a}'" for a in values) + ")" if values else "(NULL)"
        sql = sql.replace("{" + name + "}", literal)
    return sql
