import os

from dotenv import load_dotenv

load_dotenv()

PM_BUILDER_NAME = os.getenv("PM_BUILDER_NAME")
PM_API_KEY = os.getenv("PM_API_KEY")
PM_SECRET = os.getenv("PM_SECRET")
PM_PASSPHRASE = os.getenv("PM_PASSPHRASE")

# Auth (required for authenticated CLOB operations)
PM_PRIVATE_KEY = os.getenv("PM_PRIVATE_KEY")
PM_FUNDER_ADDRESS = os.getenv("PM_FUNDER_ADDRESS")
PM_SIGNATURE_TYPE = int(
    os.getenv("PM_SIGNATURE_TYPE", "0")
)  # 0=EOA, 1=Magic, 2=Browser proxy
