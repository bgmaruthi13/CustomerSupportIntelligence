from clustering.models import ClusteringSettings
from clustering.text_utils import STOPWORDS_EXTRA

# Traditional ML's defaults are the best-known configuration for a bag-of-words method,
# not merely "what the code did before this feature existed": title+description for more
# vocabulary, full cleanup, stemming on to collapse inflected forms ("timeout"/"timed out"/
# "failing"/"failed" -> one token, the exact follow-up the original under-segmentation fix
# flagged as a natural next step), and a min_token_length floor to drop noise fragments.
# Generative AI's defaults ship close-to-off on anything that reshapes natural language —
# the embedding model wants intact sentences — but keep the unambiguous-noise-removal
# toggles on, since those never carried meaningful signal for embeddings either, and also
# adds description for more context (unlike TF-IDF, embeddings only benefit from more
# natural text, never "noisy").
TRADITIONAL_ML_DEFAULTS = dict(
    source_fields=["title", "description"],
    normalize_whitespace=True, strip_html=True, remove_urls=True, remove_emails=True,
    strip_boilerplate=False, boilerplate_patterns=[],
    normalize_unicode=False, strip_ticket_id_patterns=False, ticket_id_patterns=[],
    lowercase=True, keep_only_text=False, remove_special_characters=True, remove_numbers=True,
    remove_stopwords=True, stopwords_add=sorted(STOPWORDS_EXTRA), stopwords_exclude=[],
    min_token_length=3, enable_stemming=True,
    min_document_frequency=2, max_document_frequency=0.6,
)

GENERATIVE_AI_DEFAULTS = dict(
    source_fields=["title", "description"],
    normalize_whitespace=True, strip_html=True, remove_urls=True, remove_emails=True,
    strip_boilerplate=False, boilerplate_patterns=[],
    normalize_unicode=False, strip_ticket_id_patterns=False, ticket_id_patterns=[],
    lowercase=False, keep_only_text=False, remove_special_characters=False, remove_numbers=False,
    remove_stopwords=False, stopwords_add=[], stopwords_exclude=[],
    min_token_length=1, enable_stemming=False,
    min_document_frequency=2, max_document_frequency=0.95,  # not applicable — no vectorizer in this engine
)


def default_settings_for(project, engine):
    """An unsaved ClusteringSettings instance with this engine's default values —
    used both as the get_or_default() fallback and to seed a freshly-created row on
    first save from the settings page."""
    defaults = TRADITIONAL_ML_DEFAULTS if engine == "traditional_ml" else GENERATIVE_AI_DEFAULTS
    return ClusteringSettings(project=project, engine=engine, **defaults)


def get_or_default(project, engine):
    """Looks up this project+engine's saved settings; falls back to an unsaved
    default instance if none exists yet — no migration/backfill needed, and a project
    that never visits the settings page keeps behaving exactly like today's hardcoded
    pipeline until it does."""
    existing = ClusteringSettings.objects.filter(project=project, engine=engine).first()
    return existing if existing is not None else default_settings_for(project, engine)
