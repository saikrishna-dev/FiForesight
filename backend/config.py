import os
from pathlib import Path
from dotenv import load_dotenv
import logging

# Always load .env from the backend/ directory, regardless of where uvicorn is launched from
load_dotenv(Path(__file__).parent / ".env")

class Config:
    INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
    INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
    INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "WeekendDevelopment")
    INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "FiForesightBucket")
    SERP_API_KEY = os.getenv("SERP_API_KEY")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    PORT = int(os.getenv("PORT", 8000))

class SanitizeHttpxFilter(logging.Filter):
    SENSITIVE_PARAMS = {"api_key", "token", "secret", "password", "key"}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("httpx"):
            msg = record.getMessage()
            for param in self.SENSITIVE_PARAMS:
                # crude but effective — mask value after param name
                import re
                msg = re.sub(
                    rf"({param}=)[^&\s\"']+",
                    r"\1***",
                    msg,
                    flags=re.IGNORECASE,
                )
            record.msg = msg
            record.args = ()
        return True
