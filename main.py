import os
from fastapi import FastAPI

app = FastAPI()
DATABASE_URL = os.environ.get("DATABASE_URL", "not set")

@app.get("/")
def read_root():
    return {"status": "alive"}

@app.get("/db-check")
def db_check():
    return {"database_url_present": DATABASE_URL != "not set"}