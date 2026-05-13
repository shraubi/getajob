import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
YOUR_CHAT_ID       = int(os.environ["YOUR_CHAT_ID"])

# Model routing — cheap model for scoring, quality model for agents
SCORE_MODEL    = os.environ.get("SCORE_MODEL",    "claude-haiku-4-5-20251001")
GENERATE_MODEL = os.environ.get("GENERATE_MODEL", "claude-sonnet-4-6")

SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "6"))

# Optional: OpenAI key enables GPT models and higher-quality embeddings
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Embedding model (used by ChromaDB via LiteLLM when OPENAI_API_KEY is set)
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")

# Local ChromaDB persistence path
CHROMA_DIR = os.environ.get("CHROMA_DIR", "storage/chroma_db")
