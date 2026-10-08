import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]
R2_PUBLIC_URL = os.environ["R2_PUBLIC_URL"].rstrip("/")

SANITY_PROJECT_ID = os.environ["SANITY_PROJECT_ID"]
SANITY_DATASET = os.environ.get("SANITY_DATASET", "production")
SANITY_WRITE_TOKEN = os.environ["SANITY_WRITE_TOKEN"]

SANITY_TEAM_U11_ID = os.environ.get("SANITY_TEAM_U11_ID", "")
SANITY_TEAM_U12_ID = os.environ.get("SANITY_TEAM_U12_ID", "")

CLIPS_PAD_SECONDS = int(os.environ.get("CLIPS_PAD_SECONDS", "15"))
CLIPS_OUTPUT_DIR = Path(os.environ.get("CLIPS_OUTPUT_DIR", "/tmp/kickreel-clips"))
