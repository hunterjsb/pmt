import os

from dotenv import load_dotenv

load_dotenv()

PM_BUILDER_NAME = os.getenv("PM_BUILDER_NAME")
PM_API_KEY = os.getenv("PM_API_KEY")
PM_SECRET = os.getenv("PM_SECRET")
PM_PASSPHRASE = os.getenv("PM_PASSPHRASE")
