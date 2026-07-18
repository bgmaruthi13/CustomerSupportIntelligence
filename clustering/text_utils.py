import re
import unicodedata

from sklearn.feature_extraction.text import CountVectorizer

STOPWORDS_EXTRA = {"unable", "issue", "issues", "error", "errors", "problem", "problems", "failed", "failure"}

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&[a-zA-Z]+;|&#\d+;")
_WHITESPACE_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"\d+")
_KEEP_ONLY_TEXT_RE = re.compile(r"[^a-zA-Z\s]")
_SPECIAL_CHAR_RE = re.compile(r"[^a-zA-Z0-9\s]")

_stemmer = None


def clean_text(text):
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _get_stemmer():
    global _stemmer
    if _stemmer is None:
        from nltk.stem.snowball import SnowballStemmer
        _stemmer = SnowballStemmer("english")
    return _stemmer


def _strip_patterns(text, patterns):
    """Removes every literal phrase or (only if explicitly opted into per-entry)
    regex in `patterns` — each entry is {"pattern": str, "is_regex": bool}. A plain
    phrase is escaped and matched case-insensitively; a broken user-supplied regex is
    skipped rather than allowed to crash the whole pipeline."""
    for entry in patterns:
        raw = entry.get("pattern", "") if isinstance(entry, dict) else str(entry)
        is_regex = entry.get("is_regex", False) if isinstance(entry, dict) else False
        if not raw:
            continue
        try:
            compiled = re.compile(raw if is_regex else re.escape(raw), re.IGNORECASE)
            text = compiled.sub(" ", text)
        except re.error:
            continue
    return text


def preprocess_text(text, settings, skip_stemming=False):
    """Applies the configurable cleanup/text-shape/token pipeline described in
    ClusteringSettings, in a fixed order — every step is a no-op when its toggle is
    off, so an all-off configuration reproduces the raw (post-concatenation) text
    unchanged. See BACKLOG.md's "Per-project Clustering Settings" entry for the full
    rationale behind this order.

    `skip_stemming=True` still honors every other configured toggle (remove_numbers,
    lowercase, stopwords, etc.) but leaves words unstemmed — used for cluster-name/
    keyword generation, where a stemmed fragment like "authent" reads worse than the
    real word "authentication" even though stemming is exactly right for the actual
    TF-IDF vectorization this same settings row also drives."""
    text = text or ""

    if settings.strip_html:
        text = _HTML_TAG_RE.sub(" ", text)
        text = _HTML_ENTITY_RE.sub(" ", text)
    if settings.remove_urls:
        text = _URL_RE.sub(" ", text)
    if settings.remove_emails:
        text = _EMAIL_RE.sub(" ", text)
    if settings.strip_boilerplate and settings.boilerplate_patterns:
        text = _strip_patterns(text, settings.boilerplate_patterns)
    if settings.strip_ticket_id_patterns and settings.ticket_id_patterns:
        text = _strip_patterns(text, settings.ticket_id_patterns)
    if settings.normalize_unicode:
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    if settings.normalize_whitespace:
        text = _WHITESPACE_RE.sub(" ", text).strip()

    if settings.lowercase:
        text = text.lower()

    if settings.keep_only_text:
        # Implies/overlaps remove_special_characters and remove_numbers — treated as
        # redundant no-ops rather than an error when both are also enabled.
        text = _KEEP_ONLY_TEXT_RE.sub(" ", text)
    else:
        if settings.remove_special_characters:
            text = _SPECIAL_CHAR_RE.sub(" ", text)
        if settings.remove_numbers:
            text = _NUMBER_RE.sub(" ", text)

    tokens = text.split()  # whitespace-tokenize; also collapses any gaps left by shape-stripping above
    tokens = [t for t in tokens if len(t) >= settings.min_token_length]

    if settings.remove_stopwords:
        from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS
        stop = ENGLISH_STOP_WORDS.union(settings.stopwords_add or []).difference(settings.stopwords_exclude or [])
        tokens = [t for t in tokens if t.lower() not in stop]

    if settings.enable_stemming and not skip_stemming:
        stemmer = _get_stemmer()
        tokens = [stemmer.stem(t) for t in tokens]

    return " ".join(tokens)


def build_entity_stoplist(tickets):
    """Country/application names occurring in ticket text fragment clusters by
    location/system instead of by issue type (e.g. "Backup Verification Czech" and
    "Backup Verification Ireland" end up as separate clusters for the same underlying
    problem). Those fields are already structured data on the Ticket — stripping them
    from the free text keeps clustering focused on *what* went wrong, not *where*.
    """
    terms = set()
    for t in tickets:
        if t.country and t.country != "Unspecified":
            terms.add(t.country.lower())
        if t.application and t.application not in ("Unspecified", "General IT"):
            terms.add(t.application.lower())
    return terms


def strip_entity_terms(text, stoplist):
    if not text or not stoplist:
        return text or ""
    pattern = re.compile(r"\b(" + "|".join(re.escape(term) for term in stoplist) + r")\b", re.IGNORECASE)
    return pattern.sub(" ", text)


def build_clustering_text(ticket, stoplist, settings=None, skip_stemming=False):
    """Concatenates `settings.source_fields` (defaulting to title-only when no
    settings row exists yet — today's original behavior), strips entity terms
    (country/application names, so clustering groups by *what* went wrong rather than
    *where*), then runs the configurable preprocessing pipeline if a settings
    instance was given. `settings=None` reproduces the exact pre-feature behavior for
    any caller that hasn't been threaded through yet.

    `skip_stemming=True` is for cluster-name/keyword generation — see preprocess_text.
    """
    source_fields = (settings.source_fields if settings and settings.source_fields else None) or ["title"]
    text = " ".join(str(getattr(ticket, field, "") or "") for field in source_fields).strip()
    text = strip_entity_terms(text, stoplist)
    if settings is not None:
        text = preprocess_text(text, settings, skip_stemming=skip_stemming)
    return text


def top_keywords(texts, n=4):
    """Extracts the n most representative terms across a set of ticket texts."""
    cleaned = [clean_text(t) for t in texts if clean_text(t)]
    if not cleaned:
        return []
    try:
        vec = CountVectorizer(stop_words="english", max_features=500, ngram_range=(1, 2), min_df=1)
        matrix = vec.fit_transform(cleaned)
        sums = matrix.sum(axis=0).A1
        terms = vec.get_feature_names_out()
        ranked = sorted(zip(terms, sums), key=lambda x: -x[1])
        keywords = []
        for term, _ in ranked:
            if any(w in STOPWORDS_EXTRA for w in term.split()):
                continue
            keywords.append(term)
            if len(keywords) >= n:
                break
        return keywords or [t for t, _ in ranked[:n]]
    except ValueError:
        return []


def _dedupe_keywords(keywords, n):
    """Drops a keyword once every one of its words is already covered by an
    earlier (higher-ranked) keyword already selected. top_keywords() mixes
    unigrams and bigrams (ngram_range=(1,2)), which routinely surfaces both
    "results" and "returns results" as separate top terms for the same
    cluster — without this, cluster_title() produced literal-duplicate
    titles like "Results / Returns / Returns Results"."""
    selected = []
    covered_words = set()
    for kw in keywords:
        words = set(kw.split())
        if words and words <= covered_words:
            continue
        selected.append(kw)
        covered_words |= words
        if len(selected) >= n:
            break
    return selected


def cluster_title(keywords, fallback="Uncategorized Cluster"):
    if not keywords:
        return fallback
    deduped = _dedupe_keywords(keywords, 3)
    if not deduped:
        return fallback
    return " / ".join(w.title() for w in deduped)
